"""Evaluation harness.

Runs the golden set in `cases.yaml` against the real agent and scores three
things separately, because they fail for different reasons and need different
fixes:

  * tool selection  — did it reach for the right capability?
  * content         — is the answer actually right?
  * guardrails      — did the safety behaviour hold?

Assertions are deterministic substring and state checks rather than an LLM
judge. For this domain that is a feature: the answers contain asset codes,
names and dates, so correctness is checkable exactly, and the eval suite itself
introduces no model non-determinism or extra cost.

Every case gets a fresh copy of the database, so a committed write in one case
cannot leak into another and the `expect_db_unchanged` assertion is meaningful.

Usage:
    python -m evals.runner                     # everything
    python -m evals.runner --filter 7-writes   # one category
    python -m evals.runner --limit 5           # bound free-tier spend
    python -m evals.runner --dry-run           # validate the case file only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from assistant.agent.memory import SessionStore
from assistant.agent.runtime import AgentRuntime, TurnResult
from assistant.config import get_settings
from assistant.db import seed
from assistant.llm import LLMError, get_client
from assistant.obs.trace import TraceSink

EVALS_DIR = Path(__file__).parent
CASES_PATH = EVALS_DIR / "cases.yaml"
REPORT_PATH = EVALS_DIR / "report.md"
RESULTS_PATH = EVALS_DIR / "results.json"


# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    dimension: str  # tools | content | guardrail
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    category: str
    checks: list[Check] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]


# --------------------------------------------------------------------------


def _db_fingerprint(db_path: Path) -> str:
    """Hash of asset state, so 'did anything change?' is exact rather than guessed."""
    from assistant.db import repo

    with repo.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_code, employee_id, status, location FROM assets ORDER BY asset_code"
        ).fetchall()
    payload = json.dumps([tuple(row) for row in rows], default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalise(text: str) -> str:
    """Fold typographic variants onto ASCII before matching.

    Models emit U+202F (narrow no-break space) inside names and U+2011
    (non-breaking hyphen) inside compounds, so a raw substring test rejects
    "Amit Kumar" against a reply that reads exactly that on screen. Scoring the
    typography instead of the answer makes the eval report correct behaviour as
    a failure — which it did, on four cases, until this was fixed.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(" " if ch.isspace() else ch for ch in folded)
    folded = folded.replace("‑", "-").replace("–", "-").replace("—", "-")
    folded = folded.replace("‘", "'").replace("’", "'")
    folded = folded.replace("“", '"').replace("”", '"')
    return " ".join(folded.split()).lower()


def _contains(haystack: str, needle: str) -> bool:
    return _normalise(needle) in _normalise(haystack)


def _assert_turn(
    checks: list[Check],
    spec: dict[str, Any],
    result: TurnResult,
    reply: str,
    *,
    db_before: str,
    db_after: str,
    prefix: str = "",
) -> None:
    def add(name: str, dimension: str, passed: bool, detail: str = "") -> None:
        checks.append(Check(name=f"{prefix}{name}", dimension=dimension, passed=passed, detail=detail))

    if expected := spec.get("expect_tools"):
        missing = [tool for tool in expected if tool not in result.tools_used]
        add(
            f"calls {', '.join(expected)}",
            "tools",
            not missing,
            f"used {result.tools_used or 'no tools'}",
        )

    if forbidden := spec.get("forbid_tools"):
        used = [tool for tool in forbidden if tool in result.tools_used]
        add(f"avoids {', '.join(forbidden)}", "tools", not used, f"used {used}")

    for needle in spec.get("expect_contains", []):
        add(f"mentions {needle!r}", "content", _contains(reply, needle))

    if options := spec.get("expect_any_of"):
        add(
            f"mentions one of {options}",
            "content",
            any(_contains(reply, option) for option in options),
        )

    for needle in spec.get("forbid_contains", []):
        add(f"omits {needle!r}", "content", not _contains(reply, needle))

    if spec.get("expect_citations"):
        add("cites policy", "content", bool(result.citations), f"citations={result.citations}")

    if spec.get("expect_pending_write"):
        add(
            "previews the write",
            "guardrail",
            result.pending_confirmation is not None,
        )

    if spec.get("expect_db_unchanged"):
        add("database unchanged", "guardrail", db_before == db_after)

    if spec.get("expect_db_changed"):
        add("database updated", "guardrail", db_before != db_after)

    if rule := spec.get("expect_guardrail"):
        fired = [g["rule"] for g in result.guardrails]
        add(f"guardrail {rule} fires", "guardrail", rule in fired, f"fired={fired}")

    if outcome := spec.get("expect_outcome"):
        add(f"outcome is {outcome}", "guardrail", result.outcome == outcome, result.outcome)


