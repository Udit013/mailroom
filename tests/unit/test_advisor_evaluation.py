"""The advisor report counts what the model would change, and the cache makes reruns free."""

import json

from mailroom import advisor_evaluation
from mailroom.advisor import Suggestion
from mailroom.advisor_evaluation import CachedAsk, Judged, build_report, judge
from mailroom.evaluation import DEFAULT_MATTERS, DEFAULT_SEED
from mailroom.matcher import Thresholds
from mailroom.models import DocumentType


def test_cache_answers_a_rerun_without_calling_and_retries_failures(tmp_path):
    calls: list[str] = []

    def ask(text: str) -> Suggestion:
        calls.append(text)
        if text == "fails":
            return Suggestion("m", None, "timeout")
        return Suggestion("m", DocumentType.SUMMONS)

    path = tmp_path / "answers.json"
    first = CachedAsk(ask, path, pause_seconds=0)
    first("a", "m")
    first("fails", "m")

    second = CachedAsk(ask, path, pause_seconds=0)
    assert second("a", "m") == Suggestion("m", DocumentType.SUMMONS)
    second("fails", "m")

    assert calls == ["a", "fails", "fails"]
    assert list(json.loads(path.read_text()).values()) == ["SUMMONS"]


def test_cache_is_per_model(tmp_path):
    calls: list[str] = []

    def ask(text: str) -> Suggestion:
        calls.append(text)
        return Suggestion("m", DocumentType.SUMMONS)

    cached = CachedAsk(ask, tmp_path / "answers.json", pause_seconds=0)
    cached("a", "model-one")
    cached("a", "model-two")

    assert len(calls) == 2


def test_judging_stops_once_the_daily_limit_opens_the_breaker():
    outcomes = synthetic_outcomes()[:5]
    answers = iter(
        [Suggestion("m", DocumentType.OTHER), Suggestion("m", None, "circuit_open")]
        + [Suggestion("m", DocumentType.OTHER)] * 3
    )

    judged = judge(outcomes, lambda _: next(answers))

    assert len(judged) == 2


def synthetic_outcomes():
    import random

    from mailroom.evaluation import evaluate_one, generate_documents, generate_matters

    rng = random.Random(DEFAULT_SEED)
    matters = generate_matters(rng, DEFAULT_MATTERS)
    documents = generate_documents(rng, matters, 300)
    return [evaluate_one(document, matters, Thresholds()) for document in documents]


def test_report_counts_wrong_filings_caught_and_correct_filings_stopped():
    outcomes = synthetic_outcomes()
    automatic = [outcome for outcome in outcomes if outcome.state.value == "READY_TO_FILE"]
    wrong = [o for o in automatic if o.predicted_type != o.document.true_type]
    right = [o for o in automatic if o.predicted_type == o.document.true_type]

    # A model that always knows the true type, except that it doubts one correct filing.
    doubted = right[0]
    judged = [
        Judged(
            outcome,
            Suggestion(
                "m",
                DocumentType.OTHER
                if outcome is doubted and doubted.predicted_type is not DocumentType.OTHER
                else outcome.document.true_type,
            ),
        )
        for outcome in outcomes
    ]

    report = build_report("m", len(outcomes), judged)

    assert f"| Wrong-type filings the model stopped | {len(wrong)} of {len(wrong)} |" in report
    assert "| Correct filings the model sent to review | 1 |" in report
    assert f"| Automatic filings with the model | {len(automatic) - len(wrong) - 1} |" in report
    assert "Answered: 300 of 300." in report


def test_report_lists_why_documents_went_unanswered():
    outcomes = synthetic_outcomes()[:3]
    judged = [
        Judged(outcomes[0], Suggestion("m", None, "rate_limited")),
        Judged(outcomes[1], Suggestion("m", None, "timeout")),
        Judged(outcomes[2], Suggestion("m", outcomes[2].document.true_type)),
    ]

    report = build_report("m", 300, judged)

    assert "Answered: 1 of 300. Not answered: rate_limited 1, timeout 1." in report


def test_module_never_sends_anything_without_a_configured_advisor(monkeypatch):
    monkeypatch.setattr(advisor_evaluation.advisor, "from_settings", lambda: None)

    try:
        advisor_evaluation.main()
    except SystemExit as exc:
        assert "ADVISOR=gemini" in str(exc)
    else:
        raise AssertionError("main() ran without an advisor")
