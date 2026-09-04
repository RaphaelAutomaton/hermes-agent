from __future__ import annotations

import threading
import time
from typing import Callable

import pytest

from tui_gateway.session_event_hub import SubscriptionHub


class RecordingSink:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._condition = threading.Condition()

    def write(self, frame: dict) -> bool:
        with self._condition:
            self.frames.append(frame)
            self._condition.notify_all()
        return True

    def wait_for(self, count: int, timeout: float = 1.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.frames) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return list(self.frames)


class FailedSink(RecordingSink):
    def write(self, frame: dict) -> bool:
        super().write(frame)
        return False


class BlockingSink(RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, frame: dict) -> bool:
        self.entered.set()
        self.release.wait(2.0)
        return super().write(frame)


def _params(frame: dict) -> dict:
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "event"
    return frame["params"]


def _event(kind: str, value: int = 0) -> dict:
    return {"type": kind, "session_id": "live-tip", "payload": {"value": value}}


def test_two_sinks_receive_the_same_monotonic_conversation_sequence() -> None:
    hub = SubscriptionHub()
    first = RecordingSink()
    second = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", first)
        hub.watch("profile-a", "conversation-1", second)

        assert hub.publish("profile-a", "conversation-1", _event("start")) == 1
        assert hub.publish("profile-a", "conversation-1", _event("delta")) == 2

        first_params = [_params(frame) for frame in first.wait_for(2)]
        second_params = [_params(frame) for frame in second.wait_for(2)]
        assert [item["seq"] for item in first_params] == [1, 2]
        assert [item["seq"] for item in second_params] == [1, 2]
        assert first_params == second_params
        assert all(item["conversation_id"] == "conversation-1" for item in first_params)
        assert all(item["event_stream_id"] == hub.event_stream_id for item in first_params)
    finally:
        hub.close()


def test_unwatch_removes_only_the_requested_sink() -> None:
    hub = SubscriptionHub()
    first = RecordingSink()
    second = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", first)
        hub.watch("profile-a", "conversation-1", second)

        assert hub.unwatch("profile-a", "conversation-1", first) is True
        assert hub.unwatch("profile-a", "conversation-1", first) is False
        hub.publish("profile-a", "conversation-1", _event("delta"))

        assert first.wait_for(1, timeout=0.05) == []
        assert len(second.wait_for(1)) == 1
    finally:
        hub.close()


def test_disconnect_removes_all_subscriptions_for_only_that_sink() -> None:
    hub = SubscriptionHub()
    first = RecordingSink()
    second = RecordingSink()
    try:
        for conversation_id in ("conversation-1", "conversation-2"):
            hub.watch("profile-a", conversation_id, first)
            hub.watch("profile-a", conversation_id, second)

        assert hub.disconnect(first) == 2
        hub.publish("profile-a", "conversation-1", _event("one"))
        hub.publish("profile-a", "conversation-2", _event("two"))

        assert first.wait_for(1, timeout=0.05) == []
        assert len(second.wait_for(2)) == 2
    finally:
        hub.close()


def test_duplicate_watch_is_idempotent() -> None:
    hub = SubscriptionHub()
    sink = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", sink)
        hub.watch("profile-a", "conversation-1", sink)
        hub.publish("profile-a", "conversation-1", _event("delta"))

        assert len(sink.wait_for(1)) == 1
        time.sleep(0.02)
        assert len(sink.frames) == 1
    finally:
        hub.close()


def test_publish_without_watchers_uses_v1_fallback_sink() -> None:
    hub = SubscriptionHub()
    fallback = RecordingSink()
    try:
        hub.publish(
            "profile-a",
            "conversation-1",
            _event("delta"),
            fallback_sink=fallback,
        )

        assert len(fallback.wait_for(1)) == 1
    finally:
        hub.close()


def test_fallback_that_also_watches_receives_exactly_one_copy() -> None:
    hub = SubscriptionHub()
    sink = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", sink)
        hub.publish(
            "profile-a",
            "conversation-1",
            _event("delta"),
            fallback_sink=sink,
        )

        assert len(sink.wait_for(1)) == 1
        time.sleep(0.02)
        assert len(sink.frames) == 1
    finally:
        hub.close()


def test_cursor_inside_ring_replays_in_sequence_order() -> None:
    hub = SubscriptionHub(replay_max_events=4)
    sink = RecordingSink()
    try:
        hub.publish("profile-a", "conversation-1", _event("one", 1))
        hub.publish("profile-a", "conversation-1", _event("two", 2))

        result = hub.watch(
            "profile-a",
            "conversation-1",
            sink,
            cursor={"event_stream_id": hub.event_stream_id, "after_seq": 0},
        )

        assert result == {
            "event_stream_id": hub.event_stream_id,
            "current_seq": 2,
            "replayed": 2,
            "resync_required": False,
            "watching": True,
        }
        assert [_params(frame)["seq"] for frame in sink.wait_for(2)] == [1, 2]
    finally:
        hub.close()


