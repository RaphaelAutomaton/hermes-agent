"""Skill-only RPC contract; real discovery in an isolated skill directory."""

from unittest.mock import Mock

import pytest

from agent import skill_commands, skill_utils
from tools import skills_tool
from tui_gateway import server


@pytest.fixture
def skills(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", root)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda: [])
    monkeypatch.setattr(skill_commands, "_skill_commands", {})
    monkeypatch.setattr(skill_commands, "_skill_commands_platform", None)
    monkeypatch.setattr(server, "_sessions", {
        "sid": {"session_key": "durable-key", "running": False, "agent": object()},
    })
    # Session hydration is outside this RPC's contract; never construct an LLM.
    monkeypatch.setattr(server, "_start_agent_build", Mock())
    monkeypatch.setattr(server, "_load_cfg", lambda: {})

    def create(name, description="Public description"):
        directory = root / name
        directory.mkdir(exist_ok=True)
        path = directory / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n"
            "PRIVATE_SKILL_BODY\n", encoding="utf-8",
        )
        return path

    return create


def test_catalog_separates_skill_metadata_without_changing_legacy_fields(skills, monkeypatch):
    skills("handoff")
    skills("sessions")
    skills("unique_native", "D" * 130)
    monkeypatch.setattr(server, "_load_cfg", lambda: {
        "quick_commands": {"handoff": {"type": "alias", "target": "/quit"}},
    })
    scan = Mock(wraps=skill_commands.scan_skill_commands)
    monkeypatch.setattr(skill_commands, "scan_skill_commands", scan)
    result = server._methods["commands.catalog"]("catalog", {})["result"]
    entries = result["skill_entries"]
    assert entries == [
        {"name": "/handoff", "description": "Public description"},
        {"name": "/sessions", "description": "Public description"},
        {"name": "/unique-native", "description": "D" * 120 + "…"},
    ]
    scan.assert_called_once_with()
    assert set(result) == {
        "pairs", "sub", "canon", "categories", "skill_count", "warning", "skill_entries",
    }
    assert result["skill_count"] == len(entries)
    assert result["warning"] == ""
    assert sum(pair[0] == "/handoff" for pair in result["pairs"]) == 3
    assert sum(pair[0] == "/unique-native" for pair in result["pairs"]) == 1
    for entry in entries:
        assert [entry["name"], entry["description"]] in result["pairs"]
    assert "/unique-native" not in result["canon"]
    assert all(pair[0] != "/unique-native" for cat in result["categories"] for pair in cat["pairs"])
    assert "PRIVATE_SKILL_BODY" not in str(result)
    assert "skill_dir" not in str(entries)
    assert "skill_md_path" not in str(entries)


@pytest.mark.parametrize("name, typed", [
    ("handoff", "/handoff"), ("q", "/q"), ("unique_native", "/unique_native"),
    ("unique_native", "unique-native"),
])
def test_invoke_is_skill_only_even_for_builtin_quick_plugin_collisions(skills, monkeypatch, name, typed):
    from hermes_cli import plugins

    skills(name)
    monkeypatch.setattr(server, "_load_cfg", lambda: {
        "quick_commands": {name: {"type": "exec", "command": "must-not-run"}},
    })
    plugin_handler = Mock()
    plugin_lookup = Mock(return_value=plugin_handler)
    monkeypatch.setattr(plugins, "get_plugin_command_handler", plugin_lookup)
    shell = Mock(side_effect=AssertionError("quick/worker execution forbidden"))
    monkeypatch.setattr(server.subprocess, "run", shell)
    alias = Mock(side_effect=AssertionError("builtin alias resolution forbidden"))
    monkeypatch.setattr(server, "_resolve_name", alias)
    submit = Mock(side_effect=AssertionError("inference forbidden"))
    monkeypatch.setitem(server._methods, "prompt.submit", submit)
    builder = Mock(wraps=skill_commands.build_skill_invocation_message)
    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", builder)
    result = server._methods["skill.invoke"]("invoke", {
        "session_id": "sid", "name": typed, "arg": "Keep this instruction",
    })["result"]
    assert set(result) == {"type", "name", "message"}
    assert result["type"] == "skill"
    assert result["name"] == name
    assert "PRIVATE_SKILL_BODY" in result["message"]
    assert "Keep this instruction" in result["message"]
    builder.assert_called_once_with("/" + name.replace("_", "-"), "Keep this instruction", task_id="durable-key")
    shell.assert_not_called()
    plugin_lookup.assert_not_called()
    plugin_handler.assert_not_called()
    alias.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("session_id, running, code", [
    (None, False, 4001), ("absent", False, 4001), ("sid", True, 4002),
])
def test_session_gate_prevents_skill_preprocessing(skills, monkeypatch, session_id, running, code):
    skills("handoff")
    server._sessions["sid"]["running"] = running
    scan = Mock(wraps=skill_commands.scan_skill_commands)
    builder = Mock(side_effect=AssertionError("preprocessing must not run"))
    monkeypatch.setattr(skill_commands, "scan_skill_commands", scan)
    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", builder)
    response = server._methods["skill.invoke"]("gate", {
        "session_id": session_id, "name": "/handoff", "arg": "",
    })
    assert response["error"]["code"] == code
    if running:
        assert "busy" in response["error"]["message"].lower()
    builder.assert_not_called()
    scan.assert_not_called()


