import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spanvouch.adapters.frameworks.langgraph_review import (
    LangGraphReviewWorkflow,
)
from spanvouch.adapters.storage.sqlite import SQLiteReviewRepository
from spanvouch.contracts.diagnosis import DiagnoserKind
from spanvouch.contracts.review import (
    DiagnosisReviewCase,
    ReviewStatus,
)
from spanvouch.contracts.verification import (
    VerificationMode,
    VerifierKind,
    VerifierVerdict,
)
from spanvouch.contracts.versioning import canonical_json
from spanvouch.diagnosis.engine import DiagnosisEngine
from spanvouch.diagnosis.errors import (
    ProviderConfigurationError,
    ProviderProtocolError,
    ProviderRequestError,
)
from spanvouch.diagnosis.rule_diagnoser import RuleDiagnoser
from spanvouch.review.application import ReviewApplication
from spanvouch.review.commands import RouteCappedRevisionToHuman
from spanvouch.review.errors import ReviewConflictError, ReviewWorkflowProviderError
from spanvouch.verification.invariant_engine import InvariantEngine
from tests.adapters.frameworks.test_langgraph_review import (
    FakeReviser,
    FakeVerifier,
    MutableClock,
    SequenceIds,
    _create_case,
    _deepseek_report,
    _events,
    _report,
    _workflow,
)
from tests.review.factories import NOW


class CoordinatedCappedRouteRepository(SQLiteReviewRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.route_calls = 0
        self.both_routes_entered = asyncio.Event()

    async def route_capped_revision_to_human(
        self, command: RouteCappedRevisionToHuman
    ) -> DiagnosisReviewCase:
        self.route_calls += 1
        if self.route_calls == 2:
            self.both_routes_entered.set()
        await asyncio.wait_for(self.both_routes_entered.wait(), timeout=1.0)
        return await super().route_capped_revision_to_human(command)


async def _seed_capped_revision_state(
    database: Path,
    repository: SQLiteReviewRepository,
    *,
    status: ReviewStatus,
) -> None:
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="capped-initial",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=1,
                suffix="capped-final",
            ),
        ],
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[_deepseek_report()],
    )
    detail = await _workflow(
        repository,
        deterministic,
        reviser=reviser,
    ).run("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert detail.case.current_revision_number == 1
    assert detail.case.evidence_revision_count == 1

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM workflow_events WHERE case_id = ? AND event_type = ?",
            ("case-review-1", "awaiting_human_review"),
        )
        connection.execute(
            "UPDATE workflow_events SET event_type = ?, to_status = ? "
            "WHERE case_id = ? AND event_sequence = "
            "(SELECT MAX(event_sequence) FROM workflow_events WHERE case_id = ?)",
            (
                "revision_requested",
                ReviewStatus.REVISION_REQUESTED.value,
                "case-review-1",
                "case-review-1",
            ),
        )
        version = 6
        if status is ReviewStatus.REVISING:
            version = 7
            connection.execute(
                "INSERT INTO workflow_events("
                "event_id, case_id, event_sequence, event_type, from_status, to_status, "
                "case_version, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "event-capped-revision-started",
                    "case-review-1",
                    7,
                    "revision_started",
                    ReviewStatus.REVISION_REQUESTED.value,
                    ReviewStatus.REVISING.value,
                    version,
                    canonical_json({"source": "capped-recovery-test"}),
                    NOW.isoformat(),
                ),
            )
        connection.execute(
            "UPDATE review_cases SET status = ?, version = ?, lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ? WHERE case_id = ?",
            (status.value, version, NOW.isoformat(), "case-review-1"),
        )
        connection.commit()


@pytest.mark.parametrize(
    "capped_status",
    (ReviewStatus.REVISION_REQUESTED, ReviewStatus.REVISING),
)
async def test_capped_revision_race_routes_once_without_provider_or_second_revision(
    tmp_path: Path,
    capped_status: ReviewStatus,
) -> None:
    database = tmp_path / f"capped-{capped_status.value}.sqlite3"
    repository = CoordinatedCappedRouteRepository(database)
    await repository.initialize()
    await _seed_capped_revision_state(database, repository, status=capped_status)
    provider = FakeVerifier(VerifierKind.DETERMINISTIC, [])
    reviser = FakeReviser(supported=(DiagnoserKind.DEEPSEEK,), outcomes=[])
    route_ids = iter(("capped-route-1", "capped-route-2"))

    def id_factory() -> str:
        return next(route_ids)
    first = _workflow(
        repository,
        provider,
        reviser=reviser,
        id_factory=id_factory,
        lease_owner="capped-first",
    )
    second = _workflow(
        repository,
        provider,
        reviser=reviser,
        id_factory=id_factory,
        lease_owner="capped-second",
    )

    first_detail, second_detail = await asyncio.gather(
        first.resume("case-review-1"),
        second.resume("case-review-1"),
    )

    assert first_detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert second_detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert provider.inputs == []
    assert reviser.calls == []
    assert len(first_detail.revisions) == 2
    assert repository.route_calls == 2
    assert [event for event, _ in _events(database)].count("awaiting_human_review") == 1


