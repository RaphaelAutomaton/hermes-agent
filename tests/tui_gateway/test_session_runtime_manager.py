"""State-machine tests for the isolated session runtime pool."""

from __future__ import annotations

from threading import Event, Thread

import pytest

from tui_gateway.session_runtime import (
    ExecutionCapacityExceeded,
    RuntimePolicy,
    RuntimeState,
    RuntimeClosed,
    SessionRuntimeManager,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 1.0) -> None:
        self.now += seconds


def _make_hot(manager: SessionRuntimeManager, session_id: str, **session_data):
    session = manager.register(session_id, dict(session_data))
    build = manager.acquire_build(session_id)
    manager.finish_build(session_id, build.generation, agent=f"agent-{session_id}")
    return session


def test_registered_cold_runtime_consumes_no_hot_slot():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=1, max_executing=1))

    session = manager.register("cold")

    assert session == {}
    assert manager.state("cold") is RuntimeState.COLD
    assert manager.hot_idle_count == 0


def test_completed_build_transitions_to_hot_idle():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=1, max_executing=1))
    session = manager.register("session")
    agent = object()

    build = manager.acquire_build("session")
    assert manager.state("session") is RuntimeState.BUILDING

    assert manager.finish_build("session", build.generation, agent=agent) is True
    assert session["agent"] is agent
    assert manager.state("session") is RuntimeState.HOT_IDLE
    assert manager.hot_idle_count == 1


def test_execution_lease_transitions_hot_idle_to_executing_and_back():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=1, max_executing=1))
    manager.register("session")
    build = manager.acquire_build("session")
    manager.finish_build("session", build.generation, agent=object())

    execution = manager.acquire_execution("session")
    assert manager.state("session") is RuntimeState.EXECUTING
    assert manager.executing_count == 1

    assert execution.release() is True
    assert execution.release() is False
    assert manager.state("session") is RuntimeState.HOT_IDLE
    assert manager.executing_count == 0


def test_build_can_handoff_atomically_to_execution_when_hot_idle_cap_is_zero():
    cleaned = []
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=0, max_executing=1),
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources)),
    )
    session = manager.register("session")
    agent = object()
    build = manager.acquire_build("session")

    execution = manager.finish_build_and_acquire_execution(
        "session", build.generation, agent=agent
    )

    assert execution is not None
    assert manager.state("session") is RuntimeState.EXECUTING
    assert session["agent"] is agent
    assert cleaned == []


def test_max_executing_rejects_n_plus_one_without_mutating_runtime():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=2, max_executing=1))
    for session_id in ("first", "second"):
        manager.register(session_id)
        build = manager.acquire_build(session_id)
        manager.finish_build(session_id, build.generation, agent=object())

    first = manager.acquire_execution("first")
    with pytest.raises(ExecutionCapacityExceeded):
        manager.acquire_execution("second")

    assert manager.state("second") is RuntimeState.HOT_IDLE
    assert manager.executing_count == 1
    first.release()


def test_execution_release_reenforces_hot_idle_cap():
    clock = FakeClock()
    cleaned = []
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=1, max_executing=2),
        clock=clock,
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources)),
    )
    for session_id in ("first", "second"):
        manager.register(session_id)
        build = manager.acquire_build(session_id)
        execution = manager.finish_build_and_acquire_execution(
            session_id, build.generation, agent=f"agent-{session_id}"
        )
        assert execution is not None
        manager.sessions[session_id]["execution"] = execution
        clock.advance()

    manager.sessions["first"]["execution"].release()
    manager.sessions["second"]["execution"].release()

    assert manager.hot_idle_count == 1
    assert manager.state("first") is RuntimeState.COLD
    assert manager.state("second") is RuntimeState.HOT_IDLE
    assert [session_id for session_id, _ in cleaned] == ["first"]


def test_resource_lease_pins_runtime_without_consuming_execution_slot():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=1, max_executing=1))
    manager.register("session")
    build = manager.acquire_build("session")
    manager.finish_build("session", build.generation, agent=object())

    resource = manager.acquire_resource("session")

    assert manager.state("session") is RuntimeState.HOT_IDLE
    assert manager.executing_count == 0
    assert manager.is_pinned("session") is True
    assert resource.release() is True
    assert manager.is_pinned("session") is False


def test_generation_fenced_resource_attach_is_detached_by_hibernation():
    cleaned = []
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=1, max_executing=1),
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources)),
    )
    session = manager.register("session")
    build = manager.acquire_build("session")
    manager.finish_build("session", build.generation, agent="agent")
    poller = object()

    assert manager.attach_resources(
        "session", build.generation, notification_poller=poller
    ) is True
    assert manager.attach_resources(
        "session", build.generation - 1, stale="value"
    ) is False
    assert manager.hibernate("session") is True

    assert "notification_poller" not in session
    assert "stale" not in session
    assert cleaned == [
        (
            "session",
            {"agent": "agent", "notification_poller": poller},
        )
    ]


