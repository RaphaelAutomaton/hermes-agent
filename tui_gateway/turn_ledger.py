"""Durable, idempotent turn receipts for Session Runtime Manager.

This module owns the turn state machine.  It intentionally knows nothing about
AIAgent, transports, or runtime materialization; a receipt can therefore be
accepted and queried while its conversation is completely cold.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class TurnState(str, Enum):
    ACCEPTED = "ACCEPTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    INTERRUPTED_UNKNOWN = "INTERRUPTED_UNKNOWN"


TERMINAL_STATES = frozenset(
    {TurnState.SUCCEEDED, TurnState.FAILED, TurnState.INTERRUPTED, TurnState.INTERRUPTED_UNKNOWN}
)

_MAX_ERROR_DETAIL_CHARS = 1_024
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bBearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
        r"\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
)


def _safe_error_detail(detail: str) -> str:
    redacted = detail
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", redacted)
    return redacted[:_MAX_ERROR_DETAIL_CHARS]


class TurnLedgerError(RuntimeError):
    pass


class IdempotencyConflict(TurnLedgerError):
    def __init__(self, receipt: "TurnReceipt") -> None:
        super().__init__("client_turn_id was already used with a different request")
        self.receipt = receipt


class ConversationBusy(TurnLedgerError):
    def __init__(self, receipt: "TurnReceipt") -> None:
        super().__init__("conversation already has a non-terminal durable turn")
        self.receipt = receipt


class StaleWriterFence(TurnLedgerError):
    pass


@dataclass(frozen=True)
class WriterIdentity:
    writer_id: str
    pid: Optional[int] = None
    process_started_at: Optional[float] = None


@dataclass(frozen=True)
class WriterFence:
    conversation_id: str
    turn_id: str
    writer_id: str
    generation: int


@dataclass(frozen=True)
class TurnReceipt:
    turn_id: str
    conversation_id: str
    client_turn_id: str
    tip_session_id: str
    request_hash: str
    state: TurnState
    accepted_at: float
    started_at: Optional[float]
    updated_at: float
    completed_at: Optional[float]
    accepted_by_writer_id: str
    accepted_by_pid: Optional[int]
    accepted_by_process_started_at: Optional[float]
    writer_id: Optional[str]
    writer_pid: Optional[int]
    writer_process_started_at: Optional[float]
    writer_generation: Optional[int]
    error_code: Optional[str]
    error_detail: Optional[str]
    terminal_message_id: Optional[int]

    @property
    def accepted_by(self) -> WriterIdentity:
        return WriterIdentity(
            self.accepted_by_writer_id,
            pid=self.accepted_by_pid,
            process_started_at=self.accepted_by_process_started_at,
        )

    @property
    def writer(self) -> Optional[WriterIdentity]:
        if self.writer_id is None:
            return None
        return WriterIdentity(
            self.writer_id,
            pid=self.writer_pid,
            process_started_at=self.writer_process_started_at,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TurnReceipt":
        return cls(
            turn_id=str(row["turn_id"]),
            conversation_id=str(row["conversation_id"]),
            client_turn_id=str(row["client_turn_id"]),
            tip_session_id=str(row["tip_session_id"]),
            request_hash=str(row["request_hash"]),
            state=TurnState(row["state"]),
            accepted_at=float(row["accepted_at"]),
            started_at=float(row["started_at"]) if row.get("started_at") is not None else None,
            updated_at=float(row["updated_at"]),
            completed_at=float(row["completed_at"]) if row.get("completed_at") is not None else None,
            accepted_by_writer_id=str(row["accepted_by_writer_id"]),
            accepted_by_pid=int(row["accepted_by_pid"]) if row.get("accepted_by_pid") is not None else None,
            accepted_by_process_started_at=(
                float(row["accepted_by_process_started_at"])
                if row.get("accepted_by_process_started_at") is not None
                else None
            ),
            writer_id=row.get("writer_id"),
            writer_pid=int(row["writer_pid"]) if row.get("writer_pid") is not None else None,
            writer_process_started_at=(
                float(row["writer_process_started_at"])
                if row.get("writer_process_started_at") is not None
                else None
            ),
            writer_generation=int(row["writer_generation"]) if row.get("writer_generation") is not None else None,
            error_code=row.get("error_code"),
            error_detail=row.get("error_detail"),
            terminal_message_id=int(row["terminal_message_id"]) if row.get("terminal_message_id") is not None else None,
        )


@dataclass(frozen=True)
class AcceptResult:
    receipt: TurnReceipt
    deduplicated: bool


class TurnLedger:
    """Persistence-first authority for durable turn intent and state."""

    def __init__(
        self,
        db: Any,
        *,
        writer_id: str,
        clock: Callable[[], float] = time.time,
        pid: Optional[int] = None,
        process_started_at: Optional[float] = None,
        is_writer_alive: Optional[Callable[[WriterIdentity], bool]] = None,
    ) -> None:
        if not writer_id:
            raise ValueError("writer_id required")
        self._db = db
        self.writer = WriterIdentity(
            writer_id=writer_id,
            pid=os.getpid() if pid is None else pid,
            process_started_at=process_started_at,
        )
        self._clock = clock
        # Recovery is conservative by default: without an injected process
        # liveness authority, an owned turn is treated as live and left alone.
        self._is_writer_alive = is_writer_alive or (lambda _writer: True)

    def accept(
        self,
        conversation_id: str,
        tip_session_id: str,
        client_turn_id: str,
        request_hash: str,
    ) -> AcceptResult:
        if not all((conversation_id, tip_session_id, client_turn_id, request_hash)):
            raise ValueError("conversation_id, tip_session_id, client_turn_id and request_hash are required")
        now = float(self._clock())
        row = {
            "turn_id": uuid.uuid4().hex,
            "conversation_id": conversation_id,
            "client_turn_id": client_turn_id,
            "tip_session_id": tip_session_id,
            "request_hash": request_hash,
            "state": TurnState.ACCEPTED.value,
            "accepted_at": now,
            "updated_at": now,
            "accepted_by_writer_id": self.writer.writer_id,
            "accepted_by_pid": self.writer.pid,
            "accepted_by_process_started_at": self.writer.process_started_at,
        }
        outcome, persisted = self._db.accept_turn_receipt(row)
        receipt = TurnReceipt.from_row(persisted)
        if outcome == "conflict":
            raise IdempotencyConflict(receipt)
        if outcome == "busy":
            raise ConversationBusy(receipt)
        return AcceptResult(receipt=receipt, deduplicated=outcome == "duplicate")

    def get(self, turn_id: str) -> Optional[TurnReceipt]:
        row = self._db.get_turn_receipt(turn_id)
        return TurnReceipt.from_row(row) if row is not None else None

    def get_by_client_id(
        self, conversation_id: str, client_turn_id: str
    ) -> Optional[TurnReceipt]:
        row = self._db.get_turn_receipt_by_client_id(conversation_id, client_turn_id)
        return TurnReceipt.from_row(row) if row is not None else None

    def recover(self) -> list[TurnReceipt]:
        """Atomically recover durable turns and return receipts safe to reschedule."""
        rows = self._db.recover_turn_receipts(
            now=float(self._clock()),
            is_writer_alive=lambda writer_id, pid, started_at: self._is_writer_alive(
                WriterIdentity(writer_id, pid=pid, process_started_at=started_at)
            ),
        )
        return [TurnReceipt.from_row(row) for row in rows]

    def claim_start(
        self, turn_id: str, writer: Optional[WriterIdentity] = None
    ) -> Optional[tuple[TurnReceipt, WriterFence]]:
        owner = writer or self.writer
        claimed = self._db.claim_turn_start(
            turn_id,
            writer_id=owner.writer_id,
            writer_pid=owner.pid,
            writer_process_started_at=owner.process_started_at,
            now=float(self._clock()),
        )
        if claimed is None:
            return None
        row, generation = claimed
        receipt = TurnReceipt.from_row(row)
        return receipt, WriterFence(
            conversation_id=receipt.conversation_id,
            turn_id=receipt.turn_id,
            writer_id=owner.writer_id,
            generation=generation,
        )

    def is_current(self, fence: WriterFence) -> bool:
        return self._db.is_turn_fence_current(
            fence.conversation_id,
            fence.turn_id,
            fence.writer_id,
            fence.generation,
        )

    def _transition(
        self,
        fence: WriterFence,
        *,
        expected: tuple[TurnState, ...],
        target: TurnState,
        terminal: bool = False,
        tip_session_id: Optional[str] = None,
        terminal_message_id: Optional[int] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> TurnReceipt:
        now = float(self._clock())
        row = self._db.transition_turn_receipt(
            turn_id=fence.turn_id,
            conversation_id=fence.conversation_id,
            writer_id=fence.writer_id,
            generation=fence.generation,
            expected_states=tuple(state.value for state in expected),
            target_state=target.value,
            now=now,
            completed_at=now if terminal else None,
            tip_session_id=tip_session_id,
            terminal_message_id=terminal_message_id,
            error_code=error_code,
            error_detail=_safe_error_detail(error_detail) if error_detail is not None else None,
            clear_fence=terminal,
        )
        if row is None:
            raise StaleWriterFence(
                f"writer fence is stale or transition {expected!r}->{target.value} is invalid"
            )
        return TurnReceipt.from_row(row)

    def mark_running(self, fence: WriterFence) -> TurnReceipt:
        return self._transition(
            fence, expected=(TurnState.STARTING,), target=TurnState.RUNNING
        )

    def mark_waiting(self, fence: WriterFence) -> TurnReceipt:
        return self._transition(
            fence, expected=(TurnState.RUNNING,), target=TurnState.WAITING_INPUT
        )

    def resume_running(self, fence: WriterFence) -> TurnReceipt:
        return self._transition(
            fence, expected=(TurnState.WAITING_INPUT,), target=TurnState.RUNNING
        )

    def succeed(
        self,
        fence: WriterFence,
        *,
        tip_session_id: str,
        terminal_message_id: Optional[int] = None,
    ) -> TurnReceipt:
        return self._transition(
            fence,
            expected=(TurnState.RUNNING,),
            target=TurnState.SUCCEEDED,
            terminal=True,
            tip_session_id=tip_session_id,
            terminal_message_id=terminal_message_id,
        )

    def fail(self, fence: WriterFence, *, code: str, detail: str) -> TurnReceipt:
        return self._transition(
            fence,
            expected=(TurnState.STARTING, TurnState.RUNNING, TurnState.WAITING_INPUT),
            target=TurnState.FAILED,
            terminal=True,
            error_code=code,
            error_detail=detail,
        )

    def interrupt(self, fence: WriterFence, *, detail: Optional[str] = None) -> TurnReceipt:
        return self._transition(
            fence,
            expected=(TurnState.STARTING, TurnState.RUNNING, TurnState.WAITING_INPUT),
            target=TurnState.INTERRUPTED,
            terminal=True,
            error_code="INTERRUPTED",
            error_detail=detail,
        )