async def test_capped_revising_state_does_not_steal_an_active_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "capped-active-lease.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _seed_capped_revision_state(
        database,
        repository,
        status=ReviewStatus.REVISING,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_cases SET lease_owner = ?, lease_expires_at = ? "
            "WHERE case_id = ?",
            (
                "still-active-worker",
                (NOW + timedelta(minutes=1)).isoformat(),
                "case-review-1",
            ),
        )
        connection.commit()
    provider = FakeVerifier(VerifierKind.DETERMINISTIC, [])
    reviser = FakeReviser(supported=(DiagnoserKind.DEEPSEEK,), outcomes=[])

    with pytest.raises(ReviewConflictError, match="review work lease is active"):
        await _workflow(
            repository,
            provider,
            reviser=reviser,
            clock=MutableClock(),
            id_factory=lambda: "active-capped-route",
        ).resume("case-review-1")

    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.REVISING
    assert provider.inputs == []
    assert reviser.calls == []
    assert [event for event, _ in _events(database)].count("awaiting_human_review") == 0


class BlockingSemanticVerifier:
    kind = VerifierKind.SEMANTIC
    version_fingerprint = "blocking-semantic-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def verify(self, input_):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return _report(
            VerifierKind.SEMANTIC,
            VerifierVerdict.VERIFIED,
            revision_number=input_.revision_number,
            suffix=f"blocked-semantic-{self.calls}",
        ).model_copy(update={"report_sha256": input_.report_sha256})


class BlockingReviser(FakeReviser):
    def __init__(self) -> None:
        super().__init__(supported=(DiagnoserKind.DEEPSEEK,))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def revise(self, runtime_bundle, evidence_gaps):  # type: ignore[no-untyped-def]
        self.calls.append((runtime_bundle, evidence_gaps))
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return _deepseek_report()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _resume_service(
    repository: SQLiteReviewRepository,
    workflow: LangGraphReviewWorkflow,
    deterministic: FakeVerifier,
    ids: SequenceIds,
) -> ReviewApplication:
    engine = InvariantEngine(())
    diagnosis_service = DiagnosisEngine({DiagnoserKind.RULES: RuleDiagnoser(engine)})
    return ReviewApplication(
        diagnosis_service=diagnosis_service,
        repository=repository,
        workflow=workflow,
        deterministic_verifier=deterministic,
        id_factory=ids,
        clock=_utc_now,
    )


