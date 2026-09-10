"""Loader and enforcement for safety_envelope.yaml.

The envelope is the one place that decides whether a value may be applied. It
is loaded from disk on every check rather than cached, so an operator can
tighten a bound while a tuning session is running and have it take effect
immediately -- the alternative, a bound edited in a panic that does not apply
until restart, is exactly the wrong failure mode.

Three outcomes, and the distinction matters:

``ALLOWED``   inside the safe tier; apply it.
``CONFIRM``   between safe and hard; apply only with confirm=True.
``REFUSED``   beyond a hard bound; never applied, whatever the caller passes.

Out-of-range values are *rejected*, never silently clamped. A silent clamp would
leave the caller believing it applied one config while the hardware ran another,
and every benchmark taken afterwards would be attributed to the wrong settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_ENVELOPE_PATH = Path(__file__).with_name("safety_envelope.yaml")
ENVELOPE_PATH_ENV = "BC250_SAFETY_ENVELOPE"


class Verdict(str, Enum):
    ALLOWED = "allowed"
    CONFIRM = "requires_confirmation"
    REFUSED = "refused"


@dataclass
class Check:
    verdict: Verdict
    parameter: str
    value: Any
    reason: str
    safe_bound: Any = None
    hard_bound: Any = None

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.ALLOWED

    def permitted(self, confirm: bool) -> bool:
        """Whether this value may be applied given the caller's confirm flag."""
        if self.verdict is Verdict.ALLOWED:
            return True
        if self.verdict is Verdict.CONFIRM:
            return confirm
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "parameter": self.parameter,
            "value": self.value,
            "reason": self.reason,
            "safe_bound": self.safe_bound,
            "hard_bound": self.hard_bound,
        }


class EnvelopeError(RuntimeError):
    """Raised when the envelope itself cannot be loaded.

    This is fatal by design. An unreadable envelope must never degrade into
    "no limits" -- if we cannot establish the bounds, we do not write.
    """


def envelope_path() -> Path:
    override = os.environ.get(ENVELOPE_PATH_ENV)
    return Path(override) if override else DEFAULT_ENVELOPE_PATH


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or envelope_path()
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise EnvelopeError("PyYAML is required to read the safety envelope") from exc

    try:
        raw = path.read_text()
    except OSError as exc:
        raise EnvelopeError(f"cannot read safety envelope at {path}: {exc}") from exc

    try:
        doc = yaml.safe_load(raw)
    except Exception as exc:
        raise EnvelopeError(f"safety envelope at {path} is not valid YAML: {exc}") from exc

    if not isinstance(doc, dict):
        raise EnvelopeError(f"safety envelope at {path} is not a mapping")
    return doc


def _get(doc: dict[str, Any], section: str, key: str) -> Any:
    value = doc.get(section, {})
    if not isinstance(value, dict):
        raise EnvelopeError(f"envelope section '{section}' is not a mapping")
    if key not in value:
        raise EnvelopeError(f"envelope is missing required key '{section}.{key}'")
    return value[key]


def check_upper(
    parameter: str,
    value: float,
    *,
    section: str,
    hard_key: str,
    safe_key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> Check:
    """Check a value against an upper bound pair (safe below, hard above)."""
    doc = doc if doc is not None else load()
    hard = _get(doc, section, hard_key)
    safe = _get(doc, section, safe_key) if safe_key else hard

    if value > hard:
        return Check(
            Verdict.REFUSED,
            parameter,
            value,
            f"{value} exceeds the hard ceiling of {hard}; refused regardless of "
            "confirm. Raise it in safety_envelope.yaml if you truly intend this.",
            safe,
            hard,
        )
    if value > safe:
        return Check(
            Verdict.CONFIRM,
            parameter,
            value,
            f"{value} is above the safe bound of {safe} (hard ceiling {hard}); "
            "requires confirm=True",
            safe,
            hard,
        )
    return Check(
        Verdict.ALLOWED, parameter, value, f"{value} is within the safe bound of {safe}",
        safe, hard,
    )


def check_lower(
    parameter: str,
    value: float,
    *,
    section: str,
    hard_key: str,
    safe_key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> Check:
    """Check a value against a lower bound pair (safe above, hard below)."""
    doc = doc if doc is not None else load()
    hard = _get(doc, section, hard_key)
    safe = _get(doc, section, safe_key) if safe_key else hard

    if value < hard:
        return Check(
            Verdict.REFUSED,
            parameter,
            value,
            f"{value} is below the hard floor of {hard}; refused regardless of confirm",
            safe,
            hard,
        )
    if value < safe:
        return Check(
            Verdict.CONFIRM,
            parameter,
            value,
            f"{value} is below the safe bound of {safe} (hard floor {hard}); "
            "requires confirm=True",
            safe,
            hard,
        )
    return Check(
        Verdict.ALLOWED, parameter, value, f"{value} is within the safe bound of {safe}",
        safe, hard,
    )


def worst(checks: list[Check]) -> Check:
    """Return the most severe check, so a caller can fail on one verdict."""
    if not checks:
        raise ValueError("worst() requires at least one check")
    order = {Verdict.REFUSED: 0, Verdict.CONFIRM: 1, Verdict.ALLOWED: 2}
    return min(checks, key=lambda c: order[c.verdict])


def summary(path: Path | None = None) -> dict[str, Any]:
    """The envelope as data, for the agent to reason about before proposing values."""
    doc = load(path)
    return {
        "path": str(path or envelope_path()),
        "version": doc.get("version"),
        **{k: v for k, v in doc.items() if k != "version"},
    }