@pytest.mark.parametrize(
    ("cursor_factory", "reason"),
    [
        (lambda hub: {"event_stream_id": "old-epoch", "after_seq": 3}, "stream_reset"),
        (lambda hub: {"event_stream_id": hub.event_stream_id, "after_seq": 0}, "cursor_expired"),
        (lambda hub: {"event_stream_id": hub.event_stream_id, "after_seq": 4}, "cursor_ahead"),
    ],
)
def test_invalid_cursor_requires_resync_without_activating_watch(
    cursor_factory: Callable[[SubscriptionHub], dict],
    reason: str,
) -> None:
    hub = SubscriptionHub(replay_max_events=2)
    sink = RecordingSink()
    try:
        for value in range(1, 4):
            hub.publish("profile-a", "conversation-1", _event("delta", value))

        result = hub.watch(
            "profile-a",
            "conversation-1",
            sink,
            cursor=cursor_factory(hub),
        )

        assert result == {
            "event_stream_id": hub.event_stream_id,
            "current_seq": 3,
            "replayed": 0,
            "resync_required": True,
            "reason": reason,
            "watching": False,
        }
        hub.publish("profile-a", "conversation-1", _event("later", 4))
        assert sink.wait_for(1, timeout=0.05) == []
    finally:
        hub.close()


def test_same_conversation_id_in_different_scopes_does_not_cross_events() -> None:
    hub = SubscriptionHub()
    profile_a = RecordingSink()
    profile_b = RecordingSink()
    try:
        hub.watch("profile-a", "same-id", profile_a)
        hub.watch("profile-b", "same-id", profile_b)

        assert hub.publish("profile-a", "same-id", _event("only-a")) == 1

        assert len(profile_a.wait_for(1)) == 1
        assert profile_b.wait_for(1, timeout=0.05) == []
    finally:
        hub.close()


def test_sink_returning_false_is_disconnected_without_affecting_others() -> None:
    hub = SubscriptionHub()
    failed = FailedSink()
    healthy = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", failed)
        hub.watch("profile-a", "conversation-1", healthy)

        hub.publish("profile-a", "conversation-1", _event("first"))
        assert len(failed.wait_for(1)) == 1
        assert len(healthy.wait_for(1)) == 1

        hub.publish("profile-a", "conversation-1", _event("second"))
        assert len(healthy.wait_for(2)) == 2
        time.sleep(0.02)
        assert len(failed.frames) == 1
    finally:
        hub.close()


def test_slow_sink_does_not_block_publish_or_another_sink() -> None:
    hub = SubscriptionHub()
    slow = BlockingSink()
    fast = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", slow)
        hub.watch("profile-a", "conversation-1", fast)

        started = time.monotonic()
        hub.publish("profile-a", "conversation-1", _event("delta"))
        elapsed = time.monotonic() - started

        assert elapsed < 0.1
        assert slow.entered.wait(0.5)
        assert len(fast.wait_for(1, timeout=0.5)) == 1
    finally:
        slow.release.set()
        hub.close()


def test_queue_overflow_emits_one_gap_only_to_the_slow_sink() -> None:
    hub = SubscriptionHub(queue_max_events=1)
    slow = BlockingSink()
    fast = RecordingSink()
    try:
        hub.watch("profile-a", "conversation-1", slow)
        hub.watch("profile-a", "conversation-1", fast)

        hub.publish("profile-a", "conversation-1", _event("one"))
        assert slow.entered.wait(0.5)
        assert len(fast.wait_for(1)) == 1

        hub.publish("profile-a", "conversation-1", _event("two"))
        assert len(fast.wait_for(2)) == 2
        hub.publish("profile-a", "conversation-1", _event("three"))
        assert len(fast.wait_for(3)) == 3

        slow.release.set()
        slow_frames = slow.wait_for(2)
        assert [_params(frame)["type"] for frame in slow_frames] == ["one", "session.gap"]
        gap = _params(slow_frames[1])
        assert gap["payload"] == {"reason": "queue_overflow", "current_seq": 3}

        hub.publish("profile-a", "conversation-1", _event("four"))
        assert len(fast.wait_for(4)) == 4
        time.sleep(0.02)
        assert len(slow.frames) == 2
    finally:
        slow.release.set()
        hub.close()


def test_replay_ring_respects_byte_limit() -> None:
    hub = SubscriptionHub(replay_max_events=10, replay_max_bytes=1)
    sink = RecordingSink()
    try:
        hub.publish("profile-a", "conversation-1", _event("too-large"))

        result = hub.watch(
            "profile-a",
            "conversation-1",
            sink,
            cursor={"event_stream_id": hub.event_stream_id, "after_seq": 0},
        )

        assert result["resync_required"] is True
        assert result["reason"] == "cursor_expired"
        assert result["watching"] is False
    finally:
        hub.close()
