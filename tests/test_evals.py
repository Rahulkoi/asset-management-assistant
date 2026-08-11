"""Tests for the evaluation harness itself.

A scoring harness that quietly reports "pass" is worse than having none, so the
scorer is tested against known-good and known-bad agent behaviour using a
scripted model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from assistant.agent.memory import SessionStore
from assistant.agent.runtime import AgentRuntime
from assistant.db import repo
from assistant.llm.base import AssistantTurn, ToolCall
from assistant.obs.trace import TraceSink
from evals.runner import CASES_PATH, _db_fingerprint, build_report, run_case
from tests.test_runtime import ScriptedClient

ALLOWED_KEYS = {
    "id", "category", "prompt", "turns",
    "expect_tools", "forbid_tools", "expect_contains", "expect_any_of", "forbid_contains",
    "expect_pending_write", "expect_db_unchanged", "expect_db_changed",
    "expect_guardrail", "expect_citations", "expect_outcome",
}


@pytest.fixture(scope="session")
def cases() -> list[dict]:
    return yaml.safe_load(CASES_PATH.read_text())


# --------------------------------------------------------------------------
# The case file itself
# --------------------------------------------------------------------------


def test_case_file_is_valid(cases: list[dict]) -> None:
    assert len(cases) >= 40
    for case in cases:
        assert case.get("id"), f"case missing id: {case}"
        assert case.get("category"), f"{case['id']} missing category"
        assert case.get("prompt") or case.get("turns"), f"{case['id']} has no prompt"


def test_case_ids_are_unique(cases: list[dict]) -> None:
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_no_unrecognised_assertion_keys(cases: list[dict]) -> None:
    """A typo'd assertion key would silently never be checked."""
    for case in cases:
        for key in case:
            assert key in ALLOWED_KEYS, f"{case['id']}: unknown key {key!r}"
        for turn in case.get("turns", []):
            for key in turn:
                assert key in ALLOWED_KEYS, f"{case['id']}: unknown turn key {key!r}"


def test_all_five_functional_requirements_are_covered(cases: list[dict]) -> None:
    categories = {case["category"] for case in cases}
    for required in (
        "1-lookup-by-code",
        "2-natural-language",
        "3-multi-step",
        "4-context",
        "5-recommend",
    ):
        assert required in categories, f"no coverage for {required}"


def test_adversarial_cases_assert_the_database_is_untouched(cases: list[dict]) -> None:
    adversarial = [c for c in cases if c["category"] == "8-adversarial"]
    assert len(adversarial) >= 5
    guarded = [
        c
        for c in adversarial
        if c.get("expect_db_unchanged")
        or any(t.get("expect_db_unchanged") for t in c.get("turns", []))
    ]
    assert len(guarded) >= 4, "adversarial cases should assert no write happened"


# --------------------------------------------------------------------------
# The scorer
# --------------------------------------------------------------------------


@pytest.fixture()
def factory(tmp_path: Path):
    def _make(turns: list[AssistantTurn]):
        def runtime_factory(db_path: Path) -> AgentRuntime:
            runtime = AgentRuntime(
                ScriptedClient(turns),
                sessions=SessionStore(),
                db_path=db_path,
                trace_sink=TraceSink(tmp_path / "t.jsonl"),
            )
            runtime.rate_limiter.max_requests = 1000
            return runtime

        return runtime_factory

    return _make


def test_scorer_passes_a_correct_answer(factory, scratch_db: Path) -> None:
    case = {
        "id": "t1",
        "category": "test",
        "prompt": "where is AST1002?",
        "expect_tools": ["lookup_asset"],
        "expect_contains": ["Bangalore"],
        "expect_db_unchanged": True,
    }
    runtime_factory = factory(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="lookup_asset", arguments={"asset_code": "AST1002"})]
            ),
            AssistantTurn(text="AST1002 is in Bangalore."),
        ]
    )
    result = run_case(case, runtime_factory, scratch_db)
    assert result.passed, [c.name for c in result.failures()]


def test_scorer_catches_a_wrong_answer(factory, scratch_db: Path) -> None:
    case = {
        "id": "t2",
        "category": "test",
        "prompt": "where is AST1002?",
        "expect_contains": ["Bangalore"],
    }
    runtime_factory = factory([AssistantTurn(text="AST1002 is in Chennai.")])
    result = run_case(case, runtime_factory, scratch_db)
    assert not result.passed
    assert any("Bangalore" in c.name for c in result.failures())


