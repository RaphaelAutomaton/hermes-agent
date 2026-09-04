"""Versioned JSON-RPC capability advertisement shared by stdio and WS."""

from __future__ import annotations

from typing import Any, Dict


PROTOCOL_VERSION = 2
SESSION_RUNTIME_CAPABILITIES = {
    "version": 2,
    "cold_read": True,
    "history_keyset_pagination": True,
    "stable_snapshot": True,
    # Advertise future cuts only when they are implemented end-to-end.
    "durable_turns": True,
    "subscriptions": True,
    "runtime_pool": False,
}


def gateway_ready_payload(skin: str) -> Dict[str, Any]:
    """Return one truthful capability envelope for every gateway transport."""
    return {
        "skin": skin,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": {"session_runtime": dict(SESSION_RUNTIME_CAPABILITIES)},
    }