def test_hibernate_refuses_building_executing_or_resource_leased_runtime():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=3, max_executing=1))

    manager.register("building")
    manager.acquire_build("building")

    for session_id in ("executing", "resource"):
        manager.register(session_id)
        build = manager.acquire_build(session_id)
        manager.finish_build(session_id, build.generation, agent=object())
    manager.acquire_execution("executing")
    manager.acquire_resource("resource")

    assert manager.hibernate("building") is False
    assert manager.hibernate("executing") is False
    assert manager.hibernate("resource") is False
    assert manager.state("building") is RuntimeState.BUILDING
    assert manager.state("executing") is RuntimeState.EXECUTING
    assert manager.state("resource") is RuntimeState.HOT_IDLE


def test_stale_generation_cannot_attach_agent_or_release_new_build_lease():
    manager = SessionRuntimeManager(RuntimePolicy(max_hot_idle=1, max_executing=1))
    session = manager.register("session")

    stale_build = manager.acquire_build("session")
    manager.finish_build("session", stale_build.generation, agent="old-agent")
    assert manager.hibernate("session") is True
    assert "agent" not in session

    current_build = manager.acquire_build("session")
    assert manager.finish_build(
        "session", stale_build.generation, agent="stale-agent"
    ) is False
    assert stale_build.release() is False
    assert "agent" not in session
    assert manager.state("session") is RuntimeState.BUILDING

    assert manager.finish_build(
        "session", current_build.generation, agent="current-agent"
    ) is True
    assert session["agent"] == "current-agent"


def test_discard_fences_late_build_without_running_cleanup():
    cleaned = []
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=1, max_executing=1),
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources)),
    )
    session = manager.register("session", {"history": ["durable"]})
    build = manager.acquire_build("session")

    assert manager.discard("session") is True
    assert manager.state("session") is RuntimeState.CLOSED
    assert "session" not in manager.sessions
    assert session["history"] == ["durable"]
    assert cleaned == []
    assert manager.finish_build(
        "session", build.generation, agent="late-agent"
    ) is False


def test_hot_idle_cap_hibernates_oldest_runtime_even_with_session_handle():
    clock = FakeClock()
    cleaned = []
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=1, max_executing=1),
        clock=clock,
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources)),
    )
    first = _make_hot(manager, "first", transport=object())
    clock.advance()
    _make_hot(manager, "second", transport=object())

    assert manager.state("first") is RuntimeState.COLD
    assert manager.state("second") is RuntimeState.HOT_IDLE
    assert "agent" not in first
    assert "transport" in first
    assert cleaned == [("first", {"agent": "agent-first"})]


def test_sweep_hibernates_only_hot_idle_runtimes_past_ttl():
    clock = FakeClock()
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=10, max_executing=2, idle_ttl_s=5),
        clock=clock,
    )
    _make_hot(manager, "expired")
    clock.advance(3)
    _make_hot(manager, "fresh")
    executing = manager.acquire_execution("fresh")
    clock.advance(3)

    assert manager.sweep() == ["expired"]
    assert manager.state("expired") is RuntimeState.COLD
    assert manager.state("fresh") is RuntimeState.EXECUTING
    executing.release()


def test_rss_soft_limit_hibernates_a_bounded_lru_batch():
    clock = FakeClock()
    manager = SessionRuntimeManager(
        RuntimePolicy(
            max_hot_idle=10,
            max_executing=1,
            idle_ttl_s=100,
            rss_soft_limit_bytes=100,
            rss_eviction_batch=2,
        ),
        clock=clock,
        rss_probe=lambda: 101,
    )
    for session_id in ("oldest", "middle", "newest"):
        _make_hot(manager, session_id)
        clock.advance()

    assert manager.sweep() == ["oldest", "middle"]
    assert manager.state("newest") is RuntimeState.HOT_IDLE


def test_rss_probe_failure_is_fail_open():
    manager = SessionRuntimeManager(
        RuntimePolicy(rss_soft_limit_bytes=100, idle_ttl_s=None),
        rss_probe=lambda: (_ for _ in ()).throw(OSError("probe unavailable")),
    )
    _make_hot(manager, "session")

    assert manager.sweep() == []
    assert manager.state("session") is RuntimeState.HOT_IDLE


def test_cleanup_runs_after_manager_lock_is_released():
    callback_entered = Event()
    observer_finished = Event()

    def cleanup(session_id, resources):
        callback_entered.set()
        observer = Thread(
            target=lambda: (manager.state(session_id), observer_finished.set()),
            daemon=True,
        )
        observer.start()
        observer.join(timeout=1)
        assert observer_finished.is_set(), "cleanup callback ran while manager lock was held"

    manager = SessionRuntimeManager(cleanup=cleanup)
    _make_hot(manager, "session")

    assert manager.hibernate("session") is True
    assert callback_entered.is_set()


def test_close_is_terminal_and_cleans_materialized_resources():
    cleaned = []
    manager = SessionRuntimeManager(
        cleanup=lambda session_id, resources: cleaned.append((session_id, resources))
    )
    session = _make_hot(manager, "session", transport=object())

    assert manager.close("session") is True
    assert manager.state("session") is RuntimeState.CLOSED
    assert "agent" not in session
    assert cleaned == [("session", {"agent": "agent-session"})]
    with pytest.raises(RuntimeClosed):
        manager.register("session", {})
