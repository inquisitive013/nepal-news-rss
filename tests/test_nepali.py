import copy
import dataclasses

from newsroom import nepali, publish
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter
from newsroom.models import Article


def _settings(tmp_path, **pipeline_overrides):
    s = load_settings(mock=True)
    raw = copy.deepcopy(s.raw)
    raw.setdefault("pipeline", {}).update(pipeline_overrides)
    return dataclasses.replace(s, raw=raw, root=tmp_path, data_dir=tmp_path / "data")


def _article(**overrides):
    base = dict(
        id="2026-09-26-bagmati",
        slug="bagmati",
        story_id="s_1",
        headline="Bagmati floods move 140 households overnight",
        dek="Police say the river rose faster than the warning system.",
        body_markdown="Police moved 140 households.\n\n## Why it matters\n\nRivers rose fast.",
        language="en",
        take="One number is certain: 140 households slept in schools.",
        social_hook="140 households moved in one night.",
        caption={"hook": "h", "body": "b", "trigger": "t"},
        key_facts=[{"fact": "140 households moved", "source_url": "https://kathmandupost.com/x"}],
        sources=[{"name": "Kathmandu Post", "url": "https://kathmandupost.com/x", "used_for": "toll"}],
        investigation={"angles": [{"kind": "record", "claim": "c", "evidence": [{"source": "s", "url": "https://example.org/e", "fact": "f"}]}], "unanswered": [{"question": "q", "who_could_answer": "w"}]},
        run_date="2026-09-26",
        published_at="2026-09-26T06:30:00+00:00",
    )
    base.update(overrides)
    return Article(**base)


def test_the_record_carries_everything_the_writer_needs(tmp_path):
    settings = _settings(tmp_path)
    record = nepali._record(settings, _article())
    assert record["key_facts"] and record["sources"][0]["used_for"] == "toll"
    assert record["investigation"]["angles"][0]["evidence"][0]["url"] == "https://example.org/e" and record["investigation"]["unanswered"]
    # 2026-09-26 06:30 UTC is a Saturday afternoon in Kathmandu
    assert record["weekday_ne"] == "शनिबार" and record["date_ne"] == "२६ सेप्टेम्बर २०२६"
    assert record["site_name_ne"] == "नेपाल वायर"
    assert nepali.date_words(settings, "2026-09-27") == ("आइतबार", "२७ सेप्टेम्बर २०२६")
    assert nepali.date_words(settings, "") == ("", "")


def test_the_editor_approves_a_clean_piece_first_time(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(10))
    ne = nepali.nepali_for(llm, settings, _article())
    assert llm.calls == ["nepali_writer", "nepali_editor"]
    assert ne["headline"].startswith("बागमती") and "## किन" in ne["body_markdown"] and ne["body_markdown"].startswith("काठमाडौं ।")
    assert ne["dek"] and ne["take"] and ne["image_headline"] and ne["social_hook"]
    assert ne["caption"]["hook"] and ne["caption"]["body"] and ne["caption"]["trigger"]
    assert ne["checked"] is True and ne["approved"] is True and ne["passes"] == 1 and ne["problems_fixed"] == 0 and ne["editor"]
    assert nepali.usable(ne)


def test_a_send_back_is_fixed_and_read_again(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(10), send_back_nepali=True)
    ne = nepali.nepali_for(llm, settings, _article())
    assert llm.calls == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"]
    assert "सच्याइएको" in ne["body_markdown"]
    assert ne["problems_fixed"] == 1 and ne["passes"] == 2 and ne["approved"] is True and ne["checked"] is True


def test_one_round_means_one_reading_and_the_fix_still_lands(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=1)
    llm = MockLLM(settings, UsageMeter(10), send_back_nepali=True)
    ne = nepali.nepali_for(llm, settings, _article())
    assert llm.calls == ["nepali_writer", "nepali_editor", "nepali_writer"]
    assert "सच्याइएको" in ne["body_markdown"] and ne["passes"] == 1 and ne["approved"] is False and ne["checked"] is True


def test_usable_needs_a_headline_and_a_body():
    assert not nepali.usable(None) and not nepali.usable({}) and not nepali.usable({"headline": "x"})
    assert nepali.usable({"headline": "x", "body_markdown": "y"})


def test_backfill_writes_only_the_stories_without_a_nepali_version(tmp_path):
    settings = _settings(tmp_path)
    missing = _article()
    done = _article(id="2026-09-25-petrol", slug="petrol", headline="Petrol drops Rs 5", run_date="2026-09-25", published_at="2026-09-25T06:30:00+00:00", nepali={"headline": "पेट्रोल घट्यो", "body_markdown": "घट्यो"})
    publish.save_article(settings, missing)
    publish.save_article(settings, done)
    assert [a.id for a in nepali.wanting(settings)] == [missing.id]
    assert [a.id for a in nepali.wanting(settings, everything=True)] == [missing.id, done.id]
    assert [a.id for a in nepali.wanting(settings, everything=True, limit=1)] == [missing.id]
    assert [a.id for a in nepali.wanting(settings, only=[done.id])] == [done.id]

    results = nepali.backfill(settings, MockLLM(settings, UsageMeter(10)), nepali.wanting(settings))
    assert len(results) == 1 and results[0][:2] == (missing.id, "written") and results[0][2].startswith("बागमती")
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert nepali.usable(stored[missing.id].nepali) and stored[missing.id].nepali["approved"]
    assert stored[done.id].nepali["headline"] == "पेट्रोल घट्यो"  # untouched
    assert nepali.wanting(settings) == []


def test_backfill_stops_when_the_budget_is_spent_and_keeps_what_it_has(tmp_path):
    settings = _settings(tmp_path)
    first = _article()
    second = _article(id="2026-09-25-petrol", slug="petrol", headline="Petrol drops Rs 5", run_date="2026-09-25", published_at="2026-09-25T06:30:00+00:00")
    publish.save_article(settings, first)
    publish.save_article(settings, second)
    # Two calls write and check the first story; the third call, for the second story, is refused.
    results = nepali.backfill(settings, MockLLM(settings, UsageMeter(2)), nepali.wanting(settings))
    assert [r[:2] for r in results] == [(first.id, "written")]
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert nepali.usable(stored[first.id].nepali) and not stored[second.id].nepali
