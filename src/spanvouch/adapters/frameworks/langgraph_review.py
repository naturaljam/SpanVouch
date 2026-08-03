from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from contextlib import suppress
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, NotRequired, TypedDict, TypeVar
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from spanvouch.contracts.diagnosis import DiagnosisReport
from spanvouch.contracts.review import (
    DiagnosisReviewDetail,
    DiagnosisRevision,
    ReviewStatus,
    RevisionOrigin,
)
from spanvouch.contracts.verification import (
    EvidenceGap,
    FindingCode,
    FindingSeverity,
    OperationalErrorMetadata,
    VerificationFinding,
    VerificationInput,
    VerificationMode,
    VerifierKind,
    VerifierProvenance,
    VerifierReport,
    VerifierVerdict,
)
from spanvouch.contracts.versioning import canonical_json, canonical_sha256
from spanvouch.diagnosis.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderProtocolError,
    ProviderRequestError,
)
from spanvouch.review.commands import (
    AppendDiagnosisRevision,
    AppendVerifierRun,
    ClaimReviewWork,
    FinalizeSemanticFailure,
    RenewReviewLease,
    ReviewLeaseWork,
    RouteCappedRevisionToHuman,
    RouteRevisionFailureToHuman,
    RouteToHumanReview,
    WorkflowEventType,
)
from spanvouch.review.errors import ReviewConflictError, ReviewWorkflowProviderError
from spanvouch.review.protocols import ReviewRepository, ReviewReviser
from spanvouch.review.runtime import ReviewRuntimeBundle
from spanvouch.review.transitions import (
    ReviewRoute,
    next_route,
    should_request_revision,
)
from spanvouch.verification.protocols import Verifier
from spanvouch.verification.verdicts import MergedVerifierReports, merge_verifier_reports

ProviderWorkResult = TypeVar("ProviderWorkResult")
ProviderFinalizationResult = TypeVar("ProviderFinalizationResult")

_LANGGRAPH_ROUTE_TARGETS: dict[Hashable, str] = {
    ReviewRoute.VERIFY_INITIAL: ReviewRoute.VERIFY_INITIAL.value,
    ReviewRoute.REQUEST_REVISION: ReviewRoute.REQUEST_REVISION.value,
    ReviewRoute.REVISE_ONCE: ReviewRoute.REVISE_ONCE.value,
    ReviewRoute.VERIFY_FINAL: ReviewRoute.VERIFY_FINAL.value,
    ReviewRoute.ROUTE_TO_HUMAN: ReviewRoute.ROUTE_TO_HUMAN.value,
    ReviewRoute.END: END,
}
if set(_LANGGRAPH_ROUTE_TARGETS) != set(ReviewRoute):
    raise RuntimeError("LangGraph route mapping must handle every ReviewRoute")


class ReviewWorkflowState(TypedDict):
    case_id: str
    lease_owner: str
    verification_round: int
    composite_verdict: str | None
    route: NotRequired[ReviewRoute]
    lease_claimed: NotRequired[bool]
    provider_effect_committed: NotRequired[bool]
    provider_effect_kind: NotRequired[str]
    provider_effect_id: NotRequired[str]
    provider_commit_version: NotRequired[int]
    provider_commit_status: NotRequired[str]
    provider_commit_revision_count: NotRequired[int]
    provider_commit_report_count: NotRequired[int]


def _require_aware_utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("clock must return an aware UTC timestamp")
    return value


def _provider_failure(error: ProviderError) -> tuple[str, bool]:
    if isinstance(error, ProviderRequestError):
        code = (
            error.code
            if error.code in {"transport_error", "upstream_http_error", "missing_response"}
            else "provider_request_error"
        )
        return code, error.retryable
    if isinstance(error, ProviderConfigurationError):
        return "provider_not_configured", False
    if isinstance(error, ProviderProtocolError):
        return "provider_protocol_error", False
    return "provider_error", False


