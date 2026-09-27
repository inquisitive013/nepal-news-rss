import collections
import copy
import dataclasses
import json
from datetime import datetime, timezone

from newsroom import pipeline, publish
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter
from tests.conftest import FIXTURES

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


def _settings(tmp_path, **pipeline_overrides):
    s = load_settings(mock=True)
    raw = copy.deepcopy(s.raw)
    raw.setdefault("pipeline", {}).update(pipeline_overrides)
    return dataclasses.replace(s, raw=raw, root=tmp_path, data_dir=tmp_path / "data", fixtures_dir=FIXTURES / "feeds")


def _calls(llm):
    return collections.Counter(llm.calls)


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
    # the mock red team finds one high severity problem: judge 1 orders a revision and
    # judge 2 reads the revised piece as the final check. One red team round, two versions.
    assert len(art.review.validation_rounds) == 1
    rnd = art.review.validation_rounds[0]
    assert [r["after"] for r in rnd.revisions] == ["judge_1"]
    assert rnd.judge_1["decision"] == "revise" and rnd.judge_2["decision"] == "approve"
    assert not rnd.judge_2_recheck
    assert art.version == 2
    assert art.review.final_decision == "approved"
    assert art.review.final_reason.startswith("Judge 2:")
    # the investigation rides with the article: only angles with evidence survive
    assert [a["kind"] for a in art.investigation["angles"]] == ["record"]
    assert art.investigation["unanswered"] and art.investigation["summary"]
    assert "## What the coverage missed" in art.body_markdown
    assert "## What we still do not know" in art.body_markdown
    assert art.review.ranking  # judge reasons carried onto the article
    # the card headline, the chip and the engine caption ride with the article through the reviser
    assert art.image_headline and art.theme == "DISASTER" and art.country == "NEPAL"
    assert art.caption["hook"] and art.caption["body"] and "trigger" in art.caption
    # the Nepali edition rides with the approved article, read by the Nepali editor
    assert art.nepali["headline"].startswith("बागमती") and art.nepali["approved"] and art.nepali["caption"]["body"]
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


def test_credit_exhaustion_stops_the_run(tmp_path):
    from newsroom.llm import CreditExhausted

    class BrokeLLM(MockLLM):
        def structured(self, role, *args, **kwargs):
            if role == "writer":
                raise CreditExhausted("the Anthropic account has run out of credit")
            return super().structured(role, *args, **kwargs)

    settings = _settings(tmp_path)
    llm = BrokeLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "partial"
    assert not run.published
    assert any("credit" in e for e in run.errors)
    # no red team or judge calls were attempted after the money ran out
    assert "red_team" not in llm.calls


