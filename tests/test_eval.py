from types import SimpleNamespace

import pytest

from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
from ai_monitor.config.settings import InterestArea
from ai_monitor.eval import golden_set, judge, metrics, run_eval
from ai_monitor.eval.judge import JudgeVerdict
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

INTERESTS = {"agents": InterestArea(description="Agentic systems", keywords=["agent"])}


class FakeMessages:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(
            parsed_output=self.verdict,
            usage=SimpleNamespace(input_tokens=600, output_tokens=80),
        )


class FakeClient:
    def __init__(self, score=0.7, reasoning="Reasonable work on agents."):
        self.messages = FakeMessages(
            JudgeVerdict(relevance_score=score, reasoning=reasoning)
        )


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def add_item(conn, source_id, title, analyzer_score):
    item_id = db.upsert_item(
        conn,
        Item(
            source=Source.ARXIV,
            source_id=source_id,
            title=title,
            url=f"https://arxiv.org/abs/{source_id}",
            content="An abstract about agents.",
        ),
    )
    store_analysis(
        conn,
        item_id,
        AnalysisResult(
            summary=f"Summary of {title}",
            relevance_score=analyzer_score,
            matched_areas=["agents"],
            justification="Because.",
        ),
        "hash",
        "claude-haiku-4-5",
    )
    return item_id


# --- metrics -------------------------------------------------------------


def test_perfect_agreement():
    a = metrics.compare([0.2, 0.5, 0.9], [0.2, 0.5, 0.9])
    assert a.mae == 0.0
    assert a.mean_bias == 0.0
    assert a.binary_agreement == 1.0
    assert a.pearson_r == pytest.approx(1.0)


def test_mae_and_bias_detect_systematic_overscoring():
    a = metrics.compare([0.7, 0.8, 0.9], [0.5, 0.6, 0.7])
    assert a.mae == pytest.approx(0.2)
    assert a.mean_bias == pytest.approx(0.2)  # positive: too generous


def test_pearson_none_without_variance():
    assert metrics.compare([0.5, 0.5], [0.3, 0.7]).pearson_r is None


def test_precision_recall_on_threshold():
    # predicted relevant: items 0,1  | actually relevant: items 1,2
    a = metrics.compare([0.9, 0.8, 0.2, 0.1], [0.2, 0.8, 0.9, 0.1])
    assert a.precision == pytest.approx(0.5)  # 1 of 2 predicted are truly relevant
    assert a.recall == pytest.approx(0.5)  # caught 1 of 2 relevant items
    assert a.binary_agreement == pytest.approx(0.5)


def test_compare_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        metrics.compare([0.5], [0.5, 0.5])


def test_calibration_buckets_localize_bias():
    """Bias concentrated in one band is a rubric problem, not a prompt-wide one."""
    predicted = [0.1, 0.2, 0.7, 0.8]
    reference = [0.1, 0.2, 0.3, 0.4]  # only the high band is wrong
    buckets = {b["range"]: b for b in metrics.calibration_buckets(predicted, reference)}
    assert buckets["0.0-0.3"]["mean_bias"] == pytest.approx(0.0)
    assert buckets["0.6-0.9"]["mean_bias"] == pytest.approx(0.4)


# --- golden set ----------------------------------------------------------


def test_label_and_retrieve(conn):
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.6, "overrated")

    rows = golden_set.labeled_items(conn)
    assert len(rows) == 1
    assert rows[0]["human_score"] == 0.6
    assert rows[0]["analyzer_score"] == 0.8
    assert rows[0]["human_notes"] == "overrated"


def test_relabeling_updates_in_place(conn):
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.6)
    golden_set.record_label(conn, item_id, 0.9, "changed my mind")

    rows = golden_set.labeled_items(conn)
    assert len(rows) == 1
    assert rows[0]["human_score"] == 0.9


def test_unlabeled_excludes_labeled(conn):
    a = add_item(conn, "1", "First", 0.8)
    add_item(conn, "2", "Second", 0.5)
    golden_set.record_label(conn, a, 0.7)

    pending = golden_set.unlabeled_items(conn)
    assert [r["source_id"] for r in pending] == ["2"]


