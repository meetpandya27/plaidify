"""Read-only runtime policy for authenticated site access.

Plaidify promises it never writes to the sites it reads. This module is where
that promise is enforced, in every phase of a run:

* **AUTH / MFA** — blueprint steps may fill and click, but form submissions
  (navigation POSTs, form-encoded / multipart POSTs, PUT/PATCH/DELETE) may only
  go to the targets the blueprint declares for that phase, and clicks that move
  money or change the account are refused.
* **READ** — no fills, selects or JavaScript; no form submissions or
  PUT/PATCH/DELETE (JSON fetches, which single-page apps use to read, stay
  allowed); clicks that commit anything are refused.
* **CLEANUP** — only the declared logout targets may be navigated or submitted
  to, and the same click rules as READ apply (logging out is fine).

Independently of ``enabled``, navigation always stays on http(s) within the
blueprint's own domain and the domains it explicitly allows.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

from src.core.blueprint import BlueprintStep, BlueprintV2, StepAction
from src.core.network_policy import (
    HostRule,
    TargetPattern,
    navigation_block_reason,
    url_matches_targets,
)

FORM_POST_CONTENT_TYPES = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


class ExecutionPhase(str, Enum):
    """High-level browser execution phases."""

    AUTH = "auth"
    MFA = "mfa"
    READ = "read"
    CLEANUP = "cleanup"


# ── Click vocabulary ──────────────────────────────────────────────────────────


def _inflections(word: str) -> set[str]:
    """Common English forms of a verb or noun: pay → pays, paying, paid-ish forms."""
    forms = {word, word + "s", word + "es", word + "ed", word + "ing", word + "er", word + "ers"}
    if word.endswith("e"):
        stem = word[:-1]
        forms |= {stem + "ing", word + "d", word + "r", word + "rs"}
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        stem = word[:-1]
        forms |= {stem + "ies", stem + "ied"}
    if len(word) >= 3 and word[-1] in "bdglmnprt" and word[-2] in "aeiou" and word[-3] not in "aeiou":
        forms |= {word + word[-1] + "ing", word + word[-1] + "ed", word + word[-1] + "er"}
    return forms


def _index(words: Iterable[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for word in words:
        for form in _inflections(word):
            index.setdefault(form, word)
    return index


# Words that move money, buy/sell, or destroy/alter the account. Refused in
# every phase, including login.
_RISKY_WORDS = _index(
    (
        "pay",
        "prepay",
        "autopay",
        "transfer",
        "wire",
        "withdraw",
        "withdrawal",
        "deposit",
        "buy",
        "sell",
        "purchase",
        "trade",
        "checkout",
        "remit",
        "donate",
        "redeem",
        "refund",
        "dispute",
        "liquidate",
        "delete",
        "remove",
        "erase",
        "deactivate",
        "terminate",
        "unsubscribe",
        "enroll",
        "unenroll",
        "upload",
        "approve",
    )
)
# Payment rails and non-English money verbs, matched as whole words.
_RISKY_WORDS.update(
    {
        word: word
        for word in (
            "zelle",
            "venmo",
            "paypal",
            "interac",
            "etransfer",
            "uberweisen",
            "uberweisung",
            "bezahlen",
            "zahlen",
            "payer",
            "virement",
            "virer",
            "transferer",
            "pagar",
            "transferir",
            "transferencia",
            "retirar",
            "comprar",
            "vender",
            "acheter",
            "vendre",
            "kaufen",
            "verkaufen",
            "supprimer",
            "eliminar",
            "loschen",
        )
    }
)
# Risky words that also name a read-only page when plural ("Transfers") or
# qualified ("Transfer history").
_NOUN_LIKE = frozenset({"transfer", "deposit", "withdrawal", "trade", "wire", "purchase", "dispute", "refund"})
_READ_ONLY_QUALIFIERS = frozenset(
    {
        "history",
        "histories",
        "activity",
        "activities",
        "statement",
        "statements",
        "detail",
        "details",
        "summary",
        "record",
        "records",
        "receipt",
        "receipts",
        "report",
        "reports",
        "list",
        "overview",
        "view",
        "status",
        "search",
        "log",
    }
)

# Verb + object pairs that commit a change, refused in every phase.
_COMMIT_VERBS = _index(
    (
        "make",
        "submit",
        "schedule",
        "confirm",
        "send",
        "process",
        "complete",
        "place",
        "execute",
        "authorize",
        "initiate",
        "setup",
        "start",
    )
)
_MONEY_OBJECTS = _index(
    (
        "payment",
        "transfer",
        "order",
        "purchase",
        "withdrawal",
        "deposit",
        "trade",
        "wire",
        "money",
        "fund",
        "cash",
        "donation",
    )
)
_ACCOUNT_VERBS = _index(
    ("close", "cancel", "terminate", "delete", "remove", "deactivate", "freeze", "lock", "stop", "suspend", "end")
)
_ACCOUNT_OBJECTS = _index(
    (
        "account",
        "subscription",
        "service",
        "card",
        "policy",
        "plan",
        "membership",
        "order",
        "payment",
        "autopay",
        "loan",
        "profile",
        "contract",
        "line",
    )
)
_CHANGE_PHRASES = (
    (_index(("replace", "report", "activate")), _index(("card",))),
    (_index(("save", "apply", "submit")), _index(("change", "setting", "preference"))),
    (
        _index(("update", "change", "edit", "reset")),
        _index(("profile", "address", "email", "phone", "password", "pin", "setting", "beneficiary", "payee")),
    ),
    (_index(("open", "apply")), _index(("account", "card", "loan", "credit", "line"))),
    (_index(("add", "link", "register")), _index(("payee", "recipient", "beneficiary", "account", "card", "biller"))),
    (_index(("accept", "sign")), _index(("offer", "agreement", "contract", "term", "document"))),
)
_PHRASE_WINDOW = 3

# Words that commit whatever is on screen. Fine on a login or MFA form,
# refused once signed in (READ / CLEANUP).
_SUBMIT_WORDS = frozenset(
    set(
        _index(
            (
                "submit",
                "confirm",
                "send",
                "save",
                "apply",
                "accept",
                "agree",
                "proceed",
                "continue",
                "finish",
                "authorize",
            )
        )
    )
    | {"yes", "ok", "okay"}
)
_LOGOUT_WORDS = frozenset({"logout", "logoff", "signout", "signoff", "logouts", "signouts"})
_LOGOUT_PAIRS = frozenset({("log", "out"), ("sign", "out"), ("log", "off"), ("sign", "off"), ("end", "session")})

# Characters that render as nothing, and letters that render like Latin ones.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "ё": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "ѕ": "s",
        "і": "i",
        "ї": "i",
        "ј": "j",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
        "Ѕ": "S",
        "І": "I",
        "Ј": "J",
        # Greek
        "α": "a",
        "β": "b",
        "ε": "e",
        "η": "n",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
        # Other look-alikes
        "ı": "i",
        "ℓ": "l",
        "ß": "ss",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "ł": "l",
        "Ł": "L",
    }
)
_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])"
)


def normalize_click_text(value: str) -> list[str]:
    """Tokens a human would read in ``value``.

    Removes invisible characters, folds look-alike letters and accents to plain
    Latin, splits camelCase / snake_case / kebab-case identifiers, and lower-cases.
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = _INVISIBLE.sub("", text)
    text = text.translate(_CONFUSABLES)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = _CAMEL_BOUNDARY.sub(" ", text)
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).lower()
    return text.split()


