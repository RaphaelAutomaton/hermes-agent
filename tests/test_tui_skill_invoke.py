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