# --------------------------------------------------------------------------


def run_case(case: dict[str, Any], runtime_factory, base_db: Path) -> CaseResult:
    result = CaseResult(case_id=case["id"], category=case.get("category", "uncategorised"))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "case.db"
        shutil.copy(base_db, db_path)
        runtime = runtime_factory(db_path)
        session_id = f"eval-{case['id']}"

        turns = case.get("turns") or [{k: v for k, v in case.items() if k not in {"id", "category"}}]
        started = time.time()

        for index, turn in enumerate(turns):
            prefix = f"turn {index + 1}: " if len(turns) > 1 else ""
            db_before = _db_fingerprint(db_path)
            try:
                turn_result = runtime.run_turn(session_id, turn["prompt"])
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                result.error = f"{type(exc).__name__}: {exc}"
                return result

            db_after = _db_fingerprint(db_path)
            result.replies.append(turn_result.reply)
            result.tools_used.extend(turn_result.tools_used)
            result.input_tokens += turn_result.usage.get("input_tokens", 0)
            result.output_tokens += turn_result.usage.get("output_tokens", 0)

            _assert_turn(
                result.checks,
                turn,
                turn_result,
                turn_result.reply,
                db_before=db_before,
                db_after=db_after,
                prefix=prefix,
            )

        result.duration_ms = (time.time() - started) * 1000
    return result


# --------------------------------------------------------------------------