def test_unlabeled_hides_analyzer_score(conn):
    """Seeing the analyzer's score before labeling would anchor the label."""
    add_item(conn, "1", "First", 0.8)
    row = golden_set.unlabeled_items(conn)[0]
    assert "relevance_score" not in row.keys()
    assert "analyzer_score" not in row.keys()


def test_needs_judging_tracks_state(conn):
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.7)
    assert len(golden_set.needs_judging(conn)) == 1

    golden_set.record_judge(conn, item_id, 0.65, "close enough")
    assert golden_set.needs_judging(conn) == []
    assert golden_set.stats(conn)["judged"] == 1


# --- judge ---------------------------------------------------------------


def test_judge_prompt_excludes_analyzer_output(conn):
    """The judge must form an independent opinion, so it never sees the analysis."""
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.7)

    client = FakeClient()
    judge.judge_golden_set(conn, client=client, interests=INTERESTS)

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "Agent paper" in prompt
    assert "Summary of" not in prompt
    assert "0.8" not in prompt


def test_judge_scores_are_stored(conn):
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.7)

    judge.judge_golden_set(conn, client=FakeClient(score=0.65), interests=INTERESTS)

    row = golden_set.labeled_items(conn, require_judge=True)[0]
    assert row["judge_score"] == 0.65
    assert row["judge_reasoning"] == "Reasonable work on agents."


def test_judging_is_not_repeated(conn):
    item_id = add_item(conn, "1", "Agent paper", 0.8)
    golden_set.record_label(conn, item_id, 0.7)
    client = FakeClient()

    judge.judge_golden_set(conn, client=client, interests=INTERESTS)
    judge.judge_golden_set(conn, client=client, interests=INTERESTS)

    assert client.messages.calls == 1


# --- report --------------------------------------------------------------


def test_evaluate_reports_both_comparisons(conn):
    for i, (analyzer_score, human, judge_score) in enumerate(
        [(0.9, 0.6, 0.65), (0.4, 0.4, 0.45), (0.8, 0.5, 0.55)], start=1
    ):
        item_id = add_item(conn, str(i), f"Paper {i}", analyzer_score)
        golden_set.record_label(conn, item_id, human)
        golden_set.record_judge(conn, item_id, judge_score, "reasoning")

    result = run_eval.evaluate(conn)
    assert result["n_labeled"] == 3
    assert result["n_judged"] == 3
    # analyzer runs hot; judge tracks the human labels closely
    assert result["analyzer_vs_human"].mean_bias > 0.15
    assert result["judge_vs_human"].mae < 0.1


def test_evaluate_without_judge_scores(conn):
    item_id = add_item(conn, "1", "Paper", 0.9)
    golden_set.record_label(conn, item_id, 0.5)

    result = run_eval.evaluate(conn)
    assert result["judge_vs_human"] is None
    assert result["n_judged"] == 0


def test_worst_disagreements_ranked_by_magnitude(conn):
    item_a = add_item(conn, "1", "Small miss", 0.55)
    item_b = add_item(conn, "2", "Big miss", 0.95)
    golden_set.record_label(conn, item_a, 0.5)
    golden_set.record_label(conn, item_b, 0.2)

    worst = run_eval.evaluate(conn)["worst_disagreements"]
    assert worst[0]["title"] == "Big miss"
    assert worst[0]["delta"] == pytest.approx(0.75)


def test_empty_golden_set_raises(conn):
    with pytest.raises(ValueError, match="golden set is empty"):
        run_eval.evaluate(conn)


def test_report_renders_with_and_without_judge(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "REPORTS_DIR", tmp_path / "reports")
    item_id = add_item(conn, "1", "Paper", 0.9)
    golden_set.record_label(conn, item_id, 0.5, "too generous")

    _, path = run_eval.run(conn)
    text = path.read_text()
    assert "Analyzer vs human labels" in text
    assert "Judge vs human labels" not in text
    assert "too generous" in text

    golden_set.record_judge(conn, item_id, 0.55, "agree with human")
    _, path = run_eval.run(conn)
    assert "Judge vs human labels" in path.read_text()
