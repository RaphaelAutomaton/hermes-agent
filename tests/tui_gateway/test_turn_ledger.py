"""Durable turn receipt contract for Session Runtime Manager."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_state import SessionDB
from tui_gateway.turn_ledger import (
    ConversationBusy,
    IdempotencyConflict,
    StaleWriterFence,
    TurnLedger,
    TurnState,
    WriterIdentity,
)


def test_accept_persists_receipt_before_runtime_exists(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="gateway-a")

    accepted = ledger.accept(
        conversation_id="conversation",
        tip_session_id="conversation",
        client_turn_id="client-turn-1",
        request_hash="sha256:one",
    )
    turn_id = accepted.receipt.turn_id
    db.close()

    reopened = SessionDB(db_path=path)
    persisted = TurnLedger(reopened, writer_id="gateway-b").get(turn_id)
    try:
        assert accepted.deduplicated is False
        assert persisted is not None
        assert persisted.turn_id == turn_id
        assert persisted.conversation_id == "conversation"
        assert persisted.client_turn_id == "client-turn-1"
        assert persisted.state.value == "ACCEPTED"
    finally:
        reopened.close()


def test_accept_deduplicates_same_client_key_and_hash(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="gateway-a")
    try:
        first = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        repeated = ledger.accept("conversation", "conversation", "client-1", "hash-1")

        assert repeated.deduplicated is True
        assert repeated.receipt.turn_id == first.receipt.turn_id
        assert repeated.receipt.state.value == "ACCEPTED"
    finally:
        db.close()


def test_accept_rejects_reused_key_with_different_request(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="gateway-a")
    try:
        first = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        with pytest.raises(IdempotencyConflict) as caught:
            ledger.accept("conversation", "conversation", "client-1", "hash-2")
        assert caught.value.receipt.turn_id == first.receipt.turn_id
    finally:
        db.close()


def test_accept_rejects_second_active_turn_in_same_conversation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="gateway-a")
    try:
        first = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        with pytest.raises(ConversationBusy) as caught:
            ledger.accept("conversation", "conversation", "client-2", "hash-2")
        assert caught.value.receipt.turn_id == first.receipt.turn_id
        assert ledger.get_by_client_id("conversation", "client-2") is None
    finally:
        db.close()


def test_claim_start_persists_writer_fence_and_starting_state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="acceptor", clock=lambda: 10.0)
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(
            accepted.receipt.turn_id,
            WriterIdentity("runner", pid=123, process_started_at=5.0),
        )

        assert claimed is not None
        receipt, fence = claimed
        assert receipt.state.value == "STARTING"
        assert receipt.started_at == 10.0
        assert fence.writer_id == "runner"
        assert fence.generation == 1
        assert ledger.is_current(fence) is True
    finally:
        db.close()


def test_receipt_exposes_complete_acceptor_and_owner_identities(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(
        db,
        writer_id="acceptor",
        pid=101,
        process_started_at=1.5,
        clock=lambda: 10.0,
    )
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        assert accepted.receipt.accepted_by == WriterIdentity(
            "acceptor", pid=101, process_started_at=1.5
        )
        assert accepted.receipt.writer is None

        claimed = ledger.claim_start(
            accepted.receipt.turn_id,
            WriterIdentity("runner", pid=202, process_started_at=2.5),
        )
        assert claimed is not None
        receipt, _fence = claimed
        assert receipt.accepted_by == WriterIdentity(
            "acceptor", pid=101, process_started_at=1.5
        )
        assert receipt.writer == WriterIdentity(
            "runner", pid=202, process_started_at=2.5
        )
    finally:
        db.close()


def test_recovery_keeps_accepted_turn_reschedulable_without_liveness_probe(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")

    def unexpected_probe(_writer):
        raise AssertionError("an ACCEPTED turn has no owner to probe")

    ledger = TurnLedger(
        db,
        writer_id="recovery-writer",
        clock=lambda: 20.0,
        is_writer_alive=unexpected_probe,
    )
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")

        reschedulable = ledger.recover()

        assert [receipt.turn_id for receipt in reschedulable] == [accepted.receipt.turn_id]
        assert reschedulable[0].state.value == "ACCEPTED"
        claimed = ledger.claim_start(accepted.receipt.turn_id)
        assert claimed is not None
        assert claimed[1].generation == 1
    finally:
        db.close()


def test_recovery_requeues_dead_starting_owner_and_next_claim_has_higher_generation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    seen = []
    ledger = TurnLedger(
        db,
        writer_id="recovery-writer",
        clock=lambda: 30.0,
        is_writer_alive=lambda writer: seen.append(writer) or False,
    )
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(
            accepted.receipt.turn_id,
            WriterIdentity("dead-runner", pid=222, process_started_at=2.0),
        )
        assert claimed is not None
        _starting, old_fence = claimed

        reschedulable = ledger.recover()

        assert seen == [WriterIdentity("dead-runner", pid=222, process_started_at=2.0)]
        assert len(reschedulable) == 1
        recovered = reschedulable[0]
        assert recovered.state.value == "ACCEPTED"
        assert recovered.writer is None
        assert recovered.writer_generation is None
        assert ledger.is_current(old_fence) is False

        reclaimed = ledger.claim_start(accepted.receipt.turn_id)
        assert reclaimed is not None
        _starting_again, new_fence = reclaimed
        assert new_fence.generation == old_fence.generation + 1

        with pytest.raises(StaleWriterFence):
            ledger.mark_running(old_fence)
        still_new_generation = ledger.get(accepted.receipt.turn_id)
        assert still_new_generation is not None
        assert still_new_generation.state is TurnState.STARTING
        assert still_new_generation.writer_generation == new_fence.generation
        assert ledger.mark_running(new_fence).state is TurnState.RUNNING
    finally:
        db.close()


@pytest.mark.parametrize("waiting", [False, True], ids=["running", "waiting-input"])
def test_recovery_terminalizes_dead_executing_owner_as_interrupted_unknown(
    tmp_path, waiting
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(
        db,
        writer_id="recovery-writer",
        clock=lambda: 40.0,
        is_writer_alive=lambda _writer: False,
    )
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(
            accepted.receipt.turn_id,
            WriterIdentity("dead-runner", pid=333, process_started_at=3.0),
        )
        assert claimed is not None
        _starting, fence = claimed
        ledger.mark_running(fence)
        if waiting:
            ledger.mark_waiting(fence)

        assert ledger.recover() == []

        recovered = ledger.get(accepted.receipt.turn_id)
        assert recovered is not None
        assert recovered.state is TurnState.INTERRUPTED_UNKNOWN
        assert recovered.completed_at == 40.0
        assert recovered.error_code == "INTERRUPTED_UNKNOWN"
        assert ledger.is_current(fence) is False
        with pytest.raises(StaleWriterFence):
            ledger.fail(fence, code="LATE", detail="late writer")
        assert ledger.get(accepted.receipt.turn_id) == recovered
    finally:
        db.close()


@pytest.mark.parametrize(
    "target_state", [TurnState.STARTING, TurnState.RUNNING, TurnState.WAITING_INPUT]
)
def test_recovery_leaves_live_writer_owned_turn_unchanged(tmp_path, target_state):
    db = SessionDB(db_path=tmp_path / f"{target_state.value}.db")
    db.create_session("conversation", "gateway")
    owner = WriterIdentity("live-runner", pid=444, process_started_at=4.0)
    seen = []
    ledger = TurnLedger(
        db,
        writer_id="recovery-writer",
        clock=lambda: 50.0,
        is_writer_alive=lambda writer: seen.append(writer) or True,
    )
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(accepted.receipt.turn_id, owner)
        assert claimed is not None
        before, fence = claimed
        if target_state in (TurnState.RUNNING, TurnState.WAITING_INPUT):
            before = ledger.mark_running(fence)
        if target_state is TurnState.WAITING_INPUT:
            before = ledger.mark_waiting(fence)

        assert ledger.recover() == []

        assert seen == [owner]
        assert ledger.get(accepted.receipt.turn_id) == before
        assert ledger.is_current(fence) is True
    finally:
        db.close()


def test_two_connections_recover_and_reclaim_with_one_higher_generation_owner(tmp_path):
    path = tmp_path / "state.db"
    db_a = SessionDB(db_path=path)
    db_a.create_session("conversation", "gateway")
    db_b = SessionDB(db_path=path)
    ledger_a = TurnLedger(
        db_a,
        writer_id="recovery-a",
        pid=501,
        process_started_at=5.1,
        clock=lambda: 70.0,
        is_writer_alive=lambda _writer: False,
    )
    ledger_b = TurnLedger(
        db_b,
        writer_id="recovery-b",
        pid=502,
        process_started_at=5.2,
        clock=lambda: 70.0,
        is_writer_alive=lambda _writer: False,
    )
    try:
        accepted = ledger_a.accept(
            "conversation", "conversation", "client-1", "hash-1"
        )
        claimed = ledger_a.claim_start(
            accepted.receipt.turn_id,
            WriterIdentity("dead-runner", pid=500, process_started_at=5.0),
        )
        assert claimed is not None
        _starting, old_fence = claimed

        recover_barrier = threading.Barrier(2)

        def recover(ledger):
            recover_barrier.wait()
            return ledger.recover()

        with ThreadPoolExecutor(max_workers=2) as pool:
            recovered = list(pool.map(recover, (ledger_a, ledger_b)))

        assert any(
            receipt.turn_id == accepted.receipt.turn_id
            for batch in recovered
            for receipt in batch
        )
        after_recovery = ledger_a.get(accepted.receipt.turn_id)
        assert after_recovery is not None
        assert after_recovery.state is TurnState.ACCEPTED
        assert after_recovery.writer is None

        claim_barrier = threading.Barrier(2)

        def reclaim(ledger):
            claim_barrier.wait()
            return ledger.claim_start(accepted.receipt.turn_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(reclaim, (ledger_a, ledger_b)))

        winners = [result for result in claims if result is not None]
        assert len(winners) == 1
        _receipt, winning_fence = winners[0]
        assert winning_fence.generation == old_fence.generation + 1
        with pytest.raises(StaleWriterFence):
            ledger_a.mark_running(old_fence)
        assert ledger_a.mark_running(winning_fence).state is TurnState.RUNNING
    finally:
        db_b.close()
        db_a.close()


def test_error_detail_is_redacted_before_bounded_persistence(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="runner")
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(accepted.receipt.turn_id)
        assert claimed is not None
        _starting, fence = claimed
        detail = (
            "Authorization: Bearer sk-obvious-secret "
            "api_key=another-obvious-secret password=hunter2 "
            + "x" * 2_000
        )

        failed = ledger.fail(fence, code="PROVIDER_ERROR", detail=detail)
        persisted = ledger.get(accepted.receipt.turn_id)

        assert failed == persisted
        assert persisted is not None
        assert persisted.error_detail is not None
        assert "[REDACTED]" in persisted.error_detail
        assert "sk-obvious-secret" not in persisted.error_detail
        assert "another-obvious-secret" not in persisted.error_detail
        assert "hunter2" not in persisted.error_detail
        assert len(persisted.error_detail) <= 1_024
    finally:
        db.close()


def test_waiting_running_cycle_rejects_invalid_transitions_and_terminal_is_immutable(
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="runner", clock=lambda: 60.0)
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        claimed = ledger.claim_start(accepted.receipt.turn_id)
        assert claimed is not None
        _starting, fence = claimed

        with pytest.raises(StaleWriterFence):
            ledger.succeed(fence, tip_session_id="conversation")
        assert ledger.mark_running(fence).state is TurnState.RUNNING
        with pytest.raises(StaleWriterFence):
            ledger.resume_running(fence)

        assert ledger.mark_waiting(fence).state is TurnState.WAITING_INPUT
        with pytest.raises(StaleWriterFence):
            ledger.mark_waiting(fence)
        assert ledger.resume_running(fence).state is TurnState.RUNNING
        assert ledger.mark_waiting(fence).state is TurnState.WAITING_INPUT
        assert ledger.resume_running(fence).state is TurnState.RUNNING

        terminal = ledger.succeed(fence, tip_session_id="conversation")
        assert terminal.state is TurnState.SUCCEEDED
        invalid_terminal_mutations = (
            lambda: ledger.mark_running(fence),
            lambda: ledger.mark_waiting(fence),
            lambda: ledger.resume_running(fence),
            lambda: ledger.fail(fence, code="LATE", detail="late failure"),
            lambda: ledger.interrupt(fence, detail="late interrupt"),
        )
        for mutate in invalid_terminal_mutations:
            with pytest.raises(StaleWriterFence):
                mutate()
            assert ledger.get(accepted.receipt.turn_id) == terminal
    finally:
        db.close()


def test_terminal_transition_is_committed_and_fence_becomes_inactive(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", "gateway")
    ledger = TurnLedger(db, writer_id="runner", clock=lambda: 20.0)
    try:
        accepted = ledger.accept("conversation", "conversation", "client-1", "hash-1")
        _, fence = ledger.claim_start(accepted.receipt.turn_id)
        running = ledger.mark_running(fence)
        succeeded = ledger.succeed(
            fence,
            tip_session_id="conversation",
            terminal_message_id=42,
        )

        assert running.state.value == "RUNNING"
        assert succeeded.state.value == "SUCCEEDED"
        assert succeeded.completed_at == 20.0
        assert succeeded.terminal_message_id == 42
        assert ledger.is_current(fence) is False
        assert ledger.claim_start(accepted.receipt.turn_id) is None
    finally:
        db.close()
