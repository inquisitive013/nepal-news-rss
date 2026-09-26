import dataclasses
import json
from datetime import datetime, timezone

from newsroom import pipeline, publish
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter
from tests.conftest import FIXTURES

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


def _settings(tmp_path):
    s = load_settings(mock=True)
    return dataclasses.replace(s, root=tmp_path, data_dir=tmp_path / "data", fixtures_dir=FIXTURES / "feeds")


def test_mock_run_publishes_and_records(tmp_path):
    settings = _settings(tmp_path)
    run = pipeline.run(settings, now=NOW)
    assert run.status == "ok", run.errors
    assert run.run_date == "2026-09-26"
    assert run.candidates and run.stories and run.debates
    assert [v.judge for v in run.ranking] == ["ranking_judge_1", "ranking_judge_2"]
    assert run.selected_story_ids
    assert run.published, run.rejected
    articles = publish.load_articles(settings)
    assert {a.id for a in articles} == set(run.published)
    art = articles[0]
    assert art.published_at and art.image is not None and (tmp_path / art.image.path).exists()
    # the mock red team forces one revision round, so every article went through 2 rounds
    assert len(art.review.validation_rounds) == 2
    assert art.version == 2
    assert art.review.final_decision == "approved"
    assert art.review.ranking  # judge reasons carried onto the article
    run_file = tmp_path / "data" / "runs" / "2026-09-26.json"
    assert run_file.exists()
    data = json.loads(run_file.read_text())
    assert data["usage_totals"]["calls"] == len(run.usage) > 10
    assert data["feed_health"]


def test_mock_run_rejection_path(tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.run(settings, now=NOW)
    target = first.selected_story_ids[0]
    # fresh data dir, reject the top story at judge 2 stages
    settings2 = dataclasses.replace(settings, root=tmp_path / "second", data_dir=tmp_path / "second" / "data")
    llm = MockLLM(settings2, UsageMeter(200), reject_story_ids={target})
    run = pipeline.run(settings2, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    assert target not in run.selected_story_ids  # ranking judge 2 removed it
    rejected_ids = {r["story_id"] for r in run.rejected}
    assert target in rejected_ids
    assert (tmp_path / "second" / "data" / "runs" / "2026-09-26.json").exists()


def test_budget_exhaustion_is_partial_not_crash(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(3))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "partial"
    assert run.errors and not run.published
