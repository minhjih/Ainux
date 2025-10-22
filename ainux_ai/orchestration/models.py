"""Data structures for the natural language orchestration pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Intent:
    """Structured representation of a user's natural-language request."""

    raw_input: str
    action: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reasoning: Optional[str] = None
    context_snapshot: Optional[Dict[str, Any]] = None


@dataclass
class PlanStep:
    """Atomic action produced by the planner."""

    id: str
    action: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class ActionPlan:
    """Ordered plan returned by the planner."""

    intent: Intent
    steps: List[PlanStep] = field(default_factory=list)
    notes: Optional[str] = None


@dataclass
class PlanReview:
    """Planner feedback emitted after each execution round."""

    plan: ActionPlan
    next_steps: List[PlanStep] = field(default_factory=list)
    complete: bool = False
    message: Optional[str] = None


@dataclass
class SafetyReport:
    """Planner review outcome describing approved and blocked actions."""

    approved_steps: List[PlanStep] = field(default_factory=list)
    blocked_steps: List[PlanStep] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rationale: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result emitted for every executed plan step."""

    step_id: str
    status: str
    output: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OrchestrationResult:
    """High-level summary returned by the orchestrator."""

    intent: Intent
    plan: ActionPlan
    safety: SafetyReport
    execution: List[ExecutionResult]
    reviews: List[PlanReview] = field(default_factory=list)
