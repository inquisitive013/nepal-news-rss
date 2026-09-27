import dataclasses

from newsroom import publish, translation
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter
from newsroom.models import Article


def _settings(tmp_path):
    return dataclasses.replace(load_settings(mock=True), root=tmp_path, data_dir=tmp_path / "data")


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
        sources=[{"name": "Kathmandu Post", "url": "https://kathmandupost.com/x", "used_for": "toll"}],
        run_date="2026-09-26",
        published_at="2026-09-26T06:30:00+00:00",
    )
    base.update(overrides)
    return Article(**base)


def test_translation_is_checked_and_approved(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(10))
    ne = translation.nepali_for(llm, settings, _article())
    assert llm.calls == ["translator", "translation_judge"]
    assert ne["headline"].startswith("बागमती") and "## किन" in ne["body_markdown"]
    assert ne["dek"] and ne["take"] and ne["image_headline"] and ne["social_hook"]
    assert ne["caption"]["hook"] and ne["caption"]["body"] and ne["caption"]["trigger"]
    assert ne["checked"] is True and ne["problems_fixed"] == 0 and ne["judge"]
    assert translation.usable(ne)


def test_translation_sent_back_once_gets_fixed(tmp_path):
    settings = _settings(tmp_path)
    llm = MockLLM(settings, UsageMeter(10), send_back_translation=True)
    ne = translation.nepali_for(llm, settings, _article())
    assert llm.calls == ["translator", "translation_judge", "translator"]
    assert "सच्याइएको" in ne["body_markdown"]
    assert ne["problems_fixed"] == 1 and ne["checked"] is True


def test_usable_needs_a_headline_and_a_body():
    assert not translation.usable(None) and not translation.usable({}) and not translation.usable({"headline": "x"})
    assert translation.usable({"headline": "x", "body_markdown": "y"})


def test_backfill_translates_only_the_stories_without_a_nepali_version(tmp_path):
    settings = _settings(tmp_path)
    missing = _article()
    done = _article(id="2026-09-25-petrol", slug="petrol", headline="Petrol drops Rs 5", run_date="2026-09-25", published_at="2026-09-25T06:30:00+00:00", nepali={"headline": "पेट्रोल घट्यो", "body_markdown": "घट्यो"})
    publish.save_article(settings, missing)
    publish.save_article(settings, done)
    assert [a.id for a in translation.wanting(settings)] == [missing.id]
    assert [a.id for a in translation.wanting(settings, everything=True)] == [missing.id, done.id]
    assert [a.id for a in translation.wanting(settings, everything=True, limit=1)] == [missing.id]
    assert [a.id for a in translation.wanting(settings, only=[done.id])] == [done.id]

    results = translation.backfill(settings, MockLLM(settings, UsageMeter(10)), translation.wanting(settings))
    assert len(results) == 1 and results[0][:2] == (missing.id, "translated") and results[0][2].startswith("बागमती")
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert translation.usable(stored[missing.id].nepali) and stored[missing.id].nepali["checked"]
    assert stored[done.id].nepali["headline"] == "पेट्रोल घट्यो"  # untouched
    assert translation.wanting(settings) == []


def test_backfill_stops_when_the_budget_is_spent_and_keeps_what_it_has(tmp_path):
    settings = _settings(tmp_path)
    first = _article()
    second = _article(id="2026-09-25-petrol", slug="petrol", headline="Petrol drops Rs 5", run_date="2026-09-25", published_at="2026-09-25T06:30:00+00:00")
    publish.save_article(settings, first)
    publish.save_article(settings, second)
    # Two calls translate and check the first story; the third call, for the second story, is refused.
    results = translation.backfill(settings, MockLLM(settings, UsageMeter(2)), translation.wanting(settings))
    assert [r[:2] for r in results] == [(first.id, "translated")]
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert translation.usable(stored[first.id].nepali) and not stored[second.id].nepali