def _phrase_hit(tokens: list[str], verbs: dict[str, str], objects: dict[str, str]) -> Optional[str]:
    for i, token in enumerate(tokens):
        if token not in verbs:
            continue
        for follower in tokens[i + 1 : i + 1 + _PHRASE_WINDOW]:
            if follower in objects:
                return f"{token} {follower}"
    return None


def _risky_click_reason(tokens: list[str]) -> Optional[str]:
    """A reason when the click could move money or change the account; None otherwise."""
    for verbs, objects in ((_COMMIT_VERBS, _MONEY_OBJECTS), (_ACCOUNT_VERBS, _ACCOUNT_OBJECTS), *_CHANGE_PHRASES):
        phrase = _phrase_hit(tokens, verbs, objects)
        if phrase:
            return f"'{phrase}'"

    qualified = any(token in _READ_ONLY_QUALIFIERS for token in tokens)
    for token in tokens:
        base = _RISKY_WORDS.get(token)
        if base is None:
            continue
        if base in _NOUN_LIKE and (qualified or token in (base + "s", base + "es")):
            continue  # "Transfers", "Transfer history", "View deposits"
        return f"'{token}'"
    return None


def _is_logout(tokens: list[str]) -> bool:
    if any(token in _LOGOUT_WORDS for token in tokens):
        return True
    # "Sign out", "Log me out", "End my session"
    return any(
        (token, follower) in _LOGOUT_PAIRS for i, token in enumerate(tokens) for follower in tokens[i + 1 : i + 3]
    )


