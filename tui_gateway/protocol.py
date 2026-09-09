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
    # The server now runs a bounded Session Runtime Manager (max_hot_idle /
    # max_executing / sweep TTL + RSS eviction / hibernation that preserves the
    # durable conversation). Advertised only because the pool is live and
    # limits runtime materialization regardless of client opt-in.
    "runtime_pool": True,
    # config.set reasoning/fast with a session never writes profile defaults.
    "session_model_controls": True,
}


def gateway_ready_payload(skin: str) -> Dict[str, Any]:
    """Return one truthful capability envelope for every gateway transport."""
    return {
        "skin": skin,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": {"session_runtime": dict(SESSION_RUNTIME_CAPABILITIES)},
    }
