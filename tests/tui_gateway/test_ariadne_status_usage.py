"""Ariadne's existing heartbeat consumes session.status.usage without building an agent."""

from types import SimpleNamespace
import threading

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def _status(monkeypatch, tmp_path, session):
    home = tmp_path / "profile"
    home.mkdir()
    with SessionDB(home / "state.db") as db:
        db.create_session("stored", source="tui", model="audit-model")
    session.update(session_key="stored", profile_home=str(home), history=[], running=False,
                   agent_ready=threading.Event())
    monkeypatch.setitem(server._sessions, "ariadne-status", session)

    def forbidden_build(*args, **kwargs):
        pytest.fail("status must not materialize or wait for an agent")

    monkeypatch.setattr(server, "_start_agent_build", forbidden_build)
    response = server.handle_request({"jsonrpc": "2.0", "id": "status",
                                      "method": "session.status",
                                      "params": {"session_id": "ariadne-status"}})
    assert "error" not in response, response
    assert "Hermes TUI Status" in response["result"]["output"]
    return response["result"]["usage"]


@pytest.mark.parametrize("isolated", [False, True])
def test_status_uses_the_authoritative_usage_without_building(monkeypatch, tmp_path, isolated):
    agent = SimpleNamespace(model="audit-model", provider="custom",
                            session_input_tokens=100, session_output_tokens=25,
                            session_total_tokens=125, session_api_calls=1)
    mirrored = {"model": "host-model", "input": 200, "output": 50, "total": 250,
                "calls": 2, "context_used": 80, "context_max": 100, "context_percent": 80}
    session = {"agent": agent, "_compute_host_active": isolated,
               "_metadata_mirror": {"model": "host-model", "usage": mirrored}}
    expected = server._session_usage_snapshot(session)
    usage = _status(monkeypatch, tmp_path, session)
    assert usage == expected
    assert usage["total"] == (mirrored["total"] if isolated else agent.session_total_tokens)
    if isolated:
        assert usage["context_used"] == mirrored["context_used"]
    else:
        assert "context_used" not in usage  # cumulative tokens are not context occupancy


def test_cold_status_keeps_unknown_context_unknown(monkeypatch, tmp_path):
    session = {"agent": None}
    usage = _status(monkeypatch, tmp_path, session)
    assert usage == {}
    assert session["agent"] is None
    assert not session["agent_ready"].is_set()