class LangGraphReviewWorkflow:
    """Coordinate one bounded review invocation over SQLite-authoritative state.

    A provider call is made only after a durable lease claim. Crash recovery may
    therefore invoke a model at least once (and may bill it more than once), while
    repository CAS and immutable IDs provide exactly-once persisted domain effects.
    LangGraph state contains routing hints only and is never a recovery record.
    """

    def __init__(
        self,
        *,
        repository: ReviewRepository,
        deterministic_verifier: Verifier,
        semantic_verifier: Verifier | None,
        reviser: ReviewReviser,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        lease_owner: str,
        lease_token_factory: Callable[[], str] | None = None,
        lease_duration: timedelta,
    ) -> None:
        if deterministic_verifier.kind != VerifierKind.DETERMINISTIC:
            raise ValueError("deterministic_verifier must be deterministic")
        if semantic_verifier is not None and semantic_verifier.kind != VerifierKind.SEMANTIC:
            raise ValueError("semantic_verifier must be semantic")
        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        _require_aware_utc(clock())
        self._repository = repository
        self._deterministic = deterministic_verifier
        self._semantic = semantic_verifier
        self._reviser = reviser
        self._id_factory = id_factory
        self._clock = clock
        self._lease_owner_prefix = lease_owner
        self._lease_token_factory = lease_token_factory or (lambda: str(uuid4()))
        self._lease_duration = lease_duration
        self.graph = self._compile_graph()

    def _compile_graph(self) -> Any:
        graph = StateGraph(ReviewWorkflowState)
        graph.add_node("dispatch", self._dispatch)
        graph.add_node(ReviewRoute.VERIFY_INITIAL.value, self._verify_initial)
        graph.add_node(ReviewRoute.REQUEST_REVISION.value, self._request_revision)
        graph.add_node(ReviewRoute.REVISE_ONCE.value, self._revise_once)
        graph.add_node(ReviewRoute.VERIFY_FINAL.value, self._verify_final)
        graph.add_node(ReviewRoute.ROUTE_TO_HUMAN.value, self._route_to_human)
        graph.add_edge(START, "dispatch")
        graph.add_conditional_edges(
            "dispatch", self._route, _LANGGRAPH_ROUTE_TARGETS
        )
        for source in (
            ReviewRoute.VERIFY_INITIAL,
            ReviewRoute.REQUEST_REVISION,
            ReviewRoute.REVISE_ONCE,
            ReviewRoute.VERIFY_FINAL,
        ):
            graph.add_conditional_edges(
                source.value,
                self._route,
                _LANGGRAPH_ROUTE_TARGETS,
            )
        graph.add_edge(ReviewRoute.ROUTE_TO_HUMAN.value, END)
        return graph.compile()

    @staticmethod
    def _route(state: ReviewWorkflowState) -> ReviewRoute:
        return state["route"]

    def _now(self) -> datetime:
        return _require_aware_utc(self._clock())

    def _new_execution_lease_owner(self) -> str:
        token = self._lease_token_factory()
        if not token:
            raise ValueError("lease token factory must not return an empty token")
        return f"{self._lease_owner_prefix}:{token}"

    async def _renew_lease_until_stopped(
        self,
        runtime: ReviewRuntimeBundle,
        *,
        lease_owner: str,
        work: ReviewLeaseWork,
        stopped: asyncio.Event,
        committed: asyncio.Event,
    ) -> None:
        interval = self._lease_duration.total_seconds() / 3

        async def renew() -> None:
            now = self._now()
            lease_expires_at = runtime.lease_expires_at
            if (
                lease_expires_at is not None
                and now + self._lease_duration <= lease_expires_at
            ):
                return
            try:
                await self._repository.renew_review_lease(
                    RenewReviewLease(
                        case_id=runtime.case.case_id,
                        expected_version=runtime.case.version,
                        expected_status=runtime.case.status,
                        lease_owner=lease_owner,
                        work=work,
                        now=now,
                        lease_expires_at=now + self._lease_duration,
                    )
                )
            except ReviewConflictError:
                if committed.is_set():
                    return
                raise

        await renew()
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), timeout=interval)
            if stopped.is_set():
                return
            await renew()

    async def _run_provider_lifecycle(
        self,
        runtime: ReviewRuntimeBundle,
        *,
        lease_owner: str,
        work: ReviewLeaseWork,
        provider: Callable[[], Awaitable[ProviderWorkResult]],
        finalize: Callable[[ProviderWorkResult], Awaitable[ProviderFinalizationResult]],
    ) -> ProviderFinalizationResult:
        stopped = asyncio.Event()
        committed = asyncio.Event()

        async def call_provider() -> ProviderWorkResult:
            return await provider()

        provider_task: asyncio.Task[ProviderWorkResult] = asyncio.create_task(call_provider())
        heartbeat_task = asyncio.create_task(
            self._renew_lease_until_stopped(
                runtime,
                lease_owner=lease_owner,
                work=work,
                stopped=stopped,
                committed=committed,
            )
        )
        finalization_task: asyncio.Task[ProviderFinalizationResult] | None = None
        try:
            done, _ = await asyncio.wait(
                {provider_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    if not provider_task.done():
                        provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise heartbeat_error
                if not provider_task.done():
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise ReviewConflictError(
                        "review lease heartbeat stopped before provider completion"
                    )

            provider_result = await provider_task
            if heartbeat_task.done():
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    raise heartbeat_error
                raise ReviewConflictError("review lease heartbeat stopped before finalization")

            async def call_finalizer() -> ProviderFinalizationResult:
                return await finalize(provider_result)

            finalization_task = asyncio.create_task(call_finalizer())
            finalization_wait: set[asyncio.Future[Any]] = {
                finalization_task,
                heartbeat_task,
            }
            finalization_done, _ = await asyncio.wait(
                finalization_wait,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if finalization_task not in finalization_done:
                heartbeat_error = heartbeat_task.exception()
                if isinstance(heartbeat_error, ReviewConflictError):
                    try:
                        result = await finalization_task
                    except BaseException:
                        raise heartbeat_error from None
                    committed.set()
                    stopped.set()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                    return result
                finalization_task.cancel()
                await asyncio.gather(finalization_task, return_exceptions=True)
                if heartbeat_error is not None:
                    raise heartbeat_error
                raise ReviewConflictError(
                    "review lease heartbeat stopped before finalization commit"
                )

            result = await finalization_task
            committed.set()
            stopped.set()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            return result
        finally:
            stopped.set()
            tasks: list[asyncio.Future[Any]] = [provider_task, heartbeat_task]
            if finalization_task is not None:
                tasks.append(finalization_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, case_id: str) -> DiagnosisReviewDetail:
        return await self._execute(case_id, self._new_execution_lease_owner())

    async def resume(self, case_id: str) -> DiagnosisReviewDetail:
        return await self._execute(case_id, self._new_execution_lease_owner())

    async def _execute(self, case_id: str, lease_owner: str) -> DiagnosisReviewDetail:
        runtime = await self._repository.load_runtime(case_id)
        if runtime.case.status in {
            ReviewStatus.AWAITING_HUMAN_REVIEW,
            ReviewStatus.CONFIRMED,
            ReviewStatus.CORRECTED,
            ReviewStatus.REJECTED,
        }:
            raise ReviewConflictError("review case cannot be resumed from its current status")
        state: ReviewWorkflowState = {
            "case_id": case_id,
            "lease_owner": lease_owner,
            "verification_round": runtime.case.current_revision_number,
            "composite_verdict": (
                runtime.case.composite_verdict.value
                if runtime.case.composite_verdict is not None
                else None
            ),
        }
        await self.graph.ainvoke(state)
        return await self._repository.get_detail(case_id)

    async def _dispatch(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        runtime = await self._repository.load_runtime(state["case_id"])
        route = next_route(runtime.case)
        if route is ReviewRoute.END:
            raise ReviewConflictError("review case cannot be resumed from its current status")
        return {
            **state,
            "verification_round": runtime.case.current_revision_number,
            "composite_verdict": (
                runtime.case.composite_verdict.value
                if runtime.case.composite_verdict is not None
                else None
            ),
            "route": route,
            "lease_claimed": False,
        }

    async def _verify_initial(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        return await self._verify_round(state, expected_round=0)

    async def _verify_final(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        return await self._verify_round(state, expected_round=1)

    async def _claim(
        self,
        runtime: ReviewRuntimeBundle,
        *,
        lease_owner: str,
        target: ReviewStatus,
        event_type: WorkflowEventType,
    ) -> ReviewRuntimeBundle:
        now = self._now()
        await self._repository.claim_work(
            ClaimReviewWork(
                case_id=runtime.case.case_id,
                expected_version=runtime.case.version,
                prior_status=runtime.case.status,
                target_status=target,
                lease_owner=lease_owner,
                lease_expires_at=now + self._lease_duration,
                now=now,
                event_id=self._id_factory(),
                event_type=event_type,
                event_metadata_json=canonical_json({"lease_owner": lease_owner}),
                occurred_at=now,
            )
        )
        return await self._repository.load_runtime(runtime.case.case_id)

    @staticmethod
    def _reports_for_revision(
        runtime: ReviewRuntimeBundle,
    ) -> tuple[VerifierReport | None, VerifierReport | None]:
        deterministic: VerifierReport | None = None
        semantic: VerifierReport | None = None
        for report in runtime.verifier_reports:
            if report.revision_number != runtime.case.current_revision_number:
                continue
            if report.verifier_kind == VerifierKind.DETERMINISTIC:
                deterministic = report
            else:
                semantic = report
        return deterministic, semantic

    async def _verify_round(
        self, state: ReviewWorkflowState, *, expected_round: int
    ) -> ReviewWorkflowState:
        try:
            return await self._verify_round_once(state, expected_round=expected_round)
        except ReviewConflictError:
            converged = await self._durable_postcommit_state(state)
            if converged is not None:
                return converged
            raise

    async def _verify_round_once(
        self, state: ReviewWorkflowState, *, expected_round: int
    ) -> ReviewWorkflowState:
        case_id = state["case_id"]
        semantic_committed = False
        semantic_commit_version: int | None = None
        runtime = await self._repository.load_runtime(case_id)
        if runtime.case.current_revision_number != expected_round:
            raise ReviewConflictError("verification round conflicts with durable state")
        deterministic, semantic = self._reports_for_revision(runtime)

        if deterministic is None:
            if runtime.case.status not in {
                ReviewStatus.PENDING_VERIFICATION,
                ReviewStatus.VERIFYING,
            }:
                raise ReviewConflictError("deterministic verification is not claimable")
            runtime = await self._claim(
                runtime,
                lease_owner=state["lease_owner"],
                target=ReviewStatus.VERIFYING,
                event_type=WorkflowEventType.VERIFICATION_STARTED,
            )
            input_ = self._verification_input(runtime)
            report = await self._deterministic.verify(input_)
            deterministic = self._normalize_report(
                report, VerifierKind.DETERMINISTIC, expected_round
            )
            merged = merge_verifier_reports(deterministic, None)
            request_revision = should_request_revision(
                runtime.case,
                merged.verdict,
                reviser_supported=self._reviser.supports(runtime.case.diagnoser),
            )
            runtime = await self._append_verifier(
                runtime,
                deterministic,
                merged,
                request_revision=request_revision,
                lease_owner=state["lease_owner"],
            )
            if request_revision:
                return self._state_after(
                    runtime,
                    ReviewRoute.REQUEST_REVISION,
                    lease_owner=state["lease_owner"],
                    postcommit_state=state,
                )

        if deterministic.verdict is not VerifierVerdict.VERIFIED:
            runtime = await self._repository.load_runtime(case_id)
            return self._state_after(
                runtime,
                ReviewRoute.ROUTE_TO_HUMAN,
                lease_owner=state["lease_owner"],
                postcommit_state=state,
            )

        runtime = await self._repository.load_runtime(case_id)
        if runtime.case.verification_mode is VerificationMode.HYBRID:
            deterministic, semantic = self._reports_for_revision(runtime)
            if deterministic is None:
                raise ReviewConflictError("deterministic verification is missing")
            if semantic is None:
                runtime = await self._claim(
                    runtime,
                    lease_owner=state["lease_owner"],
                    target=ReviewStatus.VERIFYING,
                    event_type=WorkflowEventType.VERIFICATION_STARTED,
                )
                claimed_runtime = runtime
                semantic_started_at = self._now()

                async def verify_semantically() -> VerifierReport | ProviderError:
                    if self._semantic is None:
                        return ProviderConfigurationError("semantic verifier is not configured")
                    try:
                        return await self._semantic.verify(
                            self._verification_input(claimed_runtime)
                        )
                    except ProviderError as error:
                        return error

                async def finalize_semantic(
                    outcome: VerifierReport | ProviderError,
                ) -> tuple[
                    VerifierReport | None,
                    bool,
                    ReviewWorkflowProviderError | None,
                ]:
                    if isinstance(outcome, ProviderError):
                        await self._persist_semantic_failure(
                            claimed_runtime,
                            deterministic,
                            outcome,
                            lease_owner=state["lease_owner"],
                            started_at=semantic_started_at,
                        )
                        code, retryable = _provider_failure(outcome)
                        return (
                            None,
                            False,
                            ReviewWorkflowProviderError(case_id, code, retryable=retryable),
                        )
                    normalized = self._normalize_report(
                        outcome, VerifierKind.SEMANTIC, expected_round
                    )
                    merged = merge_verifier_reports(deterministic, normalized)
                    request_revision = should_request_revision(
                        claimed_runtime.case,
                        merged.verdict,
                        reviser_supported=self._reviser.supports(
                            claimed_runtime.case.diagnoser
                        ),
                    )
                    await self._commit_verifier(
                        claimed_runtime,
                        normalized,
                        merged,
                        request_revision=request_revision,
                        lease_owner=state["lease_owner"],
                    )
                    return normalized, request_revision, None

                semantic, request_revision, provider_error = await self._run_provider_lifecycle(
                    claimed_runtime,
                    lease_owner=state["lease_owner"],
                    work=ReviewLeaseWork.SEMANTIC_VERIFICATION,
                    provider=verify_semantically,
                    finalize=finalize_semantic,
                )
                if provider_error is not None:
                    raise provider_error
                if semantic is None:
                    raise ReviewConflictError("semantic finalization result is missing")
                semantic_committed = True
                semantic_commit_version = claimed_runtime.case.version + 1
                runtime = await self._repository.load_runtime(case_id)
                if request_revision:
                    return self._state_after(
                        runtime,
                        ReviewRoute.REQUEST_REVISION,
                        lease_owner=state["lease_owner"],
                        provider_effect_kind="semantic",
                        provider_effect_id=semantic.verifier_run_id,
                        provider_commit_version=semantic_commit_version,
                    )

        runtime = await self._repository.load_runtime(case_id)
        if semantic_committed and semantic is not None:
            return self._state_after(
                runtime,
                ReviewRoute.ROUTE_TO_HUMAN,
                lease_owner=state["lease_owner"],
                provider_effect_kind="semantic",
                provider_effect_id=semantic.verifier_run_id,
                provider_commit_version=semantic_commit_version,
            )
        return self._state_after(
            runtime,
            ReviewRoute.ROUTE_TO_HUMAN,
            lease_owner=state["lease_owner"],
            postcommit_state=state,
        )

    @staticmethod
    def _verification_input(runtime: ReviewRuntimeBundle) -> VerificationInput:
        revision = runtime.revisions[-1]
        return VerificationInput(
            snapshot=runtime.snapshot,
            report=revision.report,
            report_sha256=revision.report_sha256,
            revision_number=runtime.case.current_revision_number,
        )

    @staticmethod
    def _normalize_report(
        report: VerifierReport, kind: VerifierKind, revision_number: int
    ) -> VerifierReport:
        if report.verifier_kind != kind or report.provenance.verifier_kind != kind:
            raise ReviewConflictError("verifier returned the wrong verifier kind")
        return VerifierReport.model_validate(
            {**report.model_dump(), "revision_number": revision_number}
        )

    async def _append_verifier(
        self,
        runtime: ReviewRuntimeBundle,
        report: VerifierReport,
        merged: MergedVerifierReports,
        *,
        request_revision: bool,
        event_type: WorkflowEventType | None = None,
        lease_owner: str | None = None,
    ) -> ReviewRuntimeBundle:
        await self._commit_verifier(
            runtime,
            report,
            merged,
            request_revision=request_revision,
            event_type=event_type,
            lease_owner=lease_owner,
        )
        return await self._repository.load_runtime(runtime.case.case_id)

    async def _commit_verifier(
        self,
        runtime: ReviewRuntimeBundle,
        report: VerifierReport,
        merged: MergedVerifierReports,
        *,
        request_revision: bool,
        event_type: WorkflowEventType | None = None,
        lease_owner: str | None = None,
    ) -> None:
        now = self._now()
        target = ReviewStatus.REVISION_REQUESTED if request_revision else ReviewStatus.VERIFYING
        if event_type is None:
            event_type = (
                WorkflowEventType.REVISION_REQUESTED
                if request_revision
                else WorkflowEventType.VERIFICATION_COMPLETED
            )
        await self._repository.append_verifier_run(
            AppendVerifierRun(
                case_id=runtime.case.case_id,
                expected_version=runtime.case.version,
                prior_status=ReviewStatus.VERIFYING,
                target_status=target,
                report=report,
                composite_verdict=merged.verdict,
                lease_owner=lease_owner,
                event_id=self._id_factory(),
                event_type=event_type,
                event_metadata_json=canonical_json(
                    {
                        "revision_number": report.revision_number,
                        "verdict": merged.verdict.value,
                        "verifier_kind": report.verifier_kind,
                    }
                ),
                occurred_at=now,
            )
        )

    async def _request_revision(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        try:
            runtime = await self._repository.load_runtime(state["case_id"])
            if runtime.case.status is not ReviewStatus.REVISION_REQUESTED:
                raise ReviewConflictError("revision is not requested")
            if (
                runtime.case.evidence_revision_count != 0
                or runtime.case.current_revision_number != 0
            ):
                raise ReviewConflictError("evidence revision limit reached")
            runtime = await self._claim(
                runtime,
                lease_owner=state["lease_owner"],
                target=ReviewStatus.REVISING,
                event_type=WorkflowEventType.REVISION_STARTED,
            )
        except ReviewConflictError:
            converged = await self._durable_postcommit_state(state)
            if converged is not None:
                return converged
            raise
        return {
            **self._state_after(
                runtime,
                ReviewRoute.REVISE_ONCE,
                lease_owner=state["lease_owner"],
            ),
            "lease_claimed": True,
        }

    async def _revise_once(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        runtime = await self._repository.load_runtime(state["case_id"])
        if runtime.case.status is not ReviewStatus.REVISING:
            raise ReviewConflictError("review case is not revising")
        if runtime.case.evidence_revision_count != 0 or runtime.case.current_revision_number != 0:
            raise ReviewConflictError("evidence revision limit reached")
        if not state.get("lease_claimed", False):
            runtime = await self._claim(
                runtime,
                lease_owner=state["lease_owner"],
                target=ReviewStatus.REVISING,
                event_type=WorkflowEventType.REVISION_STARTED,
            )
        deterministic, semantic = self._reports_for_revision(runtime)
        if deterministic is None:
            raise ReviewConflictError("revision requires a verifier report")
        merged = merge_verifier_reports(deterministic, semantic)
        gaps = tuple(sorted(merged.evidence_gaps, key=lambda gap: gap.gap_id))
        if not gaps:
            raise ReviewConflictError("evidence revision is unsupported")
        claimed_runtime = runtime
        committed_revision: DiagnosisRevision | None = None

        async def revise() -> DiagnosisReport | ProviderError:
            if not self._reviser.supports(claimed_runtime.case.diagnoser):
                return ProviderConfigurationError("revision provider is not configured")
            try:
                return await self._reviser.revise(claimed_runtime, gaps)
            except ProviderError as error:
                return error

        async def finalize_revision(
            outcome: DiagnosisReport | ProviderError,
        ) -> ReviewWorkflowProviderError | None:
            nonlocal committed_revision
            if isinstance(outcome, ProviderError):
                code, retryable = _provider_failure(outcome)
                await self._persist_revision_failure(
                    claimed_runtime,
                    lease_owner=state["lease_owner"],
                    code=code,
                    retryable=retryable,
                )
                return ReviewWorkflowProviderError(
                    claimed_runtime.case.case_id,
                    code,
                    retryable=retryable,
                )
            committed_revision = await self._commit_revision(
                claimed_runtime,
                gaps,
                outcome,
                lease_owner=state["lease_owner"],
            )
            return None

        provider_error = await self._run_provider_lifecycle(
            claimed_runtime,
            lease_owner=state["lease_owner"],
            work=ReviewLeaseWork.EVIDENCE_REVISION,
            provider=revise,
            finalize=finalize_revision,
        )
        if provider_error is not None:
            raise provider_error
        if committed_revision is None:
            raise ReviewConflictError("revision finalization result is missing")
        runtime = await self._repository.load_runtime(claimed_runtime.case.case_id)
        return self._state_after(
            runtime,
            ReviewRoute.VERIFY_FINAL,
            lease_owner=state["lease_owner"],
            provider_effect_kind="revision",
            provider_effect_id=committed_revision.revision_id,
            provider_commit_version=claimed_runtime.case.version + 1,
        )

    async def _commit_revision(
        self,
        runtime: ReviewRuntimeBundle,
        gaps: tuple[EvidenceGap, ...],
        revised_report: DiagnosisReport,
        *,
        lease_owner: str,
    ) -> DiagnosisRevision:
        previous = runtime.revisions[-1]
        if (
            revised_report.trace_id != runtime.snapshot.trace_id
            or revised_report.run_id != runtime.snapshot.run_id
            or revised_report.diagnoser != runtime.case.diagnoser
        ):
            raise ReviewConflictError("revised diagnosis is not bound to the review input")
        now = self._now()
        revision = DiagnosisRevision(
            revision_id=self._id_factory(),
            case_id=runtime.case.case_id,
            revision_number=1,
            origin=RevisionOrigin.EVIDENCE_REVISION,
            previous_report_sha256=previous.report_sha256,
            report=revised_report,
            report_sha256=canonical_sha256(revised_report),
            triggering_gap_ids=tuple(gap.gap_id for gap in gaps),
            provenance=revised_report.provenance,
            created_at=now,
        )
        await self._repository.append_revision(
            AppendDiagnosisRevision(
                case_id=runtime.case.case_id,
                expected_version=runtime.case.version,
                prior_status=ReviewStatus.REVISING,
                target_status=ReviewStatus.VERIFYING,
                revision=revision,
                lease_owner=lease_owner,
                event_id=self._id_factory(),
                event_type=WorkflowEventType.REVISION_COMPLETED,
                event_metadata_json=canonical_json(
                    {
                        "revision_number": 1,
                        "triggering_gap_ids": list(revision.triggering_gap_ids),
                    }
                ),
                occurred_at=now,
            )
        )
        return revision

    async def _route_to_human(self, state: ReviewWorkflowState) -> ReviewWorkflowState:
        runtime = await self._repository.load_runtime(state["case_id"])
        if runtime.case.status in {
            ReviewStatus.REVISION_REQUESTED,
            ReviewStatus.REVISING,
        }:
            if next_route(runtime.case) is not ReviewRoute.ROUTE_TO_HUMAN:
                raise ReviewConflictError("review case has not reached the revision limit")
            if runtime.case.composite_verdict is None:
                raise ReviewConflictError("review case has no composite verdict")
            try:
                await self._repository.route_capped_revision_to_human(
                    RouteCappedRevisionToHuman(
                        case_id=runtime.case.case_id,
                        expected_version=runtime.case.version,
                        prior_status=runtime.case.status,
                        target_status=ReviewStatus.AWAITING_HUMAN_REVIEW,
                        event_id=self._id_factory(),
                        event_type=WorkflowEventType.AWAITING_HUMAN_REVIEW,
                        event_metadata_json=canonical_json(
                            {
                                "reason": "evidence_revision_limit_reached",
                                "verdict": runtime.case.composite_verdict.value,
                            }
                        ),
                        occurred_at=self._now(),
                    )
                )
            except ReviewConflictError:
                converged = await self._converged_human_route(state)
                if converged is not None:
                    return converged
                raise
            runtime = await self._repository.load_runtime(runtime.case.case_id)
            return self._state_after(
                runtime,
                ReviewRoute.END,
                lease_owner=state["lease_owner"],
                postcommit_state=state,
            )
        if runtime.case.status is not ReviewStatus.VERIFYING:
            if self._has_validated_external_progress(state, runtime):
                return self._state_after(
                    runtime,
                    ReviewRoute.END,
                    lease_owner=state["lease_owner"],
                    postcommit_state=state,
                )
            raise ReviewConflictError("review case is not ready for human review")
        if runtime.case.composite_verdict is None:
            raise ReviewConflictError("review case has no composite verdict")
        now = self._now()
        try:
            await self._repository.route_to_human(
                RouteToHumanReview(
                    case_id=runtime.case.case_id,
                    expected_version=runtime.case.version,
                    prior_status=ReviewStatus.VERIFYING,
                    target_status=ReviewStatus.AWAITING_HUMAN_REVIEW,
                    event_id=self._id_factory(),
                    event_type=WorkflowEventType.AWAITING_HUMAN_REVIEW,
                    event_metadata_json=canonical_json(
                        {"verdict": runtime.case.composite_verdict.value}
                    ),
                    occurred_at=now,
                )
            )
        except ReviewConflictError:
            converged = await self._converged_human_route(state)
            if converged is not None:
                return converged
            converged = await self._durable_postcommit_state(state)
            if converged is not None:
                return converged
            raise
        runtime = await self._repository.load_runtime(runtime.case.case_id)
        return self._state_after(
            runtime,
            ReviewRoute.END,
            lease_owner=state["lease_owner"],
            postcommit_state=state,
        )

    async def _converged_human_route(
        self, state: ReviewWorkflowState
    ) -> ReviewWorkflowState | None:
        runtime = await self._repository.load_runtime(state["case_id"])
        if runtime.case.status is not ReviewStatus.AWAITING_HUMAN_REVIEW:
            return None
        return self._state_after(
            runtime,
            ReviewRoute.END,
            lease_owner=state["lease_owner"],
            postcommit_state=state,
        )

    async def _durable_postcommit_state(
        self, state: ReviewWorkflowState
    ) -> ReviewWorkflowState | None:
        if not state.get("provider_effect_committed", False):
            return None
        runtime = await self._repository.load_runtime(state["case_id"])
        if not self._has_validated_external_progress(state, runtime):
            return None
        return self._state_after(
            runtime,
            ReviewRoute.END,
            lease_owner=state["lease_owner"],
            postcommit_state=state,
        )

    def _has_validated_external_progress(
        self,
        state: ReviewWorkflowState,
        runtime: ReviewRuntimeBundle,
    ) -> bool:
        baseline_version = state.get("provider_commit_version")
        effect_kind = state.get("provider_effect_kind")
        effect_id = state.get("provider_effect_id")
        if baseline_version is None or effect_kind is None or effect_id is None:
            return False
        if runtime.case.version <= baseline_version:
            return False
        if effect_kind == "semantic":
            effect_is_durable = any(
                report.verifier_run_id == effect_id
                and report.verifier_kind == VerifierKind.SEMANTIC
                for report in runtime.verifier_reports
            )
        elif effect_kind == "revision":
            effect_is_durable = any(
                revision.revision_id == effect_id for revision in runtime.revisions
            )
        else:
            return False
        if not effect_is_durable:
            return False
        if runtime.case.status in {
            ReviewStatus.AWAITING_HUMAN_REVIEW,
            ReviewStatus.CONFIRMED,
            ReviewStatus.CORRECTED,
            ReviewStatus.REJECTED,
        }:
            return True
        return (
            runtime.lease_owner is not None
            and runtime.lease_owner != state["lease_owner"]
        )

    async def _persist_semantic_failure(
        self,
        runtime: ReviewRuntimeBundle,
        deterministic: VerifierReport,
        error: ProviderError,
        *,
        lease_owner: str,
        started_at: datetime,
    ) -> None:
        code, retryable = _provider_failure(error)
        completed_at = self._now()
        source = f"{runtime.case.case_id}:{runtime.case.current_revision_number}:semantic:{code}"
        digest = sha256(source.encode("utf-8")).hexdigest()
        finding = VerificationFinding(
            finding_id=f"finding-{digest}",
            code=FindingCode.PROVIDER_OPERATIONAL_ERROR,
            severity=FindingSeverity.OPERATIONAL,
            message="Semantic verifier provider failed.",
            revisable=False,
        )
        report = VerifierReport(
            verifier_run_id=f"verifier-{digest}",
            revision_number=runtime.case.current_revision_number,
            report_sha256=runtime.revisions[-1].report_sha256,
            verifier_kind=VerifierKind.SEMANTIC,
            verdict=VerifierVerdict.REVIEW_REQUIRED,
            findings=(finding,),
            provenance=VerifierProvenance(
                verifier_kind=VerifierKind.SEMANTIC,
                verifier_version=(
                    self._semantic.version_fingerprint
                    if self._semantic is not None
                    else "semantic-verifier-unconfigured-v1"
                ),
                policy_version="semantic-provider-failure-v1",
            ),
            operational_error=OperationalErrorMetadata(
                code=code,
                message="Semantic verifier provider failed.",
                retryable=retryable,
            ),
            started_at=started_at,
            completed_at=completed_at,
        )
        merged = merge_verifier_reports(deterministic, report)
        verifier = AppendVerifierRun(
            case_id=runtime.case.case_id,
            expected_version=runtime.case.version,
            prior_status=ReviewStatus.VERIFYING,
            target_status=ReviewStatus.VERIFYING,
            report=report,
            composite_verdict=merged.verdict,
            lease_owner=lease_owner,
            event_id=self._id_factory(),
            event_type=WorkflowEventType.PROVIDER_FAILED,
            event_metadata_json=canonical_json(
                {
                    "revision_number": report.revision_number,
                    "verdict": merged.verdict.value,
                    "verifier_kind": report.verifier_kind,
                }
            ),
            occurred_at=completed_at,
        )
        route = RouteToHumanReview(
            case_id=runtime.case.case_id,
            expected_version=runtime.case.version + 1,
            prior_status=ReviewStatus.VERIFYING,
            target_status=ReviewStatus.AWAITING_HUMAN_REVIEW,
            event_id=self._id_factory(),
            event_type=WorkflowEventType.AWAITING_HUMAN_REVIEW,
            event_metadata_json=canonical_json({"verdict": merged.verdict.value}),
            occurred_at=completed_at,
        )
        await self._repository.finalize_semantic_failure(
            FinalizeSemanticFailure(verifier=verifier, route=route)
        )

    async def _persist_revision_failure(
        self,
        runtime: ReviewRuntimeBundle,
        *,
        lease_owner: str,
        code: str,
        retryable: bool,
    ) -> None:
        now = self._now()
        await self._repository.route_revision_failure(
            RouteRevisionFailureToHuman(
                case_id=runtime.case.case_id,
                expected_version=runtime.case.version,
                prior_status=ReviewStatus.REVISING,
                target_status=ReviewStatus.AWAITING_HUMAN_REVIEW,
                composite_verdict=VerifierVerdict.REVIEW_REQUIRED,
                lease_owner=lease_owner,
                event_id=self._id_factory(),
                event_type=WorkflowEventType.REVISION_PROVIDER_FAILED,
                event_metadata_json=canonical_json({"code": code, "retryable": retryable}),
                occurred_at=now,
            )
        )

    @staticmethod
    def _state_after(
        runtime: ReviewRuntimeBundle,
        route: ReviewRoute,
        *,
        lease_owner: str,
        provider_effect_kind: str | None = None,
        provider_effect_id: str | None = None,
        provider_commit_version: int | None = None,
        postcommit_state: ReviewWorkflowState | None = None,
    ) -> ReviewWorkflowState:
        state: ReviewWorkflowState = {
            "case_id": runtime.case.case_id,
            "lease_owner": lease_owner,
            "verification_round": runtime.case.current_revision_number,
            "composite_verdict": (
                runtime.case.composite_verdict.value
                if runtime.case.composite_verdict is not None
                else None
            ),
            "route": route,
            "lease_claimed": False,
        }
        if provider_effect_kind is not None:
            if provider_effect_kind not in {"semantic", "revision"}:
                raise ValueError("unknown provider effect kind")
            if provider_effect_id is None or provider_commit_version is None:
                raise ReviewConflictError("provider effect is missing from durable state")
            state["provider_effect_committed"] = True
            state["provider_effect_kind"] = provider_effect_kind
            state["provider_effect_id"] = provider_effect_id
            state["provider_commit_version"] = provider_commit_version
            state["provider_commit_status"] = runtime.case.status.value
            state["provider_commit_revision_count"] = len(runtime.revisions)
            state["provider_commit_report_count"] = len(runtime.verifier_reports)
        elif postcommit_state is not None and postcommit_state.get(
            "provider_effect_committed", False
        ):
            effect_kind = postcommit_state.get("provider_effect_kind")
            effect_id = postcommit_state.get("provider_effect_id")
            commit_version = postcommit_state.get("provider_commit_version")
            commit_status = postcommit_state.get("provider_commit_status")
            revision_count = postcommit_state.get("provider_commit_revision_count")
            report_count = postcommit_state.get("provider_commit_report_count")
            if (
                effect_kind is None
                or effect_id is None
                or commit_version is None
                or commit_status is None
                or revision_count is None
                or report_count is None
            ):
                raise ReviewConflictError("provider commit state is incomplete")
            state["provider_effect_committed"] = True
            state["provider_effect_kind"] = effect_kind
            state["provider_effect_id"] = effect_id
            state["provider_commit_version"] = commit_version
            state["provider_commit_status"] = commit_status
            state["provider_commit_revision_count"] = revision_count
            state["provider_commit_report_count"] = report_count
        return state
