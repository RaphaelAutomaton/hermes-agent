"""Persistence-only conversation catalog for cold Session Runtime Manager reads.

The catalog deliberately depends on :class:`hermes_state.SessionDB` only. It has
no access to gateway runtime maps, agent builders, transports, or slash workers;
therefore list/read operations cannot materialize execution resources.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from hermes_state import SessionDB


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
DEFAULT_BYTE_LIMIT = 512 * 1024
MAX_BYTE_LIMIT = 8 * 1024 * 1024
_CURSOR_VERSION = 1


class InvalidHistoryCursor(ValueError):
    """The opaque history cursor is malformed or belongs to another query."""


class ConversationNotFound(KeyError):
    """No persisted session alias resolves to the requested conversation."""


@dataclass
class PageItemTooLarge(ValueError):
    required_bytes: int
    byte_limit: int

    def __str__(self) -> str:
        return (
            f"newest history item requires {self.required_bytes} bytes, "
            f"exceeding page byte_limit={self.byte_limit}"
        )


class SessionCatalog:
    """Read persisted logical conversations without touching live runtimes."""

    def __init__(self, db: SessionDB, *, scope: str = ""):
        self._db = db
        self._scope = str(scope or "")

    @staticmethod
    def _encode_cursor(payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> Dict[str, Any]:
        try:
            encoded = cursor.encode("ascii")
            raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidHistoryCursor("malformed history cursor") from exc
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise InvalidHistoryCursor("unsupported history cursor")
        return payload

    @staticmethod
    def _present(row: Dict[str, Any]) -> Dict[str, Any]:
        item = {
            "row_id": int(row["id"]),
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row.get("content"),
            "timestamp": row.get("timestamp"),
        }
        optional_fields = (
            "tool_call_id",
            "tool_calls",
            "tool_name",
            "finish_reason",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "codex_reasoning_items",
            "codex_message_items",
            "platform_message_id",
        )
        for field in optional_fields:
            value = row.get(field)
            if value is not None:
                item[field] = value
        if row.get("observed"):
            item["observed"] = True
        return item

    def read(
        self,
        conversation_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        byte_limit: int = DEFAULT_BYTE_LIMIT,
        view: str = "dialog",
    ) -> Dict[str, Any]:
        """Read a newest-tail page, returning messages in chronological order.

        Pagination uses message AUTOINCREMENT ids, never OFFSET. The first page
        captures a maximum id; subsequent cursor pages retain that snapshot even
        while newer messages are appended.
        """
        if view not in {"dialog", "timeline"}:
            raise ValueError("view must be 'dialog' or 'timeline'")
        bounded_limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        bounded_bytes = max(1, min(int(byte_limit), MAX_BYTE_LIMIT))

        try:
            logical_id, tip_session_id, lineage = self._db.resolve_conversation_lineage(conversation_id)
        except KeyError as exc:
            raise ConversationNotFound(conversation_id) from exc

        snapshot_max_id = None
        before_id = None
        if cursor:
            payload = self._decode_cursor(cursor)
            if (
                payload.get("conversation_id") != logical_id
                or payload.get("view") != view
                or payload.get("scope") != self._scope
                or payload.get("direction") != "older"
            ):
                raise InvalidHistoryCursor("cursor does not belong to this scope/conversation/view")
            try:
                snapshot_max_id = int(payload["snapshot_max_id"])
                before_id = int(payload["before_id"])
                cursor_lineage = [str(item) for item in payload["lineage"]]
                cursor_tip = str(payload["tip_session_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidHistoryCursor("incomplete history cursor") from exc
            if not cursor_lineage or cursor_lineage[0] != logical_id:
                raise InvalidHistoryCursor("invalid cursor lineage")
            lineage = cursor_lineage
            tip_session_id = cursor_tip

        roles = ["user", "assistant"] if view == "dialog" else None
        rows, snapshot_max_id = self._db.read_message_rows_page(
            lineage,
            snapshot_max_id=snapshot_max_id,
            before_id=before_id,
            limit=bounded_limit,
            roles=roles,
        )

        selected: List[Dict[str, Any]] = [
            self._present(row) for row in rows[:bounded_limit]
        ]

        def build_page() -> tuple[Dict[str, Any], int]:
            has_more = len(rows) > len(selected)
            next_cursor = None
            if has_more and selected:
                next_cursor = self._encode_cursor(
                    {
                        "v": _CURSOR_VERSION,
                        "scope": self._scope,
                        "conversation_id": logical_id,
                        "tip_session_id": tip_session_id,
                        "lineage": lineage,
                        "view": view,
                        "direction": "older",
                        "snapshot_max_id": snapshot_max_id,
                        "before_id": min(item["row_id"] for item in selected),
                    }
                )
            page: Dict[str, Any] = {
                "conversation_id": logical_id,
                "tip_session_id": tip_session_id,
                "view": view,
                "snapshot_max_message_id": snapshot_max_id,
                "messages": list(reversed(selected)),
                "page_bytes": 0,
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            # page_bytes describes the complete compact JSON result. Iterate to
            # a fixed point because writing the decimal size can change its own
            # encoded width at a power-of-ten boundary.
            encoded_size = 0
            for _ in range(4):
                encoded_size = len(
                    json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                if page["page_bytes"] == encoded_size:
                    break
                page["page_bytes"] = encoded_size
            encoded_size = len(
                json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            return page, encoded_size

        while True:
            page, encoded_size = build_page()
            if encoded_size <= bounded_bytes:
                return page
            if len(selected) <= 1:
                # Never silently cut persisted content. The caller can request a
                # larger bounded frame or choose another view.
                raise PageItemTooLarge(encoded_size, bounded_bytes)
            # Preserve the newest tail and leave the removed older item for the
            # next keyset page.
            selected.pop()
