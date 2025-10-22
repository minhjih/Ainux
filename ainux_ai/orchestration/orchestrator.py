"""High-level natural language orchestrator wiring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Set, TYPE_CHECKING

from ..client import ChatClient
from .execution import (
    ActionExecutor,
    AnalyzeResourceHotspotsCapability,
    ApplicationLauncherCapability,
    ApplyResourceTuningCapability,
    BlueprintCapability,
    CapabilityRegistry,
    CollectResourceMetricsCapability,
    DryRunCapability,
    LowLevelCodeCapability,
    PointerControlCapability,
    ProcessEnumerationCapability,
    ProcessEvaluationCapability,
    ProcessManagementCapability,
    ShellCommandCapability,
)
from .intent import IntentParser
from .models import ExecutionResult, OrchestrationResult, PlanReview, PlanStep
from .planner import Planner
from .safety import SafetyChecker


if TYPE_CHECKING:
    from ..context import ContextFabric


class OrchestrationError(RuntimeError):
    """Raised when orchestration cannot proceed."""


class OrchestrationObserver(Protocol):
    """Observer interface for tracking orchestration progress."""

    def on_stage(self, stage: str, detail: Optional[str] = None) -> None:
        """Called whenever a high-level stage begins."""

    def on_step_start(self, step: PlanStep, index: int, total: int) -> None:
        """Called right before a plan step is executed."""

    def on_step_result(self, result: ExecutionResult) -> None:
        """Called after a plan step produces a result."""

    def on_review(self, review: PlanReview) -> None:
        """Called after each planner review round."""


@dataclass
class AinuxOrchestrator:
    """End-to-end natural language orchestrator composed of modular stages."""

    intent_parser: IntentParser
    planner: Planner
    safety_checker: SafetyChecker
    executor: ActionExecutor
    fabric: Optional["ContextFabric"] = None
    fabric_event_limit: int = 50

    @classmethod
    def with_client(
        cls,
        client: Optional[ChatClient] = None,
        *,
        fabric: Optional["ContextFabric"] = None,
        fabric_event_limit: int = 50,
    ) -> "AinuxOrchestrator":
        """Factory that wires default components with an optional GPT client."""

        intent_parser = IntentParser(client=client)
        planner = Planner(client=client)
        safety = SafetyChecker(client=client)
        registry = CapabilityRegistry()
        registry.register(CollectResourceMetricsCapability())
        registry.register(AnalyzeResourceHotspotsCapability())
        registry.register(ApplyResourceTuningCapability())
        registry.register(DryRunCapability(name="system.collect_task_requirements"))
        registry.register(DryRunCapability(name="scheduler.create_task_schedule"))
        registry.register(DryRunCapability(name="scheduler.publish_user_guidance"))
        registry.register(ProcessEnumerationCapability())
        registry.register(ProcessEvaluationCapability())
        registry.register(ProcessManagementCapability())
        registry.register(DryRunCapability(name="ui.collect_user_context"))
        registry.register(DryRunCapability(name="ui.present_walkthrough"))
        registry.register(DryRunCapability(name="ui.queue_actions"))
        registry.register(DryRunCapability(name="analysis.review_request"))
        registry.register(BlueprintCapability())
        registry.register(ShellCommandCapability())
        registry.register(ApplicationLauncherCapability())
        registry.register(PointerControlCapability())
        registry.register(LowLevelCodeCapability())
        executor = ActionExecutor(registry=registry)
        return cls(
            intent_parser=intent_parser,
            planner=planner,
            safety_checker=safety,
            executor=executor,
            fabric=fabric,
            fabric_event_limit=fabric_event_limit,
        )

    def orchestrate(
        self,
        request: str,
        *,
        context: Optional[Dict[str, object]] = None,
        execute: bool = True,
        observer: Optional[OrchestrationObserver] = None,
    ) -> OrchestrationResult:
        """Run the full orchestration pipeline for *request*."""

        if observer:
            observer.on_stage("start", request)

        combined_context = dict(context or {})
        if self.fabric:
            now = datetime.now(timezone.utc).isoformat()
            self.fabric.merge_metadata({"last_request": request, "last_invocation": now})
            self.fabric.record_event(
                "orchestrator.request",
                {"request": request, "execute": execute},
            )
            snapshot = self.fabric.snapshot(event_limit=self.fabric_event_limit)
            combined_context.setdefault("fabric", snapshot.to_context_payload())

        intent = self.intent_parser.parse(request, combined_context)
        if observer:
            observer.on_stage("intent", intent.action or intent.raw_input)
        if intent.context_snapshot is None:
            intent.context_snapshot = combined_context

        plan = self.planner.create_plan(intent, combined_context)
        if observer:
            observer.on_stage("plan", str(len(plan.steps)))
        safety = self.safety_checker.review(plan, combined_context)
        if observer:
            detail = f"approved={len(safety.approved_steps)} blocked={len(safety.blocked_steps)}"
            observer.on_stage("safety", detail)
        if not safety.approved_steps and plan.steps:
            raise OrchestrationError("All plan steps were blocked by safety checks")

        execution_results: List[ExecutionResult] = []
        reviews: List[PlanReview] = []
        if execute and safety.approved_steps:
            approved_ids: Set[str] = {step.id for step in safety.approved_steps}
            pending_steps: List[PlanStep] = [
                step for step in plan.steps if step.id in approved_ids
            ]
            completed_ids: Set[str] = set()
            step_counter = 0

            if observer:
                observer.on_stage("execution", str(len(pending_steps)))

            while pending_steps:
                step = pending_steps.pop(0)
                if step.id in completed_ids:
                    continue

                total_steps = len([s for s in plan.steps if s.id in approved_ids])
                step_counter += 1
                if observer:
                    observer.on_step_start(step, step_counter, total_steps)

                step_results = self.executor.execute_plan([step], combined_context)
                execution_results.extend(step_results)
                for result in step_results:
                    if observer:
                        observer.on_step_result(result)
                    if result.status not in {"blocked", "error"}:
                        completed_ids.add(result.step_id)

                review = self.planner.review_execution(
                    intent,
                    plan,
                    execution_results,
                    combined_context,
                )
                reviews.append(review)
                if observer:
                    observer.on_review(review)

                if review.plan is not plan:
                    if observer:
                        observer.on_stage("replan", str(len(review.plan.steps)))
                    plan = review.plan
                    safety = self.safety_checker.review(plan, combined_context)
                    if observer:
                        detail = (
                            f"approved={len(safety.approved_steps)} "
                            f"blocked={len(safety.blocked_steps)}"
                        )
                        observer.on_stage("safety", detail)
                    if not safety.approved_steps and plan.steps:
                        raise OrchestrationError(
                            "All plan steps were blocked after planner review"
                        )
                    approved_ids = {step.id for step in safety.approved_steps}

                if review.complete:
                    break

                pending_steps = [
                    step
                    for step in plan.steps
                    if step.id not in completed_ids and step.id in approved_ids
                ]
        else:
            reason = "dry-run" if not execute else "no-approved-steps"
            if observer:
                observer.on_stage("execution_skipped", reason)
            if not execute:
                reviews.append(
                    self.planner.review_execution(intent, plan, execution_results, combined_context)
                )

        if self.fabric:
            self.fabric.merge_metadata(
                {
                    "last_intent_action": intent.action,
                    "last_plan_step_count": len(plan.steps),
                    "last_safety_approved": len(safety.approved_steps),
                    "last_safety_blocked": len(safety.blocked_steps),
                    "last_execution_count": len(execution_results),
                    "dry_run": not execute,
                }
            )
            self.fabric.record_event(
                "orchestrator.completed",
                {
                    "request": request,
                    "approved_steps": len(safety.approved_steps),
                    "blocked_steps": len(safety.blocked_steps),
                    "executed_steps": len(execution_results),
                    "dry_run": not execute,
                },
            )

        if observer:
            observer.on_stage("complete")

        return OrchestrationResult(
            intent=intent,
            plan=plan,
            safety=safety,
            execution=execution_results,
            reviews=reviews,
        )

    def dry_run(self, request: str, context: Optional[Dict[str, object]] = None) -> OrchestrationResult:
        """Run orchestration but skip execution."""

        return self.orchestrate(request, context=context, execute=False)


__all__ = [
    "AinuxOrchestrator",
    "OrchestrationObserver",
    "OrchestrationError",
]
