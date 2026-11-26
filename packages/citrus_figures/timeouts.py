"""One Worker timeout policy for every figure execution (audit F-05).

Why this module exists
----------------------
The service used to answer "how long may a figure run?" twice, in two
unrelated places: ``FIGURES_SANDBOX_TIMEOUT_SEC`` (default 120 s) for the
general script sandbox and a hard-coded ``ExecutionLimits(timeout_sec=90)``
for the pinned Nature template worker. Those two numbers were not a policy;
they were two guesses that disagreed. A long, legitimate script failed at
120 s while a short template job was allowed 90 s regardless of its data
size, its figure count or the account's remaining deadline.

The rule
--------
A timeout is derived from what the job actually asks the worker to do:

* the **kind** of execution (analysis-only, arbitrary script, fixed template);
* the **input size** the worker must read and parse;
* the **number of figures** it must draw;
* the **complexity** of the requested template;
* the **absolute deadline** the account already carries.

The result is clamped to a documented minimum and maximum so no request can
turn into an unbounded job. It is deterministic and pure, so a receipt can
print the policy that produced a timeout and a test can pin it.

What it must never be confused with
-----------------------------------
This is *wall-clock Worker* time. It is not the model output budget
(``model_length``/token budget) and must never be reported as one: a Worker
timeout is a Worker timeout.
"""
from __future__ import annotations

from typing import Any

# Policy identity. It travels with every receipt so a timeout can be explained
# later; bump it whenever the numbers below change.
POLICY_VERSION = "figure-timeout/2"

# Base budgets per execution kind. These replace the two legacy numbers
# (120 s script sandbox / 90 s Nature template worker) with one table.
BASE_SECONDS: dict[str, int] = {
    "analysis": 60,
    "script": 150,
    "template": 150,
    "plot": 150,
}

# Reading, parsing and drawing scale with the frozen input snapshot. The
# allowance is bounded separately so a 32 MiB CSV cannot demand minutes.
PER_MIB_SECONDS = 3
MAX_SIZE_SECONDS = 90

# Every additional figure in one execution re-imports nothing but does pay
# draw + export + QA per figure.
PER_FIGURE_SECONDS = 30

# Template complexity multipliers, keyed by template id. Templates that lay out
# several axes (marginal) or annotate many points cost more than a scatter.
TEMPLATE_COMPLEXITY: dict[str, float] = {
    "volcano": 1.0,
    "roc": 1.0,
    "dotplot": 1.1,
    "marginal": 1.2,
    "paired": 1.0,
}

MINIMUM_SECONDS = 30
MAXIMUM_SECONDS = 900

# A Worker is stopped a little before the account deadline so the ledger can
# still record the outcome instead of being cut off by the enclosing budget.
DEADLINE_GRACE_SECONDS = 15

# The two historical defaults. A configured value equal to either one is not
# an operator decision — it is the old value still sitting in a deployment
# file, so the policy keeps authority. Any other configured value is honoured
# as an explicit ceiling.
LEGACY_DEFAULTS = frozenset({120, 90})

_FIGURE_LIMIT = 8
_INPUT_LIMIT = 64 << 20


def execution_kind(mode: str = "", *, template_id: str = "", figures: int = 1) -> str:
    """Classify one request without trusting the caller's label."""
    value = str(mode or "").strip().lower()
    if value == "template" or template_id:
        return "template"
    if value in BASE_SECONDS:
        return value
    return "script" if figures else "analysis"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def resolve_timeout(*, kind: str = "script", input_bytes: int = 0, figures: int = 1,
                    template_id: str = "", deadline_sec: int | None = None,
                    configured_sec: int | None = None) -> int:
    """Return the wall-clock seconds one Worker execution may use.

    ``configured_sec`` is an operator *ceiling*: an explicit value caps the
    granted budget, it never raises it above the policy. The legacy 120/90
    values carry no such intent — they are the two numbers this policy
    replaces — so they are ignored. Non-integer or out-of-range configured
    values are ignored too, rather than silently widening every job.
    """
    try:
        base = int(BASE_SECONDS.get(str(kind or "script"), BASE_SECONDS["script"]))
    except (KeyError, TypeError):
        base = BASE_SECONDS["script"]
    if type(input_bytes) is not int or input_bytes < 0:
        input_bytes = 0
    size_seconds = min(_INPUT_LIMIT, input_bytes) // (1 << 20) * PER_MIB_SECONDS
    size_seconds = min(size_seconds, MAX_SIZE_SECONDS)
    count = _clamp(int(figures) if type(figures) is int else 1, 1, _FIGURE_LIMIT)
    complexity = TEMPLATE_COMPLEXITY.get(str(template_id or ""), 1.0)
    total = (base + size_seconds + (count - 1) * PER_FIGURE_SECONDS) * complexity
    total = _clamp(round(total), MINIMUM_SECONDS, MAXIMUM_SECONDS)
    if deadline_sec is not None and type(deadline_sec) is int:
        # Never plan past the account deadline; the floor still applies so a
        # nearly-exhausted turn gets a real chance instead of one second.
        total = _clamp(min(total, deadline_sec - DEADLINE_GRACE_SECONDS),
                       MINIMUM_SECONDS, MAXIMUM_SECONDS)
    if type(configured_sec) is int and configured_sec > 0 and configured_sec not in LEGACY_DEFAULTS:
        total = _clamp(min(total, configured_sec), MINIMUM_SECONDS, MAXIMUM_SECONDS)
    return int(total)


def policy() -> dict[str, Any]:
    """The printable identity of this policy, for receipts and error messages."""
    return {
        "version": POLICY_VERSION,
        "base_seconds": dict(BASE_SECONDS),
        "per_mib_seconds": PER_MIB_SECONDS,
        "max_size_seconds": MAX_SIZE_SECONDS,
        "per_figure_seconds": PER_FIGURE_SECONDS,
        "template_complexity": dict(TEMPLATE_COMPLEXITY),
        "minimum_seconds": MINIMUM_SECONDS,
        "maximum_seconds": MAXIMUM_SECONDS,
        "deadline_grace_seconds": DEADLINE_GRACE_SECONDS,
        "legacy_defaults_ignored": sorted(LEGACY_DEFAULTS),
    }


def describe(*, kind: str, timeout_sec: int, input_bytes: int = 0, figures: int = 1,
             template_id: str = "") -> dict[str, Any]:
    """One receipt row: what was granted, why, and under which policy."""
    return {"policy": POLICY_VERSION, "kind": str(kind), "timeout_sec": int(timeout_sec),
            "input_bytes": int(input_bytes or 0), "figures": int(figures or 1),
            "template": str(template_id or ""), "unit": "worker_wall_clock_seconds"}


__all__ = ["BASE_SECONDS", "DEADLINE_GRACE_SECONDS", "LEGACY_DEFAULTS", "MAXIMUM_SECONDS",
           "MINIMUM_SECONDS", "PER_FIGURE_SECONDS", "PER_MIB_SECONDS", "POLICY_VERSION",
           "TEMPLATE_COMPLEXITY", "describe", "execution_kind", "policy", "resolve_timeout"]
