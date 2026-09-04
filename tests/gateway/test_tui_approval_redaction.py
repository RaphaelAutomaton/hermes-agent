"""Regression test for TUI approval-prompt credential redaction (#48456).

Follow-up to #50767, which redacted the chat-platform and SSE/API approval
transports. The TUI JSON-RPC transport is the third egress: three
`register_gateway_notify` callbacks in `tui_gateway/server.py` emit the raw
`approval_data` (with an unredacted `command`) to the TUI client. They now
route through the module-level `_emit_approval_request` helper, which redacts
`payload["command"]` via the shared `gateway.run._redact_approval_command` seam
before emitting.
"""

import inspect
from unittest.mock import Mock

import pytest


class TestTuiApprovalEmitRedaction:
    def test_emit_approval_request_redacts_command_in_payload(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server, "_emit",
            lambda event, sid, payload=None: emitted.update(
                {"event": event, "sid": sid, "payload": payload}
            ),
        )
        fake_token = "ghp_" + "012345678901234567890123456789012345"
        raw = f"curl -H 'Authorization: token {fake_token}' https://api.github.com"
        tui_server._emit_approval_request(
            "sess-1",
            {"command": raw, "description": "x", "request_id": "tool_123"},
        )

        assert emitted["event"] == "approval.request"
        # credential removed, non-command field + command structure preserved
        assert fake_token not in emitted["payload"]["command"]
        assert emitted["payload"]["description"] == "x"
        assert emitted["payload"]["request_id"] == "tool_123"
        assert "github.com" in emitted["payload"]["command"]


class TestTuiApprovalRespondContract:
    @pytest.fixture(autouse=True)
    def _session(self, monkeypatch):
        from tui_gateway import server as tui_server

        monkeypatch.setattr(
            tui_server,
            "_sess",
            lambda params, rid: ({"session_key": "session-key"}, None),
        )

    @pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
    def test_correlated_choices_are_forwarded_exactly(self, monkeypatch, choice):
        from tools import approval as approval_mod
        from tui_gateway import server as tui_server

        resolver = Mock(return_value=1)
        monkeypatch.setattr(approval_mod, "resolve_gateway_approval", resolver)
        request_id = "a" * 32

        response = tui_server._methods["approval.respond"](
            "rpc-1",
            {"session_id": "session-1", "request_id": request_id, "choice": choice},
        )

        assert response["result"] == {"resolved": 1}
        resolver.assert_called_once_with(
            "session-key", choice, resolve_all=False, request_id=request_id
        )

    @pytest.mark.parametrize(
        "params",
        [
            {"session_id": "session-1", "choice": "once"},
            {"session_id": "session-1", "request_id": "a" * 32},
            {"session_id": "session-1", "request_id": "a" * 32, "choice": "garbage"},
            {"session_id": "session-1", "request_id": "../bad", "choice": "deny"},
            {"session_id": "session-1", "request_id": "a" * 32, "choice": "deny", "all": True},
            {"session_id": "session-1", "request_id": "a" * 32, "choice": "deny", "extra": True},
        ],
    )
    def test_malformed_or_policy_expanding_params_fail_before_resolution(self, monkeypatch, params):
        from tools import approval as approval_mod
        from tui_gateway import server as tui_server

        resolver = Mock(return_value=1)
        monkeypatch.setattr(approval_mod, "resolve_gateway_approval", resolver)

        response = tui_server._methods["approval.respond"]("rpc-bad", params)

        assert response["error"]["code"] == 4002
        resolver.assert_not_called()

    def test_unknown_well_formed_id_does_not_fall_back_to_fifo(self):
        from tools.approval import _ApprovalEntry, _gateway_queues
        from tui_gateway import server as tui_server

        first = _ApprovalEntry({"command": "first", "request_id": "1" * 32})
        second = _ApprovalEntry({"command": "second", "request_id": "2" * 32})
        _gateway_queues["session-key"] = [first, second]
        try:
            response = tui_server._methods["approval.respond"](
                "rpc-unknown",
                {"session_id": "session-1", "request_id": "f" * 32, "choice": "once"},
            )

            assert response["result"] == {"resolved": 0}
            assert _gateway_queues["session-key"] == [first, second]
            assert not first.event.is_set()
            assert not second.event.is_set()
        finally:
            _gateway_queues.pop("session-key", None)

    def test_emit_approval_request_handles_missing_command(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server, "_emit",
            lambda event, sid, payload=None: emitted.update({"payload": payload}),
        )
        tui_server._emit_approval_request("s", {"description": "no command here"})
        assert emitted["payload"] == {"description": "no command here"}
        tui_server._emit_approval_request("s", None)
        assert emitted["payload"] == {}

    def test_no_raw_command_emit_in_approval_registrations(self):
        """Every register_gateway_notify approval callback must route through the
        redacting `_emit_approval_request` helper — no registration may emit the
        raw payload via `_emit("approval.request", ...)` directly. The ONLY
        allowed raw emit is inside the helper itself."""
        from tui_gateway import server as tui_server

        src = inspect.getsource(tui_server)
        raw_emits = src.count('_emit("approval.request"')
        assert raw_emits == 1, (
            f'expected exactly 1 raw _emit("approval.request") (inside the '
            f"redacting helper), found {raw_emits} — a registration may be "
            f"emitting the unredacted command"
        )
        assert "_emit_approval_request(sid, data)" in src, (
            "registration lambdas must route through _emit_approval_request"
        )