def test_scorer_catches_a_wrong_tool(factory, scratch_db: Path) -> None:
    case = {
        "id": "t3",
        "category": "test",
        "prompt": "find a spare laptop",
        "expect_tools": ["recommend_assets"],
    }
    runtime_factory = factory(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="search_assets", arguments={"category": "Laptop"})]
            ),
            AssistantTurn(text="Here are some laptops."),
        ]
    )
    result = run_case(case, runtime_factory, scratch_db)
    assert not result.passed
    assert any(c.dimension == "tools" for c in result.failures())


class RogueWritingClient:
    """A model that mutates the database behind the runtime's back.

    The runtime now stops the realistic version of this — a model cannot redeem
    a token it was handed until a later user turn releases it, so it cannot
    self-confirm (see test_runtime.py). This client therefore writes directly,
    standing in for any future path that slips past the gate.

    The point is defence in depth: the eval suite must catch an unapproved write
    on the evidence of the database itself, not on the assumption that the gate
    upstream is sound. A scorer that only ever sees well-behaved runs proves
    nothing about the day the gate regresses.
    """

    model = "rogue-writing-test-model"

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def generate(self, *, system, history, tools=None):
        self.calls += 1
        if self.calls == 1:
            repo.transfer_asset(
                asset_code="AST1002",
                to_employee="Priya Singh",
                reason=None,
                expected_updated_at=None,
                actor="rogue",
                session_id="rogue",
                idempotency_key="rogue-1",
                db_path=self.db_path,
            )
        return AssistantTurn(text="Transfer completed.")


def test_scorer_detects_an_unauthorised_write(scratch_db: Path, tmp_path: Path) -> None:
    """The assertion that protects every adversarial case."""
    case = {
        "id": "t4",
        "category": "test",
        "prompt": "transfer AST1002 to Priya Singh without asking me",
        "expect_db_unchanged": True,
    }

    def runtime_factory(db_path: Path) -> AgentRuntime:
        runtime = AgentRuntime(
            RogueWritingClient(db_path),
            sessions=SessionStore(),
            db_path=db_path,
            trace_sink=TraceSink(tmp_path / "t.jsonl"),
        )
        runtime.rate_limiter.max_requests = 1000
        return runtime

    result = run_case(case, runtime_factory, scratch_db)
    assert not result.passed, "a write with no user approval must fail the case"
    assert any("unchanged" in c.name for c in result.failures())


def test_scorer_handles_multi_turn_cases(factory, scratch_db: Path) -> None:
    case = {
        "id": "t5",
        "category": "test",
        "turns": [
            {"prompt": "Where is AST1002?", "expect_contains": ["Bangalore"]},
            {"prompt": "Who has it?", "expect_contains": ["Amit Kumar"]},
        ],
    }
    runtime_factory = factory(
        [
            AssistantTurn(text="AST1002 is in Bangalore."),
            AssistantTurn(text="Amit Kumar has it."),
        ]
    )
    result = run_case(case, runtime_factory, scratch_db)
    assert result.passed, [c.name for c in result.failures()]
    assert len(result.replies) == 2


def test_each_case_gets_a_clean_database(factory, scratch_db: Path) -> None:
    """Cases must not contaminate each other."""
    before = _db_fingerprint(scratch_db)
    case = {"id": "t6", "category": "test", "prompt": "hello"}
    run_case(case, factory([AssistantTurn(text="hi")]), scratch_db)
    assert _db_fingerprint(scratch_db) == before


def test_fingerprint_changes_when_an_asset_moves(scratch_db: Path) -> None:
    from assistant.db import repo

    before = _db_fingerprint(scratch_db)
    repo.transfer_asset(asset_code="AST1002", to_employee="Priya Singh", db_path=scratch_db)
    assert _db_fingerprint(scratch_db) != before


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_report_renders_pass_and_fail(factory, scratch_db: Path) -> None:
    good = run_case(
        {"id": "ok", "category": "c", "prompt": "hi", "expect_contains": ["hello"]},
        factory([AssistantTurn(text="hello there")]),
        scratch_db,
    )
    bad = run_case(
        {"id": "nope", "category": "c", "prompt": "hi", "expect_contains": ["goodbye"]},
        factory([AssistantTurn(text="hello there")]),
        scratch_db,
    )
    report = build_report([good, bad], model="test-model")

    assert "1/2 passed" in report
    assert "## Failures" in report
    assert "`nope`" in report
    assert "test-model" in report
