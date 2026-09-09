"""Protocol-v2 capability negotiation and cold session.read RPC."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from tui_gateway.protocol import gateway_ready_payload


_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


def test_ready_payload_negotiates_only_implemented_v2_capabilities():
    payload = gateway_ready_payload("default")

    assert payload["skin"] == "default"
    assert payload["protocol_version"] == 2
    runtime = payload["capabilities"]["session_runtime"]
    assert runtime == {
        "version": 2,
        "cold_read": True,
        "history_keyset_pagination": True,
        "stable_snapshot": True,
        "durable_turns": True,
        "subscriptions": True,
        "runtime_pool": True,
        "session_model_controls": True,
        "skill_invocation": True,
        "explicit_queue": True,
    }


def test_session_read_rpc_is_cold_and_preserves_reasoning(server, tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("cold", "cli")
        db.append_message("cold", "user", "question")
        db.append_message(
            "cold",
            "assistant",
            "answer",
            reasoning="private chain summary",
            reasoning_details=[{"type": "summary_text", "text": "visible reasoning"}],
        )
        monkeypatch.setattr(server, "_get_db", lambda: db)
        before = dict(server._sessions)

        response = server.handle_request(
            {
                "id": "read-1",
                "method": "session.read",
                "params": {"conversation_id": "cold", "limit": 1, "view": "dialog"},
            }
        )

        assert "error" not in response
        result = response["result"]
        assert result["messages"][0]["content"] == "answer"
        assert result["messages"][0]["reasoning"] == "private chain summary"
        assert result["messages"][0]["reasoning_details"][0]["text"] == "visible reasoning"
        assert result["has_more"] is True
        assert server._sessions == before
        assert "session.read" in server._LONG_HANDLERS
    finally:
        db.close()


def test_session_read_rpc_validates_missing_unknown_and_bad_cursor(server, tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("known", "cli")
        db.append_message("known", "user", "hello")
        monkeypatch.setattr(server, "_get_db", lambda: db)

        missing = server.handle_request({"id": "a", "method": "session.read", "params": {}})
        unknown = server.handle_request(
            {"id": "b", "method": "session.read", "params": {"conversation_id": "unknown"}}
        )
        bad_cursor = server.handle_request(
            {
                "id": "c",
                "method": "session.read",
                "params": {"conversation_id": "known", "cursor": "bad"},
            }
        )

        assert missing["error"]["code"] == 4006
        assert unknown["error"]["code"] == 4007
        assert bad_cursor["error"]["code"] == 4008
    finally:
        db.close()


def test_turn_submit_commits_receipt_before_scheduler_and_status_is_cold(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    scheduled = []
    client_turn_id = "12345678-1234-4234-8234-123456789abc"

    def capture_schedule(callback):
        row = db.get_turn_receipt_by_client_id("conversation", client_turn_id)
        assert row is not None
        assert row["state"] == "ACCEPTED"
        assert server._sessions == {}
        scheduled.append(callback)

    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server, "_schedule_durable_turn", capture_schedule, raising=False
        )
        submitted = server.handle_request(
            {
                "id": "turn-1",
                "method": "turn.submit",
                "params": {
                    "conversation_id": "conversation",
                    "client_turn_id": client_turn_id,
                    "text": "hello",
                },
            }
        )

        assert "error" not in submitted
        assert submitted["result"]["state"] == "ACCEPTED"
        assert submitted["result"]["deduplicated"] is False
        assert len(scheduled) == 1
        assert server._sessions == {}

        status = server.handle_request(
            {
                "id": "turn-status-1",
                "method": "turn.status",
                "params": {"turn_id": submitted["result"]["turn_id"]},
            }
        )
        assert status["result"]["state"] == "ACCEPTED"
        assert server._sessions == {}
    finally:
        db.close()


def test_turn_submit_is_idempotent_and_rejects_hash_conflict(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    scheduled = []
    client_turn_id = "87654321-4321-4321-8321-cba987654321"
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server,
            "_schedule_durable_turn",
            lambda callback: scheduled.append(callback),
        )
        request = {
            "id": "first",
            "method": "turn.submit",
            "params": {
                "conversation_id": "conversation",
                "client_turn_id": client_turn_id,
                "text": "same",
            },
        }
        first = server.handle_request(request)
        request["id"] = "duplicate"
        duplicate = server.handle_request(request)
        request["id"] = "conflict"
        request["params"]["text"] = "different"
        conflict = server.handle_request(request)

        assert duplicate["result"]["turn_id"] == first["result"]["turn_id"]
        assert duplicate["result"]["deduplicated"] is True
        assert conflict["error"]["code"] == 4091
        assert len(scheduled) == 2
    finally:
        db.close()


def test_turn_submit_marks_receipt_failed_when_thread_scheduling_fails(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server,
            "_schedule_durable_turn",
            lambda callback: (_ for _ in ()).throw(RuntimeError("no threads")),
        )
        response = server.handle_request(
            {
                "id": "dispatch-failure",
                "method": "turn.submit",
                "params": {
                    "conversation_id": "conversation",
                    "client_turn_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "text": "hello",
                },
            }
        )

        assert "error" not in response
        assert response["result"]["state"] == "FAILED"
        assert response["result"]["error_code"] == "DISPATCH_FAILED"
        persisted = db.get_turn_receipt(response["result"]["turn_id"])
        assert persisted["state"] == "FAILED"
    finally:
        db.close()


def test_turn_submit_recovers_dead_starting_writer_before_retry(
    server, tmp_path, monkeypatch
):
    import uuid

    from tui_gateway.turn_ledger import TurnLedger

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    old = TurnLedger(
        db,
        writer_id="dead-gateway",
        pid=999_999_999,
        process_started_at=1.0,
    )
    accepted = old.accept(
        "conversation", "conversation", str(uuid.uuid4()), "sha256:seed"
    ).receipt
    old.claim_start(accepted.turn_id)
    scheduled = []
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(server, "_schedule_durable_turn", scheduled.append)
        # Reuse the original idempotency key and its exact request hash by
        # creating it through the public hash contract.
        import hashlib
        import json

        text = "recover me"
        request_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                {"text": text, "busy_policy": "reject"},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with db._lock:
            db._conn.execute(
                "UPDATE turn_receipts SET request_hash=? WHERE turn_id=?",
                (request_hash, accepted.turn_id),
            )
            db._conn.commit()

        response = server.handle_request(
            {
                "id": "retry-dead-starting",
                "method": "turn.submit",
                "params": {
                    "conversation_id": "conversation",
                    "client_turn_id": accepted.client_turn_id,
                    "text": text,
                },
            }
        )

        assert response["result"]["state"] == "ACCEPTED"
        assert response["result"]["deduplicated"] is True
        assert len(scheduled) == 1
    finally:
        db.close()


def test_durable_runner_claims_once_and_terminal_retry_never_reschedules(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    scheduled = []
    seen_states = []
    client_turn_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def execute(receipt, fence, ledger, text, **kwargs):
        seen_states.append(ledger.get(receipt.turn_id).state.value)
        assert text == "hello"
        ledger.mark_running(fence)
        ledger.succeed(fence, tip_session_id=receipt.tip_session_id)

    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server,
            "_schedule_durable_turn",
            lambda callback: scheduled.append(callback),
        )
        monkeypatch.setattr(
            server, "_execute_durable_turn", execute, raising=False
        )
        request = {
            "id": "run",
            "method": "turn.submit",
            "params": {
                "conversation_id": "conversation",
                "client_turn_id": client_turn_id,
                "text": "hello",
            },
        }
        first = server.handle_request(request)
        assert first["result"]["state"] == "ACCEPTED"
        scheduled.pop()()

        status = server.handle_request(
            {
                "id": "status",
                "method": "turn.status",
                "params": {"turn_id": first["result"]["turn_id"]},
            }
        )
        assert seen_states == ["STARTING"]
        assert status["result"]["state"] == "SUCCEEDED"

        request["id"] = "retry"
        retry = server.handle_request(request)
        assert retry["result"]["deduplicated"] is True
        assert retry["result"]["state"] == "SUCCEEDED"
        assert scheduled == []
    finally:
        db.close()


def test_durable_turn_materializes_only_after_claim_and_hands_fence_to_runner(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    scheduled = []
    observed = []
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server,
            "_schedule_durable_turn",
            lambda callback: scheduled.append(callback),
        )
        session = server._deferred_session_record(
            "conversation",
            cols=80,
            cwd=str(tmp_path),
            history=[],
            lease=None,
        )
        server._sessions["live"] = session

        def build(session_id, value, *, execution_requested=False):
            assert execution_requested is True
            observed.append(("build", db.get_turn_receipt_by_client_id(
                "conversation", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            )["state"]))
            value["agent"] = object()
            value["agent_ready"].set()

        execution = MagicMock(released=False)
        monkeypatch.setattr(
            server, "_take_runtime_execution", lambda *_args: execution
        )

        def run_prompt(
            rid,
            session_id,
            value,
            text,
            *,
            durable_turn=None,
            runtime_execution=None,
        ):
            assert runtime_execution is execution
            ledger, receipt, fence = durable_turn
            observed.append(("run", ledger.get(receipt.turn_id).state.value, text))
            ledger.succeed(fence, tip_session_id=receipt.tip_session_id)
            with value["history_lock"]:
                value["running"] = False
            return None

        monkeypatch.setattr(server, "_start_agent_build", build)
        monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
        response = server.handle_request(
            {
                "id": "run-real-handoff",
                "method": "turn.submit",
                "params": {
                    "conversation_id": "conversation",
                    "client_turn_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "text": "execute",
                },
            }
        )
        assert response["result"]["state"] == "ACCEPTED"
        scheduled.pop()()

        persisted = db.get_turn_receipt(response["result"]["turn_id"])
        assert persisted["state"] == "SUCCEEDED"
        assert observed == [("build", "STARTING"), ("run", "RUNNING", "execute")]
    finally:
        db.close()


def test_durable_turn_persists_success_before_emitting_message_complete(
    server, tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")
    scheduled = []
    terminal_state_at_emit = []

    class Agent:
        session_id = "conversation"
        model = "test-model"

        def run_conversation(self, message, **kwargs):
            return {"final_response": "", "messages": []}

    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(
            server,
            "_schedule_durable_turn",
            lambda callback: scheduled.append(callback),
        )
        monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *args: None)
        monkeypatch.setattr(server, "_wire_callbacks", lambda *args: None)
        monkeypatch.setattr(server, "_session_info", lambda *args: {})
        monkeypatch.setattr(server, "_get_usage", lambda *args: {})
        execution = MagicMock(released=False)
        monkeypatch.setattr(
            server, "_take_runtime_execution", lambda *_args: execution
        )
        session = server._deferred_session_record(
            "conversation", cols=80, cwd=str(tmp_path), history=[], lease=None
        )
        session["agent"] = Agent()
        session["agent_ready"].set()
        server._sessions["live"] = session

        def emit(event, session_id, payload=None):
            if event == "message.complete":
                row = db.get_turn_receipt_by_client_id(
                    "conversation", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                )
                terminal_state_at_emit.append(row["state"])

        monkeypatch.setattr(server, "_emit", emit)
        response = server.handle_request(
            {
                "id": "end-to-end",
                "method": "turn.submit",
                "params": {
                    "conversation_id": "conversation",
                    "client_turn_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "text": "execute",
                },
            }
        )
        scheduled.pop()()

        persisted = db.get_turn_receipt(response["result"]["turn_id"])
        assert persisted["state"] == "SUCCEEDED"
        assert terminal_state_at_emit == ["SUCCEEDED"]
    finally:
        db.close()


def test_session_watch_is_cold_and_fans_out_without_duplicating_owner(
    server, tmp_path, monkeypatch
):
    import time

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "cli")

    class Sink:
        def __init__(self):
            self.frames = []

        def write(self, frame):
            self.frames.append(frame)
            return True

    owner = Sink()
    observer = Sink()
    try:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        first = server.dispatch(
            {
                "id": "watch-owner",
                "method": "session.watch",
                "params": {"conversation_id": "conversation"},
            },
            owner,
        )
        second = server.dispatch(
            {
                "id": "watch-observer",
                "method": "session.watch",
                "params": {"conversation_id": "conversation"},
            },
            observer,
        )
        assert "error" not in first
        assert first["result"]["event_stream_id"] == second["result"]["event_stream_id"]
        assert server._sessions == {}

        session = server._deferred_session_record(
            "conversation", cols=80, cwd=str(tmp_path), history=[], lease=None
        )
        session["transport"] = owner
        server._sessions["live"] = session
        server._emit("status.update", "live", {"text": "working"})

        deadline = time.monotonic() + 1
        while len(observer.frames) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        owner_events = [frame for frame in owner.frames if frame.get("method") == "event"]
        observer_events = [frame for frame in observer.frames if frame.get("method") == "event"]
        assert len(owner_events) == 1
        assert len(observer_events) == 1
        assert owner_events[0]["params"]["conversation_id"] == "conversation"
        assert owner_events[0]["params"]["seq"] == 1
        assert observer_events[0]["params"]["conversation_id"] == "conversation"
        assert observer_events[0]["params"]["seq"] == 1
    finally:
        if hasattr(server, "_subscription_hub"):
            server._subscription_hub.disconnect(owner)
            server._subscription_hub.disconnect(observer)
        db.close()


def test_runtime_hibernation_cleanup_cannot_clobber_new_generation(server):
    class Agent:
        def __init__(self):
            self.released = 0
            self.closed = 0

        def release_clients(self):
            self.released += 1

        def close(self):
            self.closed += 1

    class Worker:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    old_agent = Agent()
    old_worker = Worker()
    old_poller = MagicMock()
    new_agent = Agent()
    new_worker = Worker()
    new_ready = MagicMock()
    session = {
        "active_session_lease": object(),
        "agent": new_agent,
        "agent_build_started": True,
        "agent_error": None,
        "agent_ready": new_ready,
        "slash_worker": new_worker,
    }
    server._sessions["runtime"] = session

    server._hibernate_runtime_resources(
        "runtime",
        {
            "agent": old_agent,
            "slash_worker": old_worker,
            "_notif_stop": old_poller,
        },
    )

    assert old_agent.released == 1
    assert old_agent.closed == 0
    assert old_worker.closed == 1
    old_poller.set.assert_called_once_with()
    assert session["active_session_lease"] is not None
    assert session["agent"] is new_agent
    assert session["slash_worker"] is new_worker
    assert session["agent_build_started"] is True
    assert session["agent_ready"] is new_ready


def test_stale_runtime_build_cannot_attach_resources(server, monkeypatch):
    class Agent:
        def __init__(self):
            self.released = 0

        def release_clients(self):
            self.released += 1

    class Worker:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    manager = MagicMock()
    manager.finish_build.return_value = False
    monkeypatch.setattr(server, "_runtime_manager", manager)
    session = {"agent": None, "slash_worker": None}
    agent = Agent()
    worker = Worker()

    attached = server._finish_runtime_build(
        "runtime", session, generation=7, agent=agent, worker=worker
    )

    assert attached is False
    manager.finish_build.assert_called_once_with(
        "runtime", 7, agent=agent, slash_worker=worker
    )
    assert session["agent"] is None
    assert session["slash_worker"] is None
    assert agent.released == 1
    assert worker.closed == 1


def test_first_turn_build_hands_off_atomically_to_execution(server, monkeypatch):
    from tui_gateway.session_runtime import RuntimePolicy, RuntimeState, SessionRuntimeManager

    agent = MagicMock()
    worker = MagicMock()
    session = {}
    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=0, max_executing=1),
        cleanup=lambda _sid, _resources: None,
    )
    manager.register("runtime", session)
    server._sessions["runtime"] = session
    build = manager.acquire_build("runtime")
    monkeypatch.setattr(server, "_runtime_manager", manager)

    execution = server._finish_runtime_build_for_execution(
        "runtime",
        session,
        generation=build.generation,
        agent=agent,
        worker=worker,
    )

    assert execution is not None
    assert manager.state("runtime") is RuntimeState.EXECUTING
    assert session["agent"] is agent
    assert session["slash_worker"] is worker
    assert manager.executing_count == 1
    execution.release()


def test_runtime_execution_adopts_legacy_hot_record_and_releases_slot(
    server, monkeypatch
):
    from tui_gateway.session_runtime import RuntimePolicy, RuntimeState, SessionRuntimeManager

    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=2, max_executing=1),
        cleanup=server._hibernate_runtime_resources,
    )
    monkeypatch.setattr(server, "_runtime_manager", manager)
    session = {"agent": object(), "slash_worker": object()}
    server._sessions["execution"] = session

    lease = server._acquire_runtime_execution("execution", session)

    assert manager.state("execution") is RuntimeState.EXECUTING
    assert manager.executing_count == 1
    assert lease.release() is True
    assert manager.state("execution") is RuntimeState.HOT_IDLE
    assert manager.executing_count == 0


def test_close_session_fences_runtime_before_teardown(server, monkeypatch):
    from tui_gateway.session_runtime import RuntimePolicy, RuntimeState, SessionRuntimeManager

    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=2, max_executing=1),
        cleanup=server._hibernate_runtime_resources,
    )
    monkeypatch.setattr(server, "_runtime_manager", manager)
    torn_down = []
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda session, *, end_reason="tui_close": torn_down.append(session),
    )
    session = {}
    manager.register("closing", session)
    build = manager.acquire_build("closing")
    server._sessions["closing"] = session

    assert server._close_session_by_id("closing") is True

    assert torn_down == [session]
    assert manager.state("closing") is RuntimeState.CLOSED
    assert manager.finish_build("closing", build.generation, agent="late") is False


def test_eager_init_session_adopts_runtime_as_hot_idle(server, monkeypatch):
    from tui_gateway.session_runtime import RuntimePolicy, RuntimeState, SessionRuntimeManager

    manager = SessionRuntimeManager(
        RuntimePolicy(max_hot_idle=2, max_executing=1),
        cleanup=server._hibernate_runtime_resources,
    )
    monkeypatch.setattr(server, "_runtime_manager", manager)
    monkeypatch.setattr(server, "_get_db", lambda: None)

    class Agent:
        model = "test-model"

        def release_clients(self):
            pass

    class Worker:
        def close(self):
            pass

    server._adopt_eager_runtime("eager", {"agent": Agent(), "slash_worker": Worker()})

    assert manager.state("eager") is RuntimeState.HOT_IDLE
    assert manager.hot_idle_count == 1