# ── Policy ────────────────────────────────────────────────────────────────────


@dataclass
class BlockedAction:
    """A single action blocked by the strict read-only runtime policy."""

    phase: ExecutionPhase
    action: str
    reason: str
    target: Optional[str] = None


def _strip_query(url: str) -> str:
    return (url or "").split("?", 1)[0].split("#", 1)[0]


@dataclass
class ReadOnlyExecutionPolicy:
    """Mutable policy state shared across engine, browser, and step execution."""

    enabled: bool = True
    phase: ExecutionPhase = ExecutionPhase.AUTH
    blocked_actions: list[BlockedAction] = field(default_factory=list)
    host_rules: tuple[HostRule, ...] = ()
    auth_targets: tuple[TargetPattern, ...] = ()
    mfa_targets: tuple[TargetPattern, ...] = ()
    logout_targets: tuple[TargetPattern, ...] = ()

    @classmethod
    def for_blueprint(cls, blueprint: BlueprintV2, *, enabled: bool = True) -> ReadOnlyExecutionPolicy:
        """The policy for one run of ``blueprint``, scoped to its domains and declared targets."""
        return cls(
            enabled=enabled,
            host_rules=tuple(blueprint.host_rules()),
            auth_targets=tuple(blueprint.auth_targets()),
            mfa_targets=tuple(blueprint.mfa_targets()),
            logout_targets=tuple(blueprint.cleanup_targets()),
        )

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

    # ── Navigation ────────────────────────────────────────────────────────

    def evaluate_navigation(self, url: str) -> Optional[str]:
        """Why the browser may not navigate to ``url`` right now (a goto step or a main-frame navigation)."""
        reason = navigation_block_reason(url, self.host_rules)
        if reason:
            return reason
        if self.enabled and self.phase == ExecutionPhase.CLEANUP:
            if not url_matches_targets(url, self.logout_targets, self.host_rules):
                return f"cleanup may only navigate to the blueprint's logout_targets, not {_strip_query(url)}"
        return None

    # ── Steps ─────────────────────────────────────────────────────────────

    def evaluate_step(self, step: BlueprintStep) -> Optional[str]:
        if not self.enabled:
            return None

        if self.phase in {ExecutionPhase.READ, ExecutionPhase.CLEANUP}:
            if step.action in {StepAction.FILL, StepAction.SELECT}:
                return f"{step.action.value} steps are blocked after authentication in strict read-only mode"

            if step.action == StepAction.EXECUTE_JS:
                return "execute_js steps are blocked after authentication in strict read-only mode"

        return None

    # ── Requests ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_navigation(request: Any) -> bool:
        try:
            probe = getattr(request, "is_navigation_request", None)
            return bool(callable(probe) and probe())
        except Exception:
            return False

    @staticmethod
    def _is_main_frame(request: Any) -> bool:
        try:
            frame = request.frame
        except Exception:
            return True  # unknown (e.g. a service worker): treat as the main frame
        return getattr(frame, "parent_frame", None) is None

    @staticmethod
    def _redirect_root(request: Any) -> Any:
        root = request
        for _ in range(32):
            previous = getattr(root, "redirected_from", None)
            if previous is None:
                break
            root = previous
        return root

    def evaluate_request(self, request: Any) -> Optional[str]:
        url = str(getattr(request, "url", "") or "")
        method = str(getattr(request, "method", "GET")).upper()
        is_navigation = self._is_navigation(request)
        main_frame = is_navigation and self._is_main_frame(request)

        if url and main_frame:
            reason = navigation_block_reason(url, self.host_rules)
            if reason:
                return reason

        if not self.enabled:
            return None

        headers = getattr(request, "headers", {}) or {}
        content_type = str(headers.get("content-type", "")).lower()
        is_form_post = method == "POST" and any(content_type.startswith(prefix) for prefix in FORM_POST_CONTENT_TYPES)

        if self.phase == ExecutionPhase.READ:
            if method in _WRITE_METHODS:
                return f"{method} requests are blocked after authentication in strict read-only mode"
            if method == "POST":
                if is_navigation:
                    return "navigation POST requests are blocked after authentication in strict read-only mode"
                if is_form_post:
                    return "form submissions are blocked after authentication in strict read-only mode"
            return None

        if self.phase in {ExecutionPhase.AUTH, ExecutionPhase.MFA}:
            submits = method in _WRITE_METHODS or (method == "POST" and (is_navigation or is_form_post))
            if not submits:
                return None
            targets = self.auth_targets if self.phase == ExecutionPhase.AUTH else self.mfa_targets + self.auth_targets
            if url and url_matches_targets(url, targets, self.host_rules):
                return None
            label = "login" if self.phase == ExecutionPhase.AUTH else "MFA"
            return (
                f"{method} submission to {_strip_query(url) or 'an unknown URL'} is not one of the blueprint's "
                f"declared {label} submit_targets"
            )

        # CLEANUP: only the declared logout targets.
        if method not in _SAFE_METHODS:
            if url and url_matches_targets(url, self.logout_targets, self.host_rules):
                return None
            return (
                f"{method} request to {_strip_query(url)} during cleanup is not one of the blueprint's logout_targets"
            )
        if main_frame:
            root_url = str(getattr(self._redirect_root(request), "url", "") or url)
            if url_matches_targets(root_url, self.logout_targets, self.host_rules):
                return None
            return f"cleanup may only navigate to the blueprint's logout_targets, not {_strip_query(url)}"
        return None

    # ── Clicks ────────────────────────────────────────────────────────────

    def evaluate_click(self, selector: str, metadata: Optional[dict[str, Any]] = None) -> Optional[str]:
        """Why a click on this element is refused in the current phase, or None."""
        if not self.enabled:
            return None

        descriptor_parts = [selector or ""]
        if metadata:
            descriptor_parts.extend(str(value) for value in metadata.values() if value)
        # Each part (text, href, id, form action, …) is judged on its own, so a
        # harmless label cannot excuse a link to /transfer/new.
        part_tokens = [normalize_click_text(part) for part in descriptor_parts if part]
        tokens = [token for part in part_tokens for token in part]
        if not tokens:
            return None

        for part in part_tokens:
            risky = _risky_click_reason(part)
            if risky:
                return (
                    f"click target looks like it moves money or changes the account ({risky}) "
                    f"in the {self.phase.value} phase"
                )

        if self.phase in {ExecutionPhase.READ, ExecutionPhase.CLEANUP}:
            if self.phase == ExecutionPhase.CLEANUP and _is_logout(tokens):
                return None
            for token in tokens:
                if token in _SUBMIT_WORDS:
                    return f"click target would submit or confirm something ('{token}') after authentication"

        return None


def _normalize_descriptor(value: str) -> str:
    """Normalized, space-separated form of a click descriptor (kept for callers and tests)."""
    return " ".join(normalize_click_text(value))
