"""Thread-safe lifecycle and capacity control for gateway session runtimes.

This module owns only runtime policy and state.  It intentionally knows nothing
about transports, persistence, agents, or the gateway protocol; callers keep
those objects in the mutable session mapping registered for each runtime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import RLock
from typing import Any, Callable, MutableMapping


class RuntimeState(Enum):
    """Lifecycle of the materialized resources for a logical session."""

    COLD = auto()
    BUILDING = auto()
    HOT_IDLE = auto()
    EXECUTING = auto()
    CLOSED = auto()


class RuntimeCapacityError(RuntimeError):
    """A runtime lease could not be granted under the configured policy."""


class ExecutionCapacityExceeded(RuntimeCapacityError):
    """The global execution lease limit is exhausted."""


class RuntimeClosed(RuntimeError):
    """The runtime identity has been closed and cannot be reused."""


@dataclass(frozen=True)
class RuntimePolicy:
    """Bounds applied to materialized and concurrently executing runtimes."""

    max_hot_idle: int = 8
    max_executing: int = 4
    idle_ttl_s: float | None = None
    rss_soft_limit_bytes: int | None = None
    rss_eviction_batch: int = 1

    def __post_init__(self) -> None:
        if self.max_hot_idle < 0:
            raise ValueError("max_hot_idle must be non-negative")
        if self.max_executing < 1:
            raise ValueError("max_executing must be positive")
        if self.idle_ttl_s is not None and self.idle_ttl_s < 0:
            raise ValueError("idle_ttl_s must be non-negative")
        if self.rss_soft_limit_bytes is not None and self.rss_soft_limit_bytes < 0:
            raise ValueError("rss_soft_limit_bytes must be non-negative")
        if self.rss_eviction_batch < 1:
            raise ValueError("rss_eviction_batch must be positive")


class RuntimeLease:
    """Idempotently releasable ownership token fenced by runtime generation."""

    def __init__(
        self,
        manager: "SessionRuntimeManager",
        session_id: str,
        kind: str,
        generation: int,
    ) -> None:
        self._manager = manager
        self.session_id = session_id
        self.kind = kind
        self.generation = generation
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def _mark_released(self) -> None:
        self._released = True

    def release(self) -> bool:
        if self._released:
            return False
        return self._manager._release(self)

    def __enter__(self) -> "RuntimeLease":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


@dataclass
class _Runtime:
    session: MutableMapping[str, Any]
    state: RuntimeState
    generation: int
    last_used: float
    build_lease: RuntimeLease | None = None
    execution_lease: RuntimeLease | None = None
    resource_leases: set[RuntimeLease] = field(default_factory=set)
    attached_keys: set[str] = field(default_factory=set)


class SessionRuntimeManager:
    """Own runtime state without coupling resource cleanup to the manager lock."""

    def __init__(
        self,
        policy: RuntimePolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cleanup: Callable[[str, dict[str, Any]], None] | None = None,
        rss_probe: Callable[[], int] | None = None,
    ) -> None:
        self.policy = policy or RuntimePolicy()
        self._clock = clock
        self._cleanup = cleanup or (lambda _session_id, _resources: None)
        self._rss_probe = rss_probe
        self._lock = RLock()
        self._runtimes: dict[str, _Runtime] = {}
        self.sessions: dict[str, MutableMapping[str, Any]] = {}

    def register(
        self,
        session_id: str,
        session: MutableMapping[str, Any] | None = None,
    ) -> MutableMapping[str, Any]:
        with self._lock:
            if session_id in self._runtimes:
                if self._runtimes[session_id].state is RuntimeState.CLOSED:
                    raise RuntimeClosed(f"runtime is closed: {session_id}")
                raise KeyError(f"runtime already registered: {session_id}")
            value = session if session is not None else {}
            self.sessions[session_id] = value
            self._runtimes[session_id] = _Runtime(
                session=value,
                state=RuntimeState.COLD,
                generation=0,
                last_used=self._clock(),
            )
            return value

    def state(self, session_id: str) -> RuntimeState:
        with self._lock:
            return self._runtimes[session_id].state

    def acquire_build(self, session_id: str) -> RuntimeLease:
        with self._lock:
            runtime = self._runtimes[session_id]
            if runtime.state is not RuntimeState.COLD:
                raise RuntimeError(f"runtime is not cold: {session_id}")
            runtime.generation += 1
            lease = RuntimeLease(self, session_id, "build", runtime.generation)
            runtime.build_lease = lease
            runtime.state = RuntimeState.BUILDING
            return lease

    def finish_build(
        self,
        session_id: str,
        generation: int,
        **resources: Any,
    ) -> bool:
        with self._lock:
            runtime = self._runtimes[session_id]
            if (
                runtime.state is not RuntimeState.BUILDING
                or runtime.generation != generation
            ):
                return False
            runtime.session.update(resources)
            runtime.attached_keys.update(resources)
            if runtime.build_lease is not None:
                runtime.build_lease._mark_released()
            runtime.build_lease = None
            runtime.state = RuntimeState.HOT_IDLE
            runtime.last_used = self._clock()
        self._enforce_hot_idle_cap()
        return True

    def finish_build_and_acquire_execution(
        self,
        session_id: str,
        generation: int,
        **resources: Any,
    ) -> RuntimeLease | None:
        """Atomically attach a completed build and reserve an execution slot.

        Returning ``None`` means the build generation is stale. Capacity errors
        are raised before resources are attached, leaving cleanup to the caller.
        """
        with self._lock:
            runtime = self._runtimes[session_id]
            if (
                runtime.state is not RuntimeState.BUILDING
                or runtime.generation != generation
            ):
                return None
            if sum(
                item.state is RuntimeState.EXECUTING
                for item in self._runtimes.values()
            ) >= self.policy.max_executing:
                raise ExecutionCapacityExceeded(
                    f"max_executing={self.policy.max_executing} is exhausted"
                )
            runtime.session.update(resources)
            runtime.attached_keys.update(resources)
            if runtime.build_lease is not None:
                runtime.build_lease._mark_released()
            runtime.build_lease = None
            lease = RuntimeLease(self, session_id, "execute", runtime.generation)
            runtime.execution_lease = lease
            runtime.state = RuntimeState.EXECUTING
            runtime.last_used = self._clock()
            return lease

    def acquire_execution(self, session_id: str) -> RuntimeLease:
        with self._lock:
            runtime = self._runtimes[session_id]
            if runtime.state is not RuntimeState.HOT_IDLE:
                raise RuntimeError(f"runtime is not hot and idle: {session_id}")
            if sum(
                item.state is RuntimeState.EXECUTING
                for item in self._runtimes.values()
            ) >= self.policy.max_executing:
                raise ExecutionCapacityExceeded(
                    f"max_executing={self.policy.max_executing} is exhausted"
                )
            lease = RuntimeLease(self, session_id, "execute", runtime.generation)
            runtime.execution_lease = lease
            runtime.state = RuntimeState.EXECUTING
            runtime.last_used = self._clock()
            return lease

    def attach_resources(
        self,
        session_id: str,
        generation: int,
        **resources: Any,
    ) -> bool:
        """Attach late-created resources only to the current hot generation."""
        with self._lock:
            runtime = self._runtimes[session_id]
            if (
                runtime.generation != generation
                or runtime.state not in {RuntimeState.HOT_IDLE, RuntimeState.EXECUTING}
            ):
                return False
            runtime.session.update(resources)
            runtime.attached_keys.update(resources)
            runtime.last_used = self._clock()
            return True

    def acquire_resource(self, session_id: str) -> RuntimeLease:
        with self._lock:
            runtime = self._runtimes[session_id]
            if runtime.state is not RuntimeState.HOT_IDLE:
                raise RuntimeError(f"runtime is not hot and idle: {session_id}")
            lease = RuntimeLease(self, session_id, "resource", runtime.generation)
            runtime.resource_leases.add(lease)
            runtime.last_used = self._clock()
            return lease

    def is_pinned(self, session_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes[session_id]
            return bool(
                runtime.build_lease
                or runtime.execution_lease
                or runtime.resource_leases
            )

    def hibernate(self, session_id: str) -> bool:
        with self._lock:
            resources = self._hibernate_locked(session_id)
        if resources is None:
            return False
        self._cleanup(session_id, resources)
        return True

    def _hibernate_locked(self, session_id: str) -> dict[str, Any] | None:
        runtime = self._runtimes[session_id]
        if runtime.state is not RuntimeState.HOT_IDLE or runtime.resource_leases:
            return None
        resources = {
            key: runtime.session[key]
            for key in runtime.attached_keys
            if key in runtime.session
        }
        for key in runtime.attached_keys:
            runtime.session.pop(key, None)
        runtime.attached_keys.clear()
        runtime.generation += 1
        runtime.state = RuntimeState.COLD
        runtime.last_used = self._clock()
        return resources

    def sweep(self) -> list[str]:
        under_pressure = False
        if self.policy.rss_soft_limit_bytes is not None and self._rss_probe is not None:
            try:
                under_pressure = self._rss_probe() > self.policy.rss_soft_limit_bytes
            except Exception:
                under_pressure = False

        evicted: list[tuple[str, dict[str, Any]]] = []
        now = self._clock()
        with self._lock:
            candidates = sorted(
                (
                    (runtime.last_used, session_id)
                    for session_id, runtime in self._runtimes.items()
                    if runtime.state is RuntimeState.HOT_IDLE
                    and not runtime.resource_leases
                ),
                key=lambda item: (item[0], item[1]),
            )
            expired = {
                session_id
                for last_used, session_id in candidates
                if self.policy.idle_ttl_s is not None
                and now - last_used >= self.policy.idle_ttl_s
            }
            selected = [
                session_id for _last_used, session_id in candidates if session_id in expired
            ]
            if under_pressure:
                remaining = [
                    session_id
                    for _last_used, session_id in candidates
                    if session_id not in expired
                ]
                selected.extend(remaining[: self.policy.rss_eviction_batch])
            for session_id in selected:
                resources = self._hibernate_locked(session_id)
                if resources is not None:
                    evicted.append((session_id, resources))
        for session_id, resources in evicted:
            self._cleanup(session_id, resources)
        return [session_id for session_id, _resources in evicted]

    def _enforce_hot_idle_cap(self) -> list[str]:
        evicted: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            candidates = sorted(
                (
                    (runtime.last_used, session_id)
                    for session_id, runtime in self._runtimes.items()
                    if runtime.state is RuntimeState.HOT_IDLE
                    and not runtime.resource_leases
                ),
                key=lambda item: (item[0], item[1]),
            )
            excess = max(0, self.hot_idle_count - self.policy.max_hot_idle)
            for _last_used, session_id in candidates[:excess]:
                resources = self._hibernate_locked(session_id)
                if resources is not None:
                    evicted.append((session_id, resources))
        for session_id, resources in evicted:
            self._cleanup(session_id, resources)
        return [session_id for session_id, _resources in evicted]

    def discard(self, session_id: str) -> bool:
        """Fence a logical runtime for external terminal teardown.

        Unlike ``close()``, this leaves resources on the caller-owned session
        mapping and does not invoke cleanup.  The gateway can therefore run its
        established finalize/agent.close path while late build and execution
        leases are invalidated immediately.
        """
        with self._lock:
            runtime = self._runtimes[session_id]
            if runtime.state is RuntimeState.CLOSED:
                return False
            if runtime.build_lease is not None:
                runtime.build_lease._mark_released()
            if runtime.execution_lease is not None:
                runtime.execution_lease._mark_released()
            for lease in runtime.resource_leases:
                lease._mark_released()
            runtime.build_lease = None
            runtime.execution_lease = None
            runtime.resource_leases.clear()
            runtime.generation += 1
            runtime.state = RuntimeState.CLOSED
            runtime.last_used = self._clock()
            self.sessions.pop(session_id, None)
            return True

    def close(self, session_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes[session_id]
            if runtime.state is RuntimeState.CLOSED:
                return False
            if (
                runtime.state in {RuntimeState.BUILDING, RuntimeState.EXECUTING}
                or runtime.resource_leases
            ):
                return False
            resources = {
                key: runtime.session[key]
                for key in runtime.attached_keys
                if key in runtime.session
            }
            for key in runtime.attached_keys:
                runtime.session.pop(key, None)
            runtime.attached_keys.clear()
            runtime.generation += 1
            runtime.state = RuntimeState.CLOSED
            runtime.last_used = self._clock()
            self.sessions.pop(session_id, None)
        self._cleanup(session_id, resources)
        return True

    def _release(self, lease: RuntimeLease) -> bool:
        enforce_hot_cap = False
        released = False
        with self._lock:
            runtime = self._runtimes.get(lease.session_id)
            if runtime is None or runtime.generation != lease.generation:
                lease._mark_released()
                return False
            if lease.kind == "build" and runtime.build_lease is lease:
                runtime.build_lease = None
                runtime.state = RuntimeState.COLD
                runtime.last_used = self._clock()
                lease._mark_released()
                released = True
            elif lease.kind == "execute" and runtime.execution_lease is lease:
                runtime.execution_lease = None
                runtime.state = RuntimeState.HOT_IDLE
                runtime.last_used = self._clock()
                lease._mark_released()
                released = True
                enforce_hot_cap = True
            elif lease.kind == "resource" and lease in runtime.resource_leases:
                runtime.resource_leases.remove(lease)
                runtime.last_used = self._clock()
                lease._mark_released()
                released = True
                enforce_hot_cap = not runtime.resource_leases
            else:
                lease._mark_released()
                return False
        if enforce_hot_cap:
            self._enforce_hot_idle_cap()
        return released

    @property
    def hot_idle_count(self) -> int:
        with self._lock:
            return sum(runtime.state is RuntimeState.HOT_IDLE for runtime in self._runtimes.values())

    @property
    def executing_count(self) -> int:
        with self._lock:
            return sum(runtime.state is RuntimeState.EXECUTING for runtime in self._runtimes.values())
