"""Read-only runtime policy for authenticated site access.

This module centralizes the strict post-auth behavior that Plaidify applies to
all browser-driven blueprints. The policy is intentionally conservative after
authentication, but still permits the bounded mutations required for login and
MFA.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from src.core.blueprint import BlueprintStep, StepAction

FORM_POST_CONTENT_TYPES = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
)

RISKY_CLICK_PATTERNS = (
    re.compile(r"\bpay\s+now\b"),
    re.compile(r"\b(make|submit|schedule)\s+payment\b"),
    re.compile(r"\btransfer\b"),
    re.compile(r"\bsend\s+money\b"),
    re.compile(r"\bwithdraw\b"),
    re.compile(r"\bdeposit\b"),
    re.compile(r"\bdelete\b"),
    re.compile(r"\bremove\b"),
    re.compile(r"\bclose\s+account\b"),
    re.compile(r"\breplace\s+card\b"),
    re.compile(r"\bupload\b"),
    re.compile(r"\bsave\s+changes?\b"),
    re.compile(r"\bupdate\s+profile\b"),
    re.compile(r"\bconfirm\s+(payment|transfer)\b"),
    re.compile(r"\bapprove\b"),
)


class ExecutionPhase(str, Enum):
    """High-level browser execution phases."""

    AUTH = "auth"
    MFA = "mfa"
    READ = "read"
    CLEANUP = "cleanup"


@dataclass
class BlockedAction:
    """A single action blocked by the strict read-only runtime policy."""

    phase: ExecutionPhase
    action: str
    reason: str
    target: Optional[str] = None


@dataclass
class ReadOnlyExecutionPolicy:
    """Mutable policy state shared across engine, browser, and step execution."""

    enabled: bool = True
    phase: ExecutionPhase = ExecutionPhase.AUTH
    blocked_actions: list[BlockedAction] = field(default_factory=list)

    def set_phase(self, phase: ExecutionPhase) -> None:
        self.phase = phase

    def record_blocked(self, action: str, reason: str, target: Optional[str] = None) -> None:
        self.blocked_actions.append(
            BlockedAction(
                phase=self.phase,
                action=action,
                reason=reason,
                target=target,
            )
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "final_phase": self.phase.value,
            "blocked_action_count": len(self.blocked_actions),
            "blocked_actions": [asdict(action) for action in self.blocked_actions],
        }

    def evaluate_step(self, step: BlueprintStep) -> Optional[str]:
        if not self.enabled:
            return None

        if self.phase in {ExecutionPhase.READ, ExecutionPhase.CLEANUP}:
            if step.action in {StepAction.FILL, StepAction.SELECT}:
                return f"{step.action.value} steps are blocked after authentication in strict read-only mode"

            if step.action == StepAction.EXECUTE_JS:
                return "execute_js steps are blocked after authentication in strict read-only mode"

        return None

    def evaluate_request(self, request: Any) -> Optional[str]:
        if not self.enabled or self.phase != ExecutionPhase.READ:
            return None

        method = str(getattr(request, "method", "GET")).upper()
        if method in {"PUT", "PATCH", "DELETE"}:
            return f"{method} requests are blocked after authentication in strict read-only mode"

        if method != "POST":
            return None

        try:
            if callable(getattr(request, "is_navigation_request", None)) and request.is_navigation_request():
                return "navigation POST requests are blocked after authentication in strict read-only mode"
        except Exception:
            pass

        headers = getattr(request, "headers", {}) or {}
        content_type = str(headers.get("content-type", "")).lower()
        if any(content_type.startswith(prefix) for prefix in FORM_POST_CONTENT_TYPES):
            return "form submissions are blocked after authentication in strict read-only mode"

        return None

    def evaluate_click(self, selector: str, metadata: Optional[dict[str, Any]] = None) -> Optional[str]:
        if not self.enabled or self.phase != ExecutionPhase.READ:
            return None

        descriptor_parts = [selector]
        if metadata:
            descriptor_parts.extend(str(value) for value in metadata.values() if value)

        descriptor = _normalize_descriptor(" ".join(descriptor_parts))
        if not descriptor:
            return None

        for pattern in RISKY_CLICK_PATTERNS:
            if pattern.search(descriptor):
                return f"click target matched a risky action pattern after authentication: {pattern.pattern}"

        return None


def _normalize_descriptor(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