def build_report(results: list[CaseResult], model: str) -> str:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    errored = [r for r in results if r.error]

    by_dimension: dict[str, list[Check]] = {}
    for result in results:
        for check in result.checks:
            by_dimension.setdefault(check.dimension, []).append(check)

    by_category: dict[str, list[CaseResult]] = {}
    for result in results:
        by_category.setdefault(result.category, []).append(result)

    lines = [
        "# Evaluation report",
        "",
        f"- Model: `{model}`",
        f"- Cases: **{passed}/{total} passed** ({passed / total:.0%})" if total else "- No cases run",
        f"- Errored before scoring: {len(errored)}" if errored else "",
        f"- Total latency: {sum(r.duration_ms for r in results) / 1000:.1f}s "
        f"(median {sorted(r.duration_ms for r in results)[total // 2] / 1000:.1f}s per case)"
        if total
        else "",
        f"- Tokens: {sum(r.input_tokens for r in results):,} in / "
        f"{sum(r.output_tokens for r in results):,} out",
        "",
        "## Scores by dimension",
        "",
        "| Dimension | Passed | Total | Rate |",
        "|---|---:|---:|---:|",
    ]
    for dimension in ("tools", "content", "guardrail"):
        checks = by_dimension.get(dimension, [])
        if not checks:
            continue
        ok = sum(1 for c in checks if c.passed)
        lines.append(f"| {dimension} | {ok} | {len(checks)} | {ok / len(checks):.0%} |")

    lines += ["", "## Scores by category", "", "| Category | Passed | Total | Rate |", "|---|---:|---:|---:|"]
    for category in sorted(by_category):
        cases = by_category[category]
        ok = sum(1 for c in cases if c.passed)
        lines.append(f"| {category} | {ok} | {len(cases)} | {ok / len(cases):.0%} |")

    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for result in failures:
            lines.append(f"### `{result.case_id}` ({result.category})")
            if result.error:
                lines.append(f"- **errored**: {result.error}")
            for check in result.failures():
                detail = f" — {check.detail}" if check.detail else ""
                lines.append(f"- ❌ {check.name}{detail}")
            if result.replies:
                snippet = result.replies[-1].replace("\n", " ")[:300]
                lines.append(f"- reply: _{snippet}_")
            lines.append("")
    else:
        lines += ["", "All cases passed.", ""]

    lines += [
        "## Guardrail assertions",
        "",
        "These are the ones that matter most — they assert the system stays safe "
        "regardless of what the model decides to do.",
        "",
    ]
    guardrail_checks = by_dimension.get("guardrail", [])
    for check in guardrail_checks:
        mark = "✅" if check.passed else "❌"
        lines.append(f"- {mark} {check.name}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the golden evaluation set.")
    parser.add_argument("--filter", help="only cases whose id or category contains this string")
    parser.add_argument("--limit", type=int, help="stop after N cases (bounds free-tier spend)")
    parser.add_argument("--dry-run", action="store_true", help="validate cases without calling the model")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument(
        "--delay",
        type=float,
        default=45.0,
        help=(
            "seconds to wait between cases (default 45). A case costs ~4-5k tokens "
            "and the Groq free tier allows 8k/minute, so running flat out rate-limits "
            "itself and scores the provider rather than the agent. Set 0 on a paid tier."
        ),
    )
    args = parser.parse_args(argv)

    cases = yaml.safe_load(args.cases.read_text())
    if args.filter:
        needle = args.filter.lower()
        cases = [
            c for c in cases
            if needle in c["id"].lower() or needle in c.get("category", "").lower()
        ]
    if args.limit:
        cases = cases[: args.limit]

    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        duplicates = {i for i in ids if ids.count(i) > 1}
        print(f"FAIL — duplicate case ids: {sorted(duplicates)}")
        return 1

    if args.dry_run:
        categories = sorted({c.get("category", "uncategorised") for c in cases})
        turn_count = sum(len(c.get("turns") or [1]) for c in cases)
        print(f"{len(cases)} cases across {len(categories)} categories, {turn_count} model turns")
        for category in categories:
            count = sum(1 for c in cases if c.get("category") == category)
            print(f"  {category:<22} {count}")
        return 0

    settings = get_settings()

    # Build the reference database once; each case works on a copy.
    with tempfile.TemporaryDirectory() as tmp:
        base_db = Path(tmp) / "base.db"
        seed.build(base_db, settings.source_xlsx)

        try:
            client = get_client()
        except LLMError as exc:
            print(f"Cannot run evaluation: {exc}")
            print("Set GEMINI_API_KEY in .env, then re-run. `--dry-run` works without a key.")
            return 2

        try:
            from assistant.rag.retriever import build_retriever

            retriever = build_retriever()
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: policy retrieval unavailable ({exc}); policy cases will fail.")
            retriever = None

        trace_sink = TraceSink(EVALS_DIR / "eval_traces.jsonl")

        def runtime_factory(db_path: Path) -> AgentRuntime:
            runtime = AgentRuntime(
                client,
                sessions=SessionStore(),
                retriever=retriever,
                db_path=db_path,
                trace_sink=trace_sink,
            )
            # The local limiter guards the app against a runaway caller; it is not
            # what constrains the harness. The provider's tokens-per-minute ceiling
            # is, and `--delay` is what respects it.
            runtime.rate_limiter.max_requests = 10_000
            return runtime

        results: list[CaseResult] = []
        for index, case in enumerate(cases, start=1):
            if index > 1 and args.delay:
                time.sleep(args.delay)
            print(f"[{index}/{len(cases)}] {case['id']} … ", end="", flush=True)
            result = run_case(case, runtime_factory, base_db)
            results.append(result)
            if result.error:
                print(f"ERROR ({result.error})")
            elif result.passed:
                print(f"pass ({result.duration_ms / 1000:.1f}s)")
            else:
                print(f"FAIL: {'; '.join(c.name for c in result.failures())}")

    report = build_report(results, settings.active_model)
    REPORT_PATH.write_text(report, encoding="utf-8")
    RESULTS_PATH.write_text(
        json.dumps(
            [
                {
                    "case_id": r.case_id,
                    "category": r.category,
                    "passed": r.passed,
                    "error": r.error,
                    "tools_used": r.tools_used,
                    "duration_ms": r.duration_ms,
                    "checks": [
                        {"name": c.name, "dimension": c.dimension, "passed": c.passed, "detail": c.detail}
                        for c in r.checks
                    ],
                    "replies": r.replies,
                }
                for r in results
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} cases passed — report written to {REPORT_PATH}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
