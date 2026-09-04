"""Conversation-scoped event subscriptions for the TUI gateway.

The hub owns ordering and replay state, while transports remain external
resources. Every transport gets an independent serial drain so a blocked peer
cannot stall event publication or another peer.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict, deque
from typing import Any, Callable


_StreamKey = tuple[Any, str]


class _SinkWorker:
    """Single-writer bounded drain for one sink."""

    def __init__(
        self,
        sink: Any,
        on_failure: Callable[[Any], None],
        max_events: int,
    ) -> None:
        self.sink = sink
        self._on_failure = on_failure
        self._max_events = max_events
        self._condition = threading.Condition()
        self._queue: deque[tuple[_StreamKey, dict[str, Any]]] = deque()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._drain,
            name=f"subscription-sink-{id(sink):x}",
            daemon=True,
        )
        self._thread.start()

    def put(self, key: _StreamKey, frame: dict[str, Any]) -> bool:
        with self._condition:
            if self._stopped or len(self._queue) >= self._max_events:
                return False
            self._queue.append((key, frame))
            self._condition.notify()
            return True

    def replace_with(self, key: _StreamKey, frame: dict[str, Any]) -> None:
        """Discard stale backlog and retain one mandatory control frame."""
        with self._condition:
            if self._stopped:
                return
            self._queue.clear()
            self._queue.append((key, frame))
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._queue.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _drain(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                _key, frame = self._queue.popleft()
            try:
                if self.sink.write(frame) is False:
                    self._on_failure(self.sink)
                    return
            except Exception:
                self._on_failure(self.sink)
                return


class SubscriptionHub:
    """Fan out ordered session events to transports watching a conversation."""

    def __init__(
        self,
        *,
        replay_max_events: int = 256,
        replay_max_bytes: int = 2 * 1024 * 1024,
        queue_max_events: int = 256,
    ) -> None:
        if replay_max_events <= 0:
            raise ValueError("replay_max_events must be positive")
        if replay_max_bytes <= 0:
            raise ValueError("replay_max_bytes must be positive")
        if queue_max_events <= 0:
            raise ValueError("queue_max_events must be positive")
        self.event_stream_id = str(uuid.uuid4())
        self._replay_max_events = replay_max_events
        self._replay_max_bytes = replay_max_bytes
        self._queue_max_events = queue_max_events
        self._lock = threading.Lock()
        self._watchers: dict[_StreamKey, dict[int, Any]] = defaultdict(dict)
        self._sequences: dict[_StreamKey, int] = defaultdict(int)
        self._rings: dict[_StreamKey, deque[dict[str, Any]]] = defaultdict(deque)
        self._ring_bytes: dict[_StreamKey, int] = defaultdict(int)
        self._workers: dict[int, _SinkWorker] = {}
        self._gapped: set[tuple[int, _StreamKey]] = set()
        self._closed = False

    def watch(
        self,
        scope: Any,
        conversation_id: str,
        sink: Any,
        cursor: Any = None,
    ) -> dict[str, Any]:
        key = (scope, conversation_id)
        with self._lock:
            self._ensure_open()
            current_seq = self._sequences[key]
            watchers = self._watchers.get(key)
            already_watching = bool(
                watchers is not None and watchers.get(id(sink)) is sink
            )
            replay: list[dict[str, Any]] = []
            if cursor is not None and not already_watching:
                after_seq = int(cursor["after_seq"])
                reason = self._cursor_error(key, cursor, after_seq, current_seq)
                if reason is not None:
                    return self._watch_result(
                        current_seq=current_seq,
                        replayed=0,
                        watching=False,
                        reason=reason,
                    )
                replay = [
                    frame
                    for frame in self._rings.get(key, ())
                    if frame["params"]["seq"] > after_seq
                ]

            worker = self._worker_for_locked(sink)
            if not already_watching:
                # Hub-lock serialization guarantees live events are enqueued
                # strictly after every replay frame.
                for frame in replay:
                    if not worker.put(key, frame):
                        self._mark_gapped_locked(sink, key, worker, current_seq)
                        return self._watch_result(
                            current_seq=current_seq,
                            replayed=0,
                            watching=False,
                            reason="queue_overflow",
                        )
                self._watchers[key][id(sink)] = sink
                self._gapped.discard((id(sink), key))

            return self._watch_result(
                current_seq=current_seq,
                replayed=len(replay),
                watching=True,
            )

    def is_watching(self, scope: Any, conversation_id: str, sink: Any) -> bool:
        key = (scope, conversation_id)
        with self._lock:
            watchers = self._watchers.get(key)
            return bool(watchers is not None and watchers.get(id(sink)) is sink)

    def unwatch(self, scope: Any, conversation_id: str, sink: Any) -> bool:
        """Stop one sink watching one conversation; repeated calls are harmless."""
        key = (scope, conversation_id)
        with self._lock:
            watchers = self._watchers.get(key)
            if not watchers or watchers.get(id(sink)) is not sink:
                self._gapped.discard((id(sink), key))
                return False
            del watchers[id(sink)]
            if not watchers:
                self._watchers.pop(key, None)
            self._gapped.discard((id(sink), key))
            return True

    def disconnect(self, sink: Any) -> int:
        """Remove every subscription owned by a disconnected sink."""
        sink_id = id(sink)
        with self._lock:
            removed = self._disconnect_locked(sink)
            worker = self._workers.pop(sink_id, None)
        if worker is not None:
            worker.stop()
        return removed

    def publish(
        self,
        scope: Any,
        conversation_id: str,
        event: dict[str, Any],
        fallback_sink: Any = None,
        exclude_sink: Any = None,
        synchronous_sink: Any = None,
    ) -> int:
        key = (scope, conversation_id)
        with self._lock:
            self._ensure_open()
            sequence = self._sequences[key] + 1
            self._sequences[key] = sequence
            frame = self._frame(conversation_id, sequence, event)
            self._rings[key].append(frame)
            self._ring_bytes[key] += self._frame_size(frame)
            while (
                len(self._rings[key]) > self._replay_max_events
                or self._ring_bytes[key] > self._replay_max_bytes
            ):
                removed = self._rings[key].popleft()
                self._ring_bytes[key] -= self._frame_size(removed)

            sinks = [
                sink
                for sink in self._watchers.get(key, {}).values()
                if sink is not exclude_sink and sink is not synchronous_sink
            ]
            if (
                fallback_sink is not None
                and fallback_sink is not exclude_sink
                and fallback_sink is not synchronous_sink
                and all(candidate is not fallback_sink for candidate in sinks)
                and (id(fallback_sink), key) not in self._gapped
            ):
                sinks.append(fallback_sink)
            for sink in sinks:
                worker = self._worker_for_locked(sink)
                if not worker.put(key, frame):
                    self._mark_gapped_locked(sink, key, worker, sequence)
        if synchronous_sink is not None:
            try:
                if synchronous_sink.write(frame) is False:
                    self.disconnect(synchronous_sink)
            except Exception:
                self.disconnect(synchronous_sink)
        return sequence

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._watchers.clear()
            self._gapped.clear()
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()

    def _worker_for_locked(self, sink: Any) -> _SinkWorker:
        sink_id = id(sink)
        worker = self._workers.get(sink_id)
        if worker is None or worker.sink is not sink:
            worker = _SinkWorker(
                sink,
                self._sink_failed,
                self._queue_max_events,
            )
            self._workers[sink_id] = worker
        return worker

    def _sink_failed(self, sink: Any) -> None:
        self.disconnect(sink)

    def _disconnect_locked(self, sink: Any) -> int:
        removed = 0
        sink_id = id(sink)
        for key, watchers in list(self._watchers.items()):
            if watchers.get(sink_id) is sink:
                del watchers[sink_id]
                removed += 1
            if not watchers:
                del self._watchers[key]
        self._gapped = {item for item in self._gapped if item[0] != sink_id}
        return removed

    def _mark_gapped_locked(
        self,
        sink: Any,
        key: _StreamKey,
        worker: _SinkWorker,
        current_seq: int,
    ) -> None:
        sink_id = id(sink)
        marker = (sink_id, key)
        if marker in self._gapped:
            return
        self._gapped.add(marker)
        watchers = self._watchers.get(key)
        if watchers is not None and watchers.get(sink_id) is sink:
            del watchers[sink_id]
            if not watchers:
                self._watchers.pop(key, None)
        worker.replace_with(key, self._gap_frame(key[1], current_seq))

    def _gap_frame(self, conversation_id: str, current_seq: int) -> dict[str, Any]:
        return self._frame(
            conversation_id,
            current_seq,
            {
                "type": "session.gap",
                "session_id": "",
                "payload": {
                    "reason": "queue_overflow",
                    "current_seq": current_seq,
                },
            },
        )

    def _cursor_error(
        self,
        key: _StreamKey,
        cursor: Any,
        after_seq: int,
        current_seq: int,
    ) -> str | None:
        if cursor.get("event_stream_id") != self.event_stream_id:
            return "stream_reset"
        if after_seq > current_seq:
            return "cursor_ahead"
        ring = self._rings.get(key, ())
        earliest_seq = ring[0]["params"]["seq"] if ring else None
        if after_seq < current_seq and (
            earliest_seq is None or after_seq < earliest_seq - 1
        ):
            return "cursor_expired"
        return None

    def _watch_result(
        self,
        *,
        current_seq: int,
        replayed: int,
        watching: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_stream_id": self.event_stream_id,
            "current_seq": current_seq,
            "replayed": replayed,
            "resync_required": reason is not None,
            "watching": watching,
        }
        if reason is not None:
            result["reason"] = reason
        return result

    def _frame(
        self,
        conversation_id: str,
        sequence: int,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        if event.get("method") == "event" and isinstance(event.get("params"), dict):
            frame = dict(event)
            params = dict(event["params"])
            frame["params"] = params
            frame.setdefault("jsonrpc", "2.0")
        else:
            params = dict(event)
            frame = {"jsonrpc": "2.0", "method": "event", "params": params}
        params.update(
            conversation_id=conversation_id,
            event_stream_id=self.event_stream_id,
            seq=sequence,
        )
        return frame

    @staticmethod
    def _frame_size(frame: dict[str, Any]) -> int:
        return len(
            json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("subscription hub is closed")