@pytest.mark.parametrize("updates", [
    {"name": None}, {"name": 42}, {"name": []}, {"name": ""},
    {"name": "/"}, {"name": "//handoff"}, {"name": "../handoff"},
    {"name": "/handoff extra"}, {"name": "handoff\n"}, {"name": "x" * 257},
    {"arg": None}, {"arg": 42}, {"arg": []}, {"arg": {}},
    {"arg": "x" * 100_001}, {"session_id": []}, {"session_id": 42},
])
def test_malformed_parameters_fail_before_any_skill_effect(skills, monkeypatch, updates):
    skills("handoff")
    scan = Mock(wraps=skill_commands.scan_skill_commands)
    builder = Mock(return_value="must-not-build")
    monkeypatch.setattr(skill_commands, "scan_skill_commands", scan)
    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", builder)
    params = {"session_id": "sid", "name": "/handoff", "arg": ""}
    params.update(updates)
    response = server._methods["skill.invoke"]("bad", params)
    assert response["error"]["code"] == 4003
    scan.assert_not_called()
    builder.assert_not_called()


@pytest.mark.parametrize("state", ["unknown", "disabled", "platform-disabled"])
def test_unavailable_skills_fail_closed(skills, monkeypatch, state):
    from hermes_constants import get_hermes_home

    if state != "unknown":
        skills("handoff")
        config = "skills:\n  disabled: [handoff]\n"
        if state == "platform-disabled":
            config = "skills:\n  platform_disabled:\n    tui: [handoff]\n"
            monkeypatch.setenv("HERMES_PLATFORM", "tui")
        (get_hermes_home() / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(server, "_load_cfg", lambda: {
        "quick_commands": {"handoff": {"type": "alias", "target": "/quit"}},
    })
    builder = Mock(return_value="must-not-build")
    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", builder)
    response = server._methods["skill.invoke"]("unavailable", {
        "session_id": "sid", "name": "/handoff", "arg": "",
    })
    assert response["error"]["message"] == "skill_unavailable"
    builder.assert_not_called()


def test_invoke_rescans_additions_and_removals(skills):
    invoke = server._methods["skill.invoke"]
    params = {"session_id": "sid", "name": "/new-skill"}
    assert invoke("before", params)["error"]["message"] == "skill_unavailable"
    path = skills("new-skill")
    assert invoke("added", params)["result"]["name"] == "new-skill"
    path.unlink()
    assert invoke("removed", params)["error"]["message"] == "skill_unavailable"


@pytest.mark.parametrize("stage", ["scan", "resolve", "build", "empty", "none", "wrong-type"])
def test_skill_load_failures_are_explicit_without_exception_details(skills, monkeypatch, stage):
    skills("handoff")
    leak = "private/path/credential-like-details"
    if stage in {"scan", "resolve", "build"}:
        target = {
            "scan": "scan_skill_commands", "resolve": "resolve_skill_command_key",
            "build": "build_skill_invocation_message",
        }[stage]
        monkeypatch.setattr(skill_commands, target, Mock(side_effect=RuntimeError(leak)))
    else:
        monkeypatch.setattr(skill_commands, "build_skill_invocation_message", Mock(
            return_value={"empty": "", "none": None, "wrong-type": {"secret": leak}}[stage],
        ))
    response = server._methods["skill.invoke"]("failed", {
        "session_id": "sid", "name": "/handoff", "arg": "",
    })
    assert response["error"]["message"] == "skill_load_failed"
    assert leak not in str(response)
    assert "result" not in response


@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("control", ["approval.respond", "session.interrupt"])
def test_slow_skill_dispatch_keeps_interrupt_and_approval_responsive(skills, monkeypatch, lazy, control):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway.transport import current_transport

    skills("handoff")
    entered, release, returned, replied = (threading.Event() for _ in range(4))
    frames, seen = [], []

    class Sink:
        def write(self, frame):
            frames.append(frame)
            replied.set()
            return True

    owner = Sink()
    session = server._sessions["sid"]
    session["transport"] = owner
    if lazy:
        session.update(agent=None, agent_ready=threading.Event(), agent_build_started=False)
    build = Mock()
    wait = Mock(return_value=server._err("control", 5032, "must not wait"))
    monkeypatch.setattr(server, "_start_agent_build", build)
    monkeypatch.setattr(server, "_wait_agent", wait)
    original = skill_commands.build_skill_invocation_message

    def slow(*args, **kwargs):
        seen.append(current_transport())
        entered.set()
        assert release.wait(5), "test failed to release preprocessing"
        return original(*args, **kwargs)

    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", slow)
    # Use the real approval resolver, but an isolated pending entry.
    from tools import approval as approval_module
    request_id = "a" * 32
    approval = approval_module._ApprovalEntry({"request_id": request_id})
    monkeypatch.setattr(approval_module, "_gateway_queues", {"durable-key": [approval]})
    session["history_lock"] = threading.RLock()
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_answers", {})

    def read_loop():
        server.dispatch({"id": "slow", "method": "skill.invoke", "params": {
            "session_id": "sid", "name": "/handoff",
        }}, transport=owner)
        returned.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(server, "_pool", pool)
        reader = threading.Thread(target=read_loop)
        reader.start()
        try:
            assert entered.wait(3)
            assert returned.wait(0.5), "skill.invoke blocked the dispatcher during preprocessing"
            answer = server.dispatch({"id": "control", "method": control, "params": {
                "session_id": "sid", "request_id": request_id, "choice": "once",
            }}, transport=owner)
            build.assert_not_called()
            wait.assert_not_called()
            if control == "approval.respond":
                assert answer["result"]["resolved"] == 1
                assert approval.result == "once"
            else:
                assert answer["result"]["status"] == "interrupted"
                assert approval.result == "deny"
                assert session["_turn_cancel_requested"] is True
            assert approval.event.is_set()
            assert not replied.is_set(), "skill should still be blocked in test loader"
        finally:
            release.set()
            reader.join(5)
    assert replied.wait(1)
    assert frames[0]["id"] == "slow"
    assert "PRIVATE_SKILL_BODY" in frames[0]["result"]["message"]
    assert seen == [owner]
    assert session["transport"] is owner
    assert session["running"] is False


@pytest.mark.parametrize("running", [False, True])
def test_lazy_skill_never_builds_or_waits_for_an_agent(skills, monkeypatch, running):
    import threading

    skills("handoff")
    session = server._sessions["sid"]
    session.update(agent=None, running=running, agent_ready=threading.Event(), agent_build_started=False)
    build = Mock()
    wait = Mock(return_value=server._err("lazy", 5032, "must not wait"))
    monkeypatch.setattr(server, "_start_agent_build", build)
    monkeypatch.setattr(server, "_wait_agent", wait)
    before = dict(session)
    result = server.handle_request({"id": "lazy", "method": "skill.invoke", "params": {
        "session_id": "sid", "name": "/handoff",
    }})
    build.assert_not_called()
    wait.assert_not_called()
    assert session == before
    if running:
        assert result["error"]["code"] == 4002
    else:
        assert "PRIVATE_SKILL_BODY" in result["result"]["message"]


def test_ready_advertises_native_skill_invocation():
    from tui_gateway.protocol import gateway_ready_payload

    assert gateway_ready_payload("default")["capabilities"]["session_runtime"]["skill_invocation"] is True


def test_wire_invocation_preserves_argument_boundary_and_profile_defaults(skills):
    from hermes_constants import get_hermes_home

    skills("handoff")
    home = get_hermes_home()
    config = home / "config.yaml"
    auth = home / "auth.json"
    config.write_text("model: isolated-test-model\n", encoding="utf-8")
    auth.write_text("{}\n", encoding="utf-8")
    before = (config.read_bytes(), auth.read_bytes())
    argument = "line one\n" + "x" * (100_000 - len("line one\n"))
    response = server.handle_request({
        "jsonrpc": "2.0", "id": "wire", "method": "skill.invoke",
        "params": {"session_id": "sid", "name": "/handoff", "arg": argument},
    })
    assert response["id"] == "wire"
    assert response["result"]["type"] == "skill"
    assert argument in response["result"]["message"]
    assert (config.read_bytes(), auth.read_bytes()) == before
    assert server._sessions["sid"]["running"] is False


def test_disabled_after_catalog_is_rejected_without_loading(skills, monkeypatch):
    from hermes_constants import get_hermes_home

    skills("handoff")
    assert server._methods["commands.catalog"]("catalog", {})["result"]["skill_entries"]
    (get_hermes_home() / "config.yaml").write_text(
        "skills:\n  disabled: [handoff]\n", encoding="utf-8",
    )
    builder = Mock()
    monkeypatch.setattr(skill_commands, "build_skill_invocation_message", builder)
    result = server._methods["skill.invoke"]("disabled", {"session_id": "sid", "name": "/handoff"})
    assert result["error"]["message"] == "skill_unavailable"
    builder.assert_not_called()
    assert server._methods["commands.catalog"]("catalog", {})["result"]["skill_entries"] == []
