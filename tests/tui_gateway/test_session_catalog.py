"""Cold, snapshot-stable conversation reads for Session Runtime Manager."""

import json

import pytest

from hermes_state import SessionDB
from tui_gateway.session_catalog import (
    InvalidHistoryCursor,
    PageItemTooLarge,
    SessionCatalog,
)


@pytest.fixture
def catalog(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield db, SessionCatalog(db)
    finally:
        db.close()


def _compression_chain(db):
    db.create_session("root", "cli")
    db.append_message("root", "user", "one", timestamp=1)
    db.append_message(
        "root",
        "assistant",
        "two",
        reasoning="reasoning-two",
        reasoning_details=[{"type": "summary_text", "text": "trace-two"}],
        timestamp=2,
    )
    db.end_session("root", end_reason="compression")
    db.create_session("tip", "cli", parent_session_id="root")
    db.append_message("tip", "user", "three", timestamp=3)
    db.append_message("tip", "assistant", "four", timestamp=4)


def test_cold_tail_read_pages_across_compression_without_runtime(catalog):
    db, cold = catalog
    _compression_chain(db)

    page = cold.read("root", limit=2, byte_limit=64_000, view="dialog")

    assert page["conversation_id"] == "root"
    assert page["tip_session_id"] == "tip"
    assert [item["content"] for item in page["messages"]] == ["three", "four"]
    assert page["has_more"] is True
    assert page["next_cursor"]
    # The cold catalog owns no agent/runtime factory and therefore cannot
    # materialize AIAgent/slash_worker as a side effect of reading.
    assert not hasattr(cold, "runtime_factory")

    older = cold.read("root", cursor=page["next_cursor"], limit=2, byte_limit=64_000)
    assert [item["content"] for item in older["messages"]] == ["one", "two"]
    assert older["messages"][1]["reasoning"] == "reasoning-two"
    assert older["messages"][1]["reasoning_details"][0]["text"] == "trace-two"
    assert older["has_more"] is False


def test_cursor_holds_stable_snapshot_while_new_messages_arrive(catalog):
    db, cold = catalog
    _compression_chain(db)

    first = cold.read("tip", limit=2, byte_limit=64_000)
    db.append_message("tip", "assistant", "arrived-after-snapshot", timestamp=5)

    older = cold.read("tip", cursor=first["next_cursor"], limit=10, byte_limit=64_000)
    assert "arrived-after-snapshot" not in [item["content"] for item in older["messages"]]
    assert older["snapshot_max_message_id"] == first["snapshot_max_message_id"]

    fresh = cold.read("tip", limit=10, byte_limit=64_000)
    assert fresh["messages"][-1]["content"] == "arrived-after-snapshot"
    assert fresh["snapshot_max_message_id"] > first["snapshot_max_message_id"]


def test_dialog_and_timeline_views_are_explicit(catalog):
    db, cold = catalog
    db.create_session("session", "cli")
    db.append_message("session", "assistant", None, tool_calls=[{"id": "call-1", "type": "function"}])
    db.append_message("session", "tool", "tool result", tool_name="terminal", tool_call_id="call-1")

    dialog = cold.read("session", limit=10, byte_limit=64_000, view="dialog")
    timeline = cold.read("session", limit=10, byte_limit=64_000, view="timeline")

    assert [item["role"] for item in dialog["messages"]] == ["assistant"]
    assert [item["role"] for item in timeline["messages"]] == ["assistant", "tool"]
    assert timeline["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert timeline["messages"][1]["tool_name"] == "terminal"


def test_cursor_is_bound_to_conversation_and_view(catalog):
    db, cold = catalog
    _compression_chain(db)
    db.create_session("other", "cli")

    cursor = cold.read("root", limit=1, byte_limit=64_000)["next_cursor"]

    with pytest.raises(InvalidHistoryCursor):
        cold.read("other", cursor=cursor, limit=1, byte_limit=64_000)
    with pytest.raises(InvalidHistoryCursor):
        cold.read("root", cursor=cursor, limit=1, byte_limit=64_000, view="timeline")
    with pytest.raises(InvalidHistoryCursor):
        cold.read("root", cursor="not-base64", limit=1, byte_limit=64_000)


def test_count_and_byte_budgets_are_enforced_without_silent_truncation(catalog):
    db, cold = catalog
    db.create_session("session", "cli")
    for index in range(4):
        db.append_message("session", "user", f"message-{index}-" + ("x" * 80))

    page = cold.read("session", limit=10, byte_limit=800)
    encoded_size = len(
        json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert 0 < len(page["messages"]) < 4
    assert encoded_size <= 800
    assert page["has_more"] is True

    db.append_message("session", "user", "z" * 2_000)
    with pytest.raises(PageItemTooLarge) as exc_info:
        cold.read("session", limit=10, byte_limit=200)
    assert exc_info.value.required_bytes > 200


def test_root_middle_and_tip_aliases_share_one_logical_conversation(catalog):
    db, cold = catalog
    db.create_session("root", "cli")
    db.append_message("root", "user", "root-message")
    db.end_session("root", end_reason="compression")
    db.create_session("middle", "cli", parent_session_id="root")
    db.append_message("middle", "assistant", "middle-message")
    db.end_session("middle", end_reason="compression")
    db.create_session("tip", "cli", parent_session_id="middle")
    db.append_message("tip", "user", "tip-message")

    pages = [cold.read(alias, limit=10, byte_limit=64_000) for alias in ("root", "middle", "tip")]
    assert {page["conversation_id"] for page in pages} == {"root"}
    assert {page["tip_session_id"] for page in pages} == {"tip"}
    assert [[item["content"] for item in page["messages"]] for page in pages] == [
        ["root-message", "middle-message", "tip-message"],
        ["root-message", "middle-message", "tip-message"],
        ["root-message", "middle-message", "tip-message"],
    ]


def test_explicit_branch_is_not_folded_into_parent_conversation(catalog):
    db, cold = catalog
    db.create_session("root", "cli")
    db.append_message("root", "user", "parent")
    db.end_session("root", end_reason="compression")
    db.create_session(
        "branch",
        "cli",
        parent_session_id="root",
        model_config={"_branched_from": "root"},
    )
    db.append_message("branch", "user", "branch-only")

    branch = cold.read("branch", limit=10, byte_limit=64_000)
    assert branch["conversation_id"] == "branch"
    assert [item["content"] for item in branch["messages"]] == ["branch-only"]


def test_pagination_orders_by_row_id_not_non_monotonic_timestamp(catalog):
    db, cold = catalog
    db.create_session("session", "cli")
    db.append_message("session", "user", "inserted-first", timestamp=200)
    db.append_message("session", "assistant", "inserted-second", timestamp=100)

    page = cold.read("session", limit=10, byte_limit=64_000)
    assert [item["content"] for item in page["messages"]] == ["inserted-first", "inserted-second"]
    assert [item["row_id"] for item in page["messages"]] == sorted(
        item["row_id"] for item in page["messages"]
    )


def test_inactive_rows_are_excluded(catalog):
    db, cold = catalog
    db.create_session("session", "cli")
    first = db.append_message("session", "user", "keep")
    target = db.append_message("session", "user", "rewind-me")
    db.append_message("session", "assistant", "also-rewound")
    db.rewind_to_message("session", target)

    page = cold.read("session", limit=10, byte_limit=64_000)
    assert [item["content"] for item in page["messages"]] == ["keep"]
    assert page["messages"][0]["row_id"] == first
    assert page["has_more"] is False


def test_cursor_is_bound_to_profile_scope(catalog):
    db, cold = catalog
    _compression_chain(db)
    scoped_a = SessionCatalog(db, scope="profile-a")
    scoped_b = SessionCatalog(db, scope="profile-b")
    cursor = scoped_a.read("root", limit=1, byte_limit=64_000)["next_cursor"]

    with pytest.raises(InvalidHistoryCursor):
        scoped_b.read("root", cursor=cursor, limit=1, byte_limit=64_000)