def test_call_budget_matches_the_settings(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    n_articles = len(run.selected_story_ids)
    assert 0 < n_articles <= int(settings.get("pipeline.articles_per_day"))
    calls = _calls(llm)
    assert calls["story_clusterer"] == 1
    assert calls["advocate"] == calls["skeptic"] == len(run.stories) <= int(settings.get("pipeline.max_debate_stories"))
    assert calls["ranking_judge"] == 2
    assert calls["writer"] == n_articles
    # every article is investigated once before it is written
    assert calls["investigator"] == n_articles
    # one red team pass and one defence per article, never a second round
    assert calls["red_team"] == calls["defense"] == n_articles
    # judge 1 once and judge 2 once per article; the mock never sends anything back
    assert calls["validation_judge"] == 2 * n_articles
    assert calls["reviser"] == n_articles <= int(settings.get("pipeline.max_revisions_per_run"))
    # every approved article is written in Nepali once and read by the editor once
    assert calls["nepali_writer"] == calls["nepali_editor"] == n_articles
    assert sum(calls.values()) <= int(settings.get("pipeline.max_llm_calls"))


def test_the_nepali_edition_can_be_switched_off(tmp_path):
    settings = _settings(tmp_path, nepali_edition=False)
    llm = MockLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok" and run.published
    assert "nepali_writer" not in llm.calls and "nepali_editor" not in llm.calls
    assert all(a.nepali == {} for a in publish.load_articles(settings))


def test_a_nepali_send_back_is_fixed_and_read_again_within_the_run(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(200), send_back_nepali=True)
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok" and run.published
    calls = _calls(llm)
    assert calls["nepali_writer"] == 2 * len(run.published) and calls["nepali_editor"] == 2 * len(run.published)
    for a in publish.load_articles(settings):
        assert a.nepali["problems_fixed"] == 1 and a.nepali["passes"] == 2 and a.nepali["approved"] and "सच्याइएको" in a.nepali["body_markdown"]


def test_judge_2_send_back_gets_one_more_revision_and_a_recheck(tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.run(settings, now=NOW)
    target = first.selected_story_ids[0]
    settings2 = dataclasses.replace(settings, root=tmp_path / "second", data_dir=tmp_path / "second" / "data")
    llm = MockLLM(settings2, UsageMeter(200), send_back_story_ids={target})
    run = pipeline.run(settings2, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    art = next(a for a in publish.load_articles(settings2) if a.story_id == target)
    rnd = art.review.validation_rounds[0]
    assert [r["after"] for r in rnd.revisions] == ["judge_1", "judge_2"]
    assert rnd.judge_2["decision"] == "revise"
    assert rnd.judge_2_recheck["decision"] == "approve"
    assert art.version == 3
    assert art.review.final_decision == "approved"
    # one extra reviser call and one extra judge call for the story that was sent back
    n = len(run.selected_story_ids)
    assert _calls(llm)["reviser"] == n + 1
    assert _calls(llm)["validation_judge"] == 2 * n + 1
    assert _calls(llm)["red_team"] == n


def test_revision_pool_caps_the_reviser_for_the_whole_run(tmp_path):
    settings = _settings(tmp_path, max_revisions_per_run=0)
    llm = MockLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    # with no revision budget judge 1 sees revisions_left 0 and rejects the flawed drafts
    assert not run.published
    assert run.rejected and all(r["stage"] == "validation" for r in run.rejected if "article_id" in r)
    assert "reviser" not in llm.calls


def test_two_red_team_rounds_when_configured(tmp_path):
    settings = _settings(tmp_path, red_team_rounds=2)
    llm = MockLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    art = publish.load_articles(settings)[0]
    # the revision gets a fresh red team round; judge 1 approves it and judge 2 confirms
    assert len(art.review.validation_rounds) == 2
    assert [r["after"] for r in art.review.validation_rounds[0].revisions] == ["judge_1"]
    assert art.review.validation_rounds[1].judge_1["decision"] == "approve"
    assert art.review.validation_rounds[1].judge_2["decision"] == "approve"
    n = len(run.selected_story_ids)
    assert _calls(llm)["red_team"] == _calls(llm)["defense"] == 2 * n


def test_second_run_on_the_same_day_keeps_both_records(tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.run(settings, now=NOW)
    first_file = tmp_path / "data" / "runs" / "2026-09-26.json"
    before = first_file.read_text()
    second = pipeline.run(settings, now=NOW)
    assert second.run_date == first.run_date
    assert first_file.read_text() == before
    second_file = tmp_path / "data" / "runs" / "2026-09-26-2.json"
    assert second_file.exists()
    assert [p.name for p in publish.run_records(settings)] == ["2026-09-26.json", "2026-09-26-2.json"]


def test_investigation_can_be_switched_off(tmp_path):
    settings = _settings(tmp_path, investigate=False)
    llm = MockLLM(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    assert "investigator" not in llm.calls
    art = publish.load_articles(settings)[0]
    assert art.investigation == {}
    assert "What the coverage missed" not in art.body_markdown


def test_failed_investigation_never_loses_the_article(tmp_path):
    from newsroom.llm import LLMError

    class NoDigging(MockLLM):
        def structured(self, role, *args, **kwargs):
            if role == "investigator":
                self.meter.reserve()
                self.calls.append(role)
                raise LLMError("search tool unavailable")
            return super().structured(role, *args, **kwargs)

    settings = _settings(tmp_path)
    llm = NoDigging(settings, UsageMeter(200))
    run = pipeline.run(settings, llm=llm, now=NOW)
    assert run.status == "ok", run.errors
    assert run.published
    art = publish.load_articles(settings)[0]
    assert art.investigation["angles"] == []
    assert "investigation failed" in art.investigation["summary"]
    assert "What the coverage missed" not in art.body_markdown


def test_items_already_cited_are_dropped_and_the_desk_sees_its_recent_stories(tmp_path):
    from newsroom.llm import UsageMeter, make_llm

    settings = _settings(tmp_path)
    payloads: dict[str, list[dict]] = {}

    def spy(llm):
        orig = llm.structured

        def structured(role, task, payload, schema, **kw):
            payloads.setdefault(role, []).append(payload)
            return orig(role, task, payload, schema, **kw)

        llm.structured = structured
        return llm

    first = pipeline.run(settings, llm=spy(make_llm(settings, UsageMeter(90))), now=NOW)
    assert first.published
    assert payloads["story_clusterer"][0]["recently_published"] == []
    assert payloads["ranking_judge"][0]["recently_published"] == []
    cited = pipeline.published_source_urls(settings, now=NOW)
    assert cited and any(pipeline._url_key(c.url) in cited for c in first.candidates)

    payloads.clear()
    second = pipeline.run(settings, llm=spy(make_llm(settings, UsageMeter(90))), now=NOW)
    assert len(second.candidates) < len(first.candidates)
    assert not any(pipeline._url_key(c.url) in cited for c in second.candidates)
    recent = payloads["story_clusterer"][0]["recently_published"]
    assert {r["id"] for r in recent} >= set(first.published)
    assert payloads["ranking_judge"][0]["recently_published"] == recent
    # the take rides on every article and survives the reviser
    art = publish.load_articles(settings)[0]
    assert art.take.startswith("One number is confirmed") and art.version == 2