class CrashAfterAppendRepository(SQLiteReviewRepository):
    def __init__(
        self,
        database: Path,
        *,
        crash_verifier_once: bool = False,
        crash_revision_once: bool = False,
    ) -> None:
        super().__init__(database)
        self.crash_verifier_once = crash_verifier_once
        self.crash_revision_once = crash_revision_once

    async def append_verifier_run(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_verifier_run(command)
        if self.crash_verifier_once:
            self.crash_verifier_once = False
            raise RuntimeError("crash after verifier commit")
        return result

    async def append_revision(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_revision(command)
        if self.crash_revision_once:
            self.crash_revision_once = False
            raise RuntimeError("crash after revision commit")
        return result


class ClearedLeaseRaceRepository(SQLiteReviewRepository):
    """Force a renewal to observe the lease-clearing finalization commit."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.renew_entered = asyncio.Event()
        self.finalized = asyncio.Event()
        self.conflict_observed = asyncio.Event()
        self.active_renewals = 0

    async def renew_review_lease(self, command):  # type: ignore[no-untyped-def]
        self.active_renewals += 1
        self.renew_entered.set()
        try:
            await asyncio.wait_for(self.finalized.wait(), timeout=1.0)
            try:
                await super().renew_review_lease(command)
            except ReviewConflictError:
                self.conflict_observed.set()
                raise
        finally:
            self.active_renewals -= 1

    async def append_verifier_run(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_verifier_run(command)
        if command.report.verifier_kind == VerifierKind.SEMANTIC:
            self.finalized.set()
            await asyncio.wait_for(self.conflict_observed.wait(), timeout=1.0)
        return result

    async def append_revision(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_revision(command)
        self.finalized.set()
        await asyncio.wait_for(self.conflict_observed.wait(), timeout=1.0)
        return result

    async def route_revision_failure(self, command):  # type: ignore[no-untyped-def]
        result = await super().route_revision_failure(command)
        self.finalized.set()
        await asyncio.wait_for(self.conflict_observed.wait(), timeout=1.0)
        return result

    async def finalize_semantic_failure(self, command):  # type: ignore[no-untyped-def]
        result = await super().finalize_semantic_failure(command)
        self.finalized.set()
        await asyncio.wait_for(self.conflict_observed.wait(), timeout=1.0)
        return result

    async def load_runtime(self, case_id: str):  # type: ignore[no-untyped-def]
        if self.finalized.is_set() and not self.conflict_observed.is_set():
            await asyncio.wait_for(self.conflict_observed.wait(), timeout=1.0)
        return await super().load_runtime(case_id)


class HeartbeatAwareVerifier(FakeVerifier):
    def __init__(
        self,
        kind: VerifierKind,
        outcomes: list[object],
        repository: ClearedLeaseRaceRepository,
    ) -> None:
        super().__init__(kind, outcomes)
        self._race_repository = repository

    async def verify(self, input_):  # type: ignore[no-untyped-def]
        await asyncio.wait_for(self._race_repository.renew_entered.wait(), timeout=1.0)
        return await super().verify(input_)


class HeartbeatAwareReviser(FakeReviser):
    def __init__(
        self,
        repository: ClearedLeaseRaceRepository,
        *,
        outcomes: list[object],
    ) -> None:
        super().__init__(supported=(DiagnoserKind.DEEPSEEK,), outcomes=outcomes)
        self._race_repository = repository

    async def revise(self, runtime_bundle, evidence_gaps):  # type: ignore[no-untyped-def]
        await asyncio.wait_for(self._race_repository.renew_entered.wait(), timeout=1.0)
        return await super().revise(runtime_bundle, evidence_gaps)


class PostCommitPauseRepository(SQLiteReviewRepository):
    """Pause the original worker after SQLite commits its provider effect."""

    def __init__(
        self,
        database: Path,
        *,
        pause_semantic: bool = False,
        pause_revision: bool = False,
    ) -> None:
        super().__init__(database)
        self.pause_semantic = pause_semantic
        self.pause_revision = pause_revision
        self.provider_effect_committed = asyncio.Event()
        self.release_original = asyncio.Event()

    async def append_verifier_run(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_verifier_run(command)
        if self.pause_semantic and command.report.verifier_kind == VerifierKind.SEMANTIC:
            self.provider_effect_committed.set()
            await asyncio.wait_for(self.release_original.wait(), timeout=2.0)
        return result

    async def append_revision(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_revision(command)
        if self.pause_revision:
            self.provider_effect_committed.set()
            await asyncio.wait_for(self.release_original.wait(), timeout=2.0)
        return result


class FinalVerificationClaimConflictRepository(SQLiteReviewRepository):
    """Raise before the same worker can mutate final-verification state."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.verification_claims = 0

    async def claim_work(self, command):  # type: ignore[no-untyped-def]
        if command.event_type.value == "verification_started":
            self.verification_claims += 1
            if self.verification_claims == 2:
                raise ReviewConflictError("injected unchanged final claim conflict")
        return await super().claim_work(command)


class FinalVerifierEventConflictRepository(SQLiteReviewRepository):
    """Roll back the final verifier append when its event insert fails."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self._fail_next_workflow_event = False

    async def append_revision(self, command):  # type: ignore[no-untyped-def]
        result = await super().append_revision(command)
        self._fail_next_workflow_event = True
        return result

    def _after_insert(self, stage: str) -> None:
        if self._fail_next_workflow_event and stage == "workflow_event":
            self._fail_next_workflow_event = False
            raise ReviewConflictError("injected final verifier event conflict")
        super()._after_insert(stage)


class InvalidBindingFinalVerifier(FakeVerifier):
    async def verify(self, input_):  # type: ignore[no-untyped-def]
        report = await super().verify(input_)
        if input_.revision_number == 1:
            return report.model_copy(update={"report_sha256": "f" * 64})
        return report


class BlockingFinalVerifier(FakeVerifier):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__(VerifierKind.DETERMINISTIC, outcomes)
        self.final_entered = asyncio.Event()
        self.release_final = asyncio.Event()

    async def verify(self, input_):  # type: ignore[no-untyped-def]
        if input_.revision_number == 1:
            self.final_entered.set()
            await self.release_final.wait()
        return await super().verify(input_)


async def test_crash_after_verifying_commit_requires_expired_lease_before_resume(
    tmp_path: Path,
) -> None:
    database = tmp_path / "verify-crash.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.RULES,
    )
    clock = MutableClock()
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            RuntimeError("process crashed during provider call"),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="recovered",
            ),
        ],
    )
    workflow = _workflow(repository, verifier, clock=clock)

    with pytest.raises(RuntimeError, match="process crashed"):
        await workflow.run("case-review-1")
    crashed = await repository.get_detail("case-review-1")
    assert crashed.case.status is ReviewStatus.VERIFYING
    assert not crashed.verifier_reports

    clock.now += timedelta(seconds=10)
    with pytest.raises(ReviewConflictError, match="lease is still active"):
        await workflow.resume("case-review-1")
    assert len(verifier.inputs) == 1

    clock.now += timedelta(seconds=21)
    recovered = await workflow.resume("case-review-1")
    assert recovered.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(verifier.inputs) == 2


async def test_crash_after_revising_commit_recovers_once_after_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "revision-crash.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    clock = MutableClock()
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="needs-revision",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="revised",
            ),
        ],
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[RuntimeError("process crashed during revision"), _deepseek_report()],
    )
    workflow = _workflow(repository, verifier, reviser=reviser, clock=clock)

    with pytest.raises(RuntimeError, match="process crashed"):
        await workflow.run("case-review-1")
    assert (await repository.get_detail("case-review-1")).case.status is ReviewStatus.REVISING

    clock.now += timedelta(seconds=31)
    recovered = await workflow.resume("case-review-1")
    assert recovered.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert recovered.case.current_revision_number == 1
    assert len(reviser.calls) == 2


async def test_pending_resume_succeeds_but_human_and_terminal_resume_do_not_call_provider(
    tmp_path: Path,
) -> None:
    repository = SQLiteReviewRepository(tmp_path / "resume.sqlite3")
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.RULES,
    )
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="pending-resume",
            )
        ],
    )
    workflow = _workflow(repository, verifier)

    detail = await workflow.resume("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    with pytest.raises(ReviewConflictError, match="cannot be resumed"):
        await workflow.resume("case-review-1")
    assert len(verifier.inputs) == 1

    with sqlite3.connect(tmp_path / "resume.sqlite3") as connection:
        connection.execute(
            "UPDATE review_cases SET status = 'confirmed', terminal_decision_id = 'manual' "
            "WHERE case_id = 'case-review-1'"
        )
        connection.execute(
            "INSERT INTO human_decisions(decision_id, case_id, action, reviewer_label, "
            "expected_version, created_at) VALUES "
            "('manual', 'case-review-1', 'confirm', 'test', 3, ?)",
            (detail.case.updated_at.isoformat(),),
        )
        connection.commit()
    with pytest.raises(ReviewConflictError, match="cannot be resumed"):
        await workflow.resume("case-review-1")
    assert len(verifier.inputs) == 1


async def test_restart_after_committed_verifier_effect_does_not_repeat_provider(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart-verifier.sqlite3"
    crashing_repository = CrashAfterAppendRepository(database, crash_verifier_once=True)
    await crashing_repository.initialize()
    await _create_case(
        crashing_repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.RULES,
    )
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="committed",
            )
        ],
    )
    ids = SequenceIds()

    with pytest.raises(RuntimeError, match="after verifier commit"):
        await _workflow(crashing_repository, verifier, id_factory=ids).run("case-review-1")
    restarted_repository = SQLiteReviewRepository(database)
    restarted = await _workflow(restarted_repository, verifier, id_factory=ids).resume(
        "case-review-1"
    )

    assert restarted.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(verifier.inputs) == 1
    assert len(restarted.verifier_reports) == 1
    assert [event for event, _ in _events(database)].count("verification_completed") == 1


async def test_restart_after_committed_revision_does_not_repeat_reviser(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart-revision.sqlite3"
    repository = CrashAfterAppendRepository(database, crash_revision_once=True)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="revision-needed",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="revision-recovered",
            ),
        ],
    )
    reviser = FakeReviser(supported=(DiagnoserKind.DEEPSEEK,), outcomes=[_deepseek_report()])
    ids = SequenceIds()

    with pytest.raises(RuntimeError, match="after revision commit"):
        await _workflow(repository, verifier, reviser=reviser, id_factory=ids).run("case-review-1")
    restarted_repository = SQLiteReviewRepository(database)
    detail = await _workflow(
        restarted_repository, verifier, reviser=reviser, id_factory=ids
    ).resume("case-review-1")

    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(reviser.calls) == 1
    assert len(detail.revisions) == 2
    assert [event for event, _ in _events(database)].count("revision_completed") == 1


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    [
        (ProviderConfigurationError("TOP-SECRET"), "provider_not_configured", False),
        (ProviderProtocolError("TOP-SECRET"), "provider_protocol_error", False),
        (ProviderRequestError("transport_error", retryable=True), "transport_error", True),
        (ProviderRequestError("TOP-SECRET", retryable=False), "provider_request_error", False),
    ],
)
async def test_semantic_provider_failure_is_persisted_before_typed_error(
    tmp_path: Path,
    error: Exception,
    expected_code: str,
    retryable: bool,
) -> None:
    database = tmp_path / f"semantic-{expected_code}.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(repository, mode=VerificationMode.HYBRID, diagnoser=DiagnoserKind.DEEPSEEK)
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix=f"det-{expected_code}",
            )
        ],
    )
    semantic = FakeVerifier(VerifierKind.SEMANTIC, [error])

    with pytest.raises(ReviewWorkflowProviderError) as raised:
        await _workflow(repository, deterministic, semantic=semantic).run("case-review-1")

    assert raised.value.case_id == "case-review-1"
    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    semantic_report = detail.verifier_reports[-1]
    assert semantic_report.operational_error is not None
    assert semantic_report.operational_error.code == expected_code
    assert "TOP-SECRET" not in semantic_report.model_dump_json()
    assert [event for event, _ in _events(database)][-2:] == [
        "provider_failed",
        "awaiting_human_review",
    ]


async def test_missing_semantic_verifier_persists_configuration_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-missing.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(repository, mode=VerificationMode.HYBRID, diagnoser=DiagnoserKind.DEEPSEEK)
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="det-missing-semantic",
            )
        ],
    )

    with pytest.raises(ReviewWorkflowProviderError) as raised:
        await _workflow(repository, deterministic, semantic=None).run("case-review-1")

    assert raised.value.case_id == "case-review-1"
    assert raised.value.code == "provider_not_configured"
    assert raised.value.retryable is False
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(detail.verifier_reports) == 2
    semantic_report = detail.verifier_reports[-1]
    assert semantic_report.verifier_kind == VerifierKind.SEMANTIC
    assert semantic_report.operational_error is not None
    assert semantic_report.operational_error.code == "provider_not_configured"
    assert [event for event, _ in _events(database)][-2:] == [
        "provider_failed",
        "awaiting_human_review",
    ]


@pytest.mark.parametrize(
    ("provider_error", "expected_code", "expected_retryable"),
    (
        (
            ProviderRequestError("transport_error", retryable=True),
            "transport_error",
            True,
        ),
        (
            ProviderProtocolError("TOP-SECRET"),
            "provider_protocol_error",
            False,
        ),
    ),
)
async def test_revision_provider_failure_preserves_classification_without_fabricated_effects(
    tmp_path: Path,
    provider_error: Exception,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    database = tmp_path / f"revision-provider-{expected_code}.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    verifier = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="revision-provider",
            )
        ],
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[provider_error],
    )

    with pytest.raises(ReviewWorkflowProviderError) as raised:
        await _workflow(repository, verifier, reviser=reviser).run("case-review-1")

    assert raised.value.code == expected_code
    assert raised.value.retryable is expected_retryable
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert detail.case.composite_verdict is VerifierVerdict.REVIEW_REQUIRED
    assert len(detail.revisions) == 1
    assert len(detail.verifier_reports) == 1
    with sqlite3.connect(database) as connection:
        event = connection.execute(
            "SELECT event_type, metadata_json FROM workflow_events "
            "ORDER BY event_sequence DESC LIMIT 1"
        ).fetchone()
    assert event is not None
    assert event[0] == "revision_provider_failed"
    assert f'"code":"{expected_code}"' in event[1]
    assert f'"retryable":{str(expected_retryable).lower()}' in event[1]
    assert "TOP-SECRET" not in event[1]


async def test_semantic_finalize_survives_heartbeat_observing_cleared_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-finalize-race.sqlite3"
    repository = ClearedLeaseRaceRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="semantic-finalize-race",
            )
        ],
    )
    semantic = HeartbeatAwareVerifier(
        VerifierKind.SEMANTIC,
        [
            _report(
                VerifierKind.SEMANTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="semantic-finalize-race-provider",
            )
        ],
        repository,
    )

    detail = await _workflow(
        repository,
        deterministic,
        semantic=semantic,
        clock=_utc_now,
        lease_duration=timedelta(seconds=1),
    ).run("case-review-1")

    assert repository.conflict_observed.is_set()
    assert repository.active_renewals == 0
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(detail.verifier_reports) == 2


async def test_revision_finalize_survives_heartbeat_observing_cleared_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "revision-finalize-race.sqlite3"
    repository = ClearedLeaseRaceRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="revision-finalize-race",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="revision-finalize-race-final",
            ),
        ],
    )
    reviser = HeartbeatAwareReviser(repository, outcomes=[_deepseek_report()])

    detail = await _workflow(
        repository,
        deterministic,
        reviser=reviser,
        clock=_utc_now,
        lease_duration=timedelta(seconds=1),
    ).run("case-review-1")

    assert repository.conflict_observed.is_set()
    assert repository.active_renewals == 0
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert detail.case.current_revision_number == 1
    assert len(detail.revisions) == 2


async def test_semantic_postcommit_race_converges_without_recalling_provider(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-postcommit-convergence.sqlite3"
    repository = PostCommitPauseRepository(database, pause_semantic=True)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = BlockingFinalVerifier(
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="semantic-postcommit-deterministic",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="semantic-postcommit-final-deterministic",
            ),
        ],
    )
    semantic = FakeVerifier(
        VerifierKind.SEMANTIC,
        [
            _report(
                VerifierKind.SEMANTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="semantic-postcommit-provider",
            ),
            _report(
                VerifierKind.SEMANTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="semantic-postcommit-final-provider",
            ),
        ],
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[_deepseek_report()],
    )
    ids = SequenceIds()
    lease_tokens = iter(("semantic-origin", "semantic-resumer"))
    workflow = _workflow(
        repository,
        deterministic,
        semantic=semantic,
        reviser=reviser,
        id_factory=ids,
        lease_owner="semantic-singleton",
        lease_token_factory=lambda: next(lease_tokens),
    )

    original_task = asyncio.create_task(workflow.run("case-review-1"))
    await asyncio.wait_for(repository.provider_effect_committed.wait(), timeout=1.0)
    competitor_task = asyncio.create_task(workflow.resume("case-review-1"))
    await asyncio.wait_for(deterministic.final_entered.wait(), timeout=1.0)
    competing_runtime = await repository.load_runtime("case-review-1")
    assert competing_runtime.lease_owner == "semantic-singleton:semantic-resumer"
    repository.release_original.set()
    original_detail = await asyncio.wait_for(original_task, timeout=1.0)
    deterministic.release_final.set()
    competitor_detail = await asyncio.wait_for(competitor_task, timeout=1.0)

    assert competitor_detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert original_detail.case.status is ReviewStatus.VERIFYING
    assert len(semantic.inputs) == 2
    assert len(deterministic.inputs) == 2
    assert len(reviser.calls) == 1


async def test_revision_postcommit_race_converges_without_recalling_reviser(
    tmp_path: Path,
) -> None:
    database = tmp_path / "revision-postcommit-convergence.sqlite3"
    repository = PostCommitPauseRepository(database, pause_revision=True)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = BlockingFinalVerifier(
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="revision-postcommit-initial",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="revision-postcommit-final",
            ),
        ],
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[_deepseek_report()],
    )
    ids = SequenceIds()
    lease_tokens = iter(("revision-origin", "revision-resumer"))
    workflow = _workflow(
        repository,
        deterministic,
        reviser=reviser,
        id_factory=ids,
        lease_owner="revision-singleton",
        lease_token_factory=lambda: next(lease_tokens),
    )

    original_task = asyncio.create_task(workflow.run("case-review-1"))
    await asyncio.wait_for(repository.provider_effect_committed.wait(), timeout=1.0)
    competitor_task = asyncio.create_task(workflow.resume("case-review-1"))
    await asyncio.wait_for(deterministic.final_entered.wait(), timeout=1.0)
    competing_runtime = await repository.load_runtime("case-review-1")
    assert competing_runtime.lease_owner == "revision-singleton:revision-resumer"
    repository.release_original.set()
    original_detail = await asyncio.wait_for(original_task, timeout=1.0)
    deterministic.release_final.set()
    competitor_detail = await asyncio.wait_for(competitor_task, timeout=1.0)

    assert competitor_detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert original_detail.case.status is ReviewStatus.VERIFYING
    assert original_detail.case.current_revision_number == 1
    assert len(reviser.calls) == 1
    assert len(deterministic.inputs) == 2


@pytest.mark.parametrize(
    "failure_kind",
    ("wrong_kind", "invalid_binding", "event_rollback", "same_owner_unchanged"),
)
async def test_revision_postcommit_does_not_hide_genuine_final_verification_conflicts(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    database = tmp_path / f"revision-genuine-conflict-{failure_kind}.sqlite3"
    repository: SQLiteReviewRepository
    if failure_kind == "event_rollback":
        repository = FinalVerifierEventConflictRepository(database)
    elif failure_kind == "same_owner_unchanged":
        repository = FinalVerificationClaimConflictRepository(database)
    else:
        repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    initial = _report(
        VerifierKind.DETERMINISTIC,
        VerifierVerdict.NEEDS_EVIDENCE,
        revision_number=0,
        suffix=f"genuine-conflict-initial-{failure_kind}",
    )
    final_kind = (
        VerifierKind.SEMANTIC
        if failure_kind == "wrong_kind"
        else VerifierKind.DETERMINISTIC
    )
    final = _report(
        final_kind,
        VerifierVerdict.VERIFIED,
        revision_number=1,
        suffix=f"genuine-conflict-final-{failure_kind}",
    )
    verifier_type = (
        InvalidBindingFinalVerifier
        if failure_kind == "invalid_binding"
        else FakeVerifier
    )
    deterministic = verifier_type(VerifierKind.DETERMINISTIC, [initial, final])
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[_deepseek_report()],
    )

    with pytest.raises(ReviewConflictError):
        await _workflow(repository, deterministic, reviser=reviser).run(
            "case-review-1"
        )

    durable = await repository.get_detail("case-review-1")
    assert durable.case.status is ReviewStatus.VERIFYING
    assert durable.case.current_revision_number == 1
    assert len(reviser.calls) == 1


async def test_revision_postcommit_accepts_only_other_workers_active_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "revision-external-active-claim.sqlite3"
    repository = PostCommitPauseRepository(database, pause_revision=True)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = BlockingFinalVerifier(
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="external-claim-initial",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="external-claim-final",
            ),
        ]
    )
    reviser = FakeReviser(
        supported=(DiagnoserKind.DEEPSEEK,),
        outcomes=[_deepseek_report()],
    )
    ids = SequenceIds()
    original = _workflow(
        repository,
        deterministic,
        reviser=reviser,
        id_factory=ids,
        lease_owner="external-claim-original",
    )
    competitor = _workflow(
        repository,
        deterministic,
        reviser=reviser,
        id_factory=ids,
        lease_owner="external-claim-competitor",
    )

    original_task = asyncio.create_task(original.run("case-review-1"))
    await asyncio.wait_for(repository.provider_effect_committed.wait(), timeout=1.0)
    competitor_task = asyncio.create_task(competitor.resume("case-review-1"))
    await asyncio.wait_for(deterministic.final_entered.wait(), timeout=1.0)
    repository.release_original.set()
    original_detail = await asyncio.wait_for(original_task, timeout=1.0)

    assert original_detail.case.status is ReviewStatus.VERIFYING
    assert len(reviser.calls) == 1
    deterministic.release_final.set()
    competitor_detail = await asyncio.wait_for(competitor_task, timeout=1.0)
    assert competitor_detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(reviser.calls) == 1


async def test_semantic_failure_route_is_atomic_and_survives_cleared_lease_race(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-failure-race.sqlite3"
    repository = ClearedLeaseRaceRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="semantic-failure-race",
            )
        ],
    )
    semantic = HeartbeatAwareVerifier(
        VerifierKind.SEMANTIC,
        [ProviderRequestError("transport_error", retryable=True)],
        repository,
    )

    with pytest.raises(ReviewWorkflowProviderError) as raised:
        await _workflow(
            repository,
            deterministic,
            semantic=semantic,
            clock=_utc_now,
            lease_duration=timedelta(seconds=1),
        ).run("case-review-1")

    assert raised.value.case_id == "case-review-1"
    assert raised.value.code == "transport_error"
    assert raised.value.retryable is True
    assert repository.conflict_observed.is_set()
    assert repository.active_renewals == 0
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert [event for event, _ in _events(database)][-2:] == [
        "provider_failed",
        "awaiting_human_review",
    ]


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    (
        (ProviderConfigurationError("removed"), "provider_not_configured", False),
        (ProviderRequestError("transport_error", retryable=True), "transport_error", True),
    ),
)
async def test_revision_failure_survives_cleared_lease_race(
    tmp_path: Path,
    error: Exception,
    expected_code: str,
    retryable: bool,
) -> None:
    database = tmp_path / f"revision-failure-race-{expected_code}.sqlite3"
    repository = ClearedLeaseRaceRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix=f"revision-failure-race-{expected_code}",
            )
        ],
    )
    reviser = HeartbeatAwareReviser(repository, outcomes=[error])

    with pytest.raises(ReviewWorkflowProviderError) as raised:
        await _workflow(
            repository,
            deterministic,
            reviser=reviser,
            clock=_utc_now,
            lease_duration=timedelta(seconds=1),
        ).run("case-review-1")

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
    assert repository.conflict_observed.is_set()
    assert repository.active_renewals == 0
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert detail.case.composite_verdict is VerifierVerdict.REVIEW_REQUIRED


async def test_semantic_heartbeat_blocks_concurrent_consented_resume_past_original_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic-heartbeat.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="heartbeat-semantic",
            )
        ],
    )
    semantic = BlockingSemanticVerifier()
    ids = SequenceIds()
    duration = timedelta(milliseconds=150)
    first_workflow = _workflow(
        repository,
        deterministic,
        semantic=semantic,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="semantic-worker-a",
        lease_duration=duration,
    )
    second_workflow = _workflow(
        repository,
        deterministic,
        semantic=semantic,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="semantic-worker-b",
        lease_duration=duration,
    )
    service = _resume_service(repository, second_workflow, deterministic, ids)
    first = asyncio.create_task(first_workflow.run("case-review-1"))
    await asyncio.wait_for(semantic.entered.wait(), timeout=1.0)
    await asyncio.sleep(0.22)

    try:
        with pytest.raises(ReviewConflictError, match="lease is still active"):
            await asyncio.wait_for(
                service.resume("case-review-1", allow_live_api=True),
                timeout=0.5,
            )
        assert semantic.calls == 1
    finally:
        semantic.release.set()
    completed = await asyncio.wait_for(first, timeout=1.0)
    assert completed.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert semantic.calls == 1


async def test_provider_lifecycle_starts_lease_heartbeat_immediately(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immediate-heartbeat.sqlite3"
    repository = ClearedLeaseRaceRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="immediate-heartbeat",
            )
        ],
    )
    semantic = BlockingSemanticVerifier()
    workflow = _workflow(
        repository,
        deterministic,
        semantic=semantic,
        clock=_utc_now,
        lease_duration=timedelta(seconds=1),
    )
    running = asyncio.create_task(workflow.run("case-review-1"))
    await asyncio.wait_for(semantic.entered.wait(), timeout=1.0)
    assert repository.renew_entered.is_set()
    semantic.release.set()
    completed = await asyncio.wait_for(running, timeout=1.0)
    assert completed.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW


async def test_revision_heartbeat_blocks_concurrent_consented_resume_past_original_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "revision-heartbeat.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.DETERMINISTIC,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.NEEDS_EVIDENCE,
                revision_number=0,
                suffix="heartbeat-revision",
            ),
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=1,
                suffix="heartbeat-revision-final",
            ),
        ],
    )
    reviser = BlockingReviser()
    ids = SequenceIds()
    duration = timedelta(milliseconds=150)
    first_workflow = _workflow(
        repository,
        deterministic,
        reviser=reviser,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="revision-worker-a",
        lease_duration=duration,
    )
    second_workflow = _workflow(
        repository,
        deterministic,
        reviser=reviser,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="revision-worker-b",
        lease_duration=duration,
    )
    service = _resume_service(repository, second_workflow, deterministic, ids)
    first = asyncio.create_task(first_workflow.run("case-review-1"))
    await asyncio.wait_for(reviser.entered.wait(), timeout=1.0)
    await asyncio.sleep(0.22)

    try:
        with pytest.raises(ReviewConflictError, match="lease is still active"):
            await asyncio.wait_for(
                service.resume("case-review-1", allow_live_api=True),
                timeout=0.5,
            )
        assert len(reviser.calls) == 1
    finally:
        reviser.release.set()
    completed = await asyncio.wait_for(first, timeout=1.0)
    assert completed.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert completed.case.current_revision_number == 1
    assert len(reviser.calls) == 1


async def test_lease_ownership_loss_cancels_provider_then_allows_stale_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "heartbeat-owner-loss.sqlite3"
    repository = SQLiteReviewRepository(database)
    await repository.initialize()
    await _create_case(
        repository,
        mode=VerificationMode.HYBRID,
        diagnoser=DiagnoserKind.DEEPSEEK,
    )
    deterministic = FakeVerifier(
        VerifierKind.DETERMINISTIC,
        [
            _report(
                VerifierKind.DETERMINISTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="owner-loss",
            )
        ],
    )
    blocked = BlockingSemanticVerifier()
    ids = SequenceIds()
    duration = timedelta(milliseconds=150)
    first_workflow = _workflow(
        repository,
        deterministic,
        semantic=blocked,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="owner-loss-worker",
        lease_duration=duration,
    )
    first = asyncio.create_task(first_workflow.run("case-review-1"))
    await asyncio.wait_for(blocked.entered.wait(), timeout=1.0)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_cases SET lease_owner = ? WHERE case_id = ?",
            ("replacement-owner", "case-review-1"),
        )
        connection.commit()

    with pytest.raises(ReviewConflictError, match="lease"):
        await asyncio.wait_for(first, timeout=0.5)
    await asyncio.wait_for(blocked.cancelled.wait(), timeout=0.5)
    detail = await repository.get_detail("case-review-1")
    assert detail.case.status is ReviewStatus.VERIFYING
    assert len(detail.verifier_reports) == 1

    with sqlite3.connect(database) as connection:
        stored_expiry = connection.execute(
            "SELECT lease_expires_at FROM review_cases WHERE case_id = ?",
            ("case-review-1",),
        ).fetchone()
    assert stored_expiry is not None and stored_expiry[0] is not None
    expires_at = datetime.fromisoformat(stored_expiry[0])
    await asyncio.sleep(max(0.0, (expires_at - _utc_now()).total_seconds()) + 0.05)

    recovered_semantic = FakeVerifier(
        VerifierKind.SEMANTIC,
        [
            _report(
                VerifierKind.SEMANTIC,
                VerifierVerdict.VERIFIED,
                revision_number=0,
                suffix="owner-loss-recovered",
            )
        ],
    )
    recovery_workflow = _workflow(
        repository,
        deterministic,
        semantic=recovered_semantic,
        clock=_utc_now,
        id_factory=ids,
        lease_owner="recovery-worker",
        lease_duration=duration,
    )
    recovered = await _resume_service(repository, recovery_workflow, deterministic, ids).resume(
        "case-review-1", allow_live_api=True
    )
    assert recovered.case.status is ReviewStatus.AWAITING_HUMAN_REVIEW
    assert len(recovered_semantic.inputs) == 1
