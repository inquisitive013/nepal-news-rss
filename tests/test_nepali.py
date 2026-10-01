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
    # The investigator's notes and the English caption stay out: the Nepali carries what the judged story carries.
    assert "investigation" not in record and "caption" not in record
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
    assert list(ne["caption"]) == ["hook", "angle", "trigger"] and all(ne["caption"].values()) and ne["caption"]["trigger"].endswith("?")
    assert ne["checked"] is True and ne["approved"] is True and ne["passes"] == 1 and ne["problems_fixed"] == 0 and ne["editor"]
    assert nepali.usable(ne)


def test_a_send_back_is_fixed_and_read_again(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=2)
    llm = MockLLM(settings, UsageMeter(10), send_back_nepali=True)
    ne = nepali.nepali_for(llm, settings, _article())
    assert llm.calls == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"]
    assert "सच्याइएको" in ne["body_markdown"]
    assert ne["problems_fixed"] == 1 and ne["passes"] == 2 and ne["approved"] is True and ne["checked"] is True
    # The draft opens the Nepali sources for wording and spelling; the fix pass carries no search tool.
    assert llm.searches == [("nepali_writer", settings.web_search_uses("nepali_writer")), ("nepali_editor", 0), ("nepali_writer", 0), ("nepali_editor", 0)]
    assert settings.web_search_uses("nepali_writer") > 0


def test_one_round_means_one_reading_one_fix_and_a_final_reading(tmp_path):
    """Until 30 September the last round's fixes went out unread; now the editor reads what publishes."""
    settings = _settings(tmp_path, nepali_rounds=1)
    llm = MockLLM(settings, UsageMeter(10), send_back_nepali=True)
    ne = nepali.nepali_for(llm, settings, _article())
    assert llm.calls == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"]
    assert "सच्याइएको" in ne["body_markdown"] and ne["passes"] == 2 and ne["approved"] is True and ne["held"] is False


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


def test_backfill_with_workers_writes_stories_side_by_side_in_input_order(tmp_path):
    settings = _settings(tmp_path)
    ids = []
    for n in range(3):
        a = _article(id=f"2026-09-2{6 - n}-story-{n}", slug=f"story-{n}", run_date=f"2026-09-2{6 - n}", published_at=f"2026-09-2{6 - n}T06:30:00+00:00")
        publish.save_article(settings, a)
        ids.append(a.id)
    wanted = nepali.wanting(settings)
    assert [a.id for a in wanted] == ids
    results = nepali.backfill(settings, MockLLM(settings, UsageMeter(20)), wanted, workers=3)
    assert [r[:2] for r in results] == [(i, "written") for i in ids]
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert all(nepali.usable(stored[i].nepali) and stored[i].nepali["approved"] for i in ids)
    # a budget that runs out mid way leaves the cut off stories untouched and unreported
    for a in wanted:
        a.nepali = {}
        publish.save_article(settings, a)
    partial = nepali.backfill(settings, MockLLM(settings, UsageMeter(3)), nepali.wanting(settings), workers=3)
    stored = {a.id: a for a in publish.load_articles(settings)}
    assert 0 <= len(partial) <= 1 and all(nepali.usable(stored[i].nepali) == (i in {r[0] for r in partial}) for i in ids)


PIECE = {
    "headline": "बागमती उर्लियो, १४० घरधुरी विस्थापित",
    "dek": "प्रहरीका अनुसार चेतावनी ढिलो आयो।",
    "take": "एउटा कुरा पक्का छ।",
    "body_markdown": "काठमाडौं । नदी बढ्यो।\n\nचेतावनी प्रणाली असफल भयो।",
    "image_headline": "१४० घरधुरी विस्थापित",
    "social_hook": "१४० घरधुरी एकै रातमा।",
    "caption": {"hook": "एकै रातमा घर छोड्नुपर्‍यो। सरकारले  चेतावनी\nलुकायो।", "angle": "नदी बढ्यो।", "trigger": ""},
    "notes": "",
}


def test_a_fix_goes_in_where_its_passage_stands_and_nothing_else_moves():
    problems = [
        {"passage": "सरकारले चेतावनी लुकायो।", "problem": "not in the record", "fix": "प्रहरीका अनुसार चेतावनी ढिलो आयो।"},  # spacing differs from the piece
        {"passage": "यो वाक्य कतै छैन।", "problem": "p", "fix": "f"},
        {"passage": "१४० घरधुरी", "problem": "p", "fix": "f"},  # in three places: the writer must place it
        {"passage": "कसको असफलता?", "problem": "p", "fix": ""},
    ]
    out, left = nepali.apply_fixes(PIECE, problems)
    assert out["caption"]["hook"] == "एकै रातमा घर छोड्नुपर्‍यो। प्रहरीका अनुसार चेतावनी ढिलो आयो।" and out["caption"]["angle"] == "नदी बढ्यो।"
    assert [p["passage"] for p in left] == ["यो वाक्य कतै छैन।", "१४० घरधुरी", "कसको असफलता?"]
    assert {k: v for k, v in out.items() if k != "caption"} == {k: v for k, v in PIECE.items() if k != "caption"}
    assert PIECE["caption"]["hook"] == "एकै रातमा घर छोड्नुपर्‍यो। सरकारले  चेतावनी\nलुकायो।"  # the original is not touched


def test_an_outlet_the_writer_misspells_reaches_no_reader(tmp_path):
    settings = _settings(tmp_path)
    llm = ScriptedLLM(
        {**PIECE, "body_markdown": "काठमाडौं । पानी बन्द भएको रातोपातीले जनाएको छ।", "caption": {"hook": "रातोपातीका अनुसार पानी बन्द छ।", "angle": "सेतोपातीले पनि लेखेको छ।", "trigger": ""}},
        {"decision": "approve", "problems": [], "reason": "ok"},
    )
    ne = nepali.nepali_for(llm, settings, _article())
    assert "रातोपाटीले जनाएको" in ne["body_markdown"] and ne["caption"]["hook"] == "रातोपाटीका अनुसार पानी बन्द छ।" and ne["caption"]["angle"] == "सेतोपाटीले पनि लेखेको छ।"
    # the editor never saw the slip either
    assert "रातोपाती" not in str(llm.calls[1][1]["nepali"])


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.texts = []

    def structured(self, role, user_text, payload, schema, **kw):
        self.calls.append((role, payload, kw.get("web_search_uses", 0)))
        self.texts.append(user_text)
        return copy.deepcopy(self.replies.pop(0))


def test_in_place_fixes_skip_the_writer_unless_a_passage_cannot_be_found(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=2, nepali_fix="in_place")
    placed = {"passage": "चेतावनी प्रणाली असफल भयो।", "problem": "stronger than the record", "fix": "प्रहरीका अनुसार चेतावनी ढिलो आयो।"}
    llm = ScriptedLLM(PIECE, {"decision": "revise", "problems": [placed], "reason": "r"}, {"decision": "approve", "problems": [], "reason": "ok"})
    ne = nepali.nepali_for(llm, settings, _article())
    assert [c[0] for c in llm.calls] == ["nepali_writer", "nepali_editor", "nepali_editor"]
    assert ne["body_markdown"].endswith("प्रहरीका अनुसार चेतावनी ढिलो आयो।") and ne["approved"] is True
    assert ne["problems_fixed"] == 1 and ne["fixed_in_place"] == 1
    assert llm.calls[2][1]["nepali"]["body_markdown"] == ne["body_markdown"]  # the second reading reads the fixed piece

    lost = {"passage": "यो वाक्य कतै छैन।", "problem": "p", "fix": "f"}
    fixed_by_writer = {**PIECE, "take": "लेखकले मिलाएको।"}
    llm = ScriptedLLM(PIECE, {"decision": "revise", "problems": [placed, lost], "reason": "r"}, fixed_by_writer, {"decision": "approve", "problems": [], "reason": "ok"})
    ne = nepali.nepali_for(llm, settings, _article())
    role, payload, searches = llm.calls[2]
    assert role == "nepali_writer" and payload["fixes"] == [lost] and searches == 0
    assert payload["nepali"]["body_markdown"].endswith("प्रहरीका अनुसार चेतावनी ढिलो आयो।")  # the placed fix rides along
    assert ne["problems_fixed"] == 2 and ne["fixed_in_place"] == 1 and ne["take"] == "लेखकले मिलाएको।"


def test_by_default_the_writer_applies_every_fix(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=1)
    assert settings.get("pipeline.nepali_fix", "rewrite") == "rewrite"
    placed = {"passage": "चेतावनी प्रणाली असफल भयो।", "problem": "p", "fix": "f"}
    llm = ScriptedLLM(PIECE, {"decision": "revise", "problems": [placed], "reason": "r"}, {**PIECE, "take": "लेखकले मिलाएको।"}, {"decision": "approve", "problems": [], "reason": "ok"})
    ne = nepali.nepali_for(llm, settings, _article())
    assert [c[0] for c in llm.calls] == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"] and ne["fixed_in_place"] == 0
    # The fix pass changes what the fixes name and keeps every other sentence, and every source, where it was.
    fix_text = llm.texts[2]
    assert "word for word" in fix_text and "source" in fix_text and "re-read the whole piece" not in fix_text


def test_the_trial_fixes_one_draft_both_ways_and_reads_each_result_again(tmp_path):
    from newsroom.llm import UsageMeter
    from newsroom.models import UsageRecord

    class MeteredLLM(ScriptedLLM):
        def __init__(self, *replies):
            super().__init__(*replies)
            self.meter = UsageMeter(50)

        def structured(self, role, user_text, payload, schema, **kw):
            self.meter.record(UsageRecord(role=role, model="mock"))
            return super().structured(role, user_text, payload, schema, **kw)

    settings = _settings(tmp_path)
    placed = {"passage": "चेतावनी प्रणाली असफल भयो।", "problem": "stronger than the record", "fix": "प्रहरीका अनुसार चेतावनी ढिलो आयो।"}
    lost = {"passage": "यो वाक्य कतै छैन।", "problem": "a fact is missing", "fix": "थप तथ्य।"}
    fixed_body = PIECE["body_markdown"].replace(placed["passage"], placed["fix"])
    llm = MeteredLLM(
        PIECE,
        {"decision": "revise", "problems": [placed, lost], "reason": "r"},  # the first reading, shared
        {**PIECE, "take": "पुनर्लेखन १"},  # rewrite: the writer applies both
        {"decision": "revise", "problems": [{"passage": "पुनर्लेखन १", "problem": "p", "fix": "पुनर्लेखन २"}], "reason": "r"},
        {**PIECE, "take": "पुनर्लेखन २"},
        {"decision": "approve", "problems": [], "reason": "ok"},  # rewrite: the closing reading
        {**PIECE, "body_markdown": fixed_body, "take": "लेखकले राखेको"},  # in place: the writer places only the lost fix
        {"decision": "approve", "problems": [], "reason": "ok"},
        {"decision": "approve", "problems": [], "reason": "ok"},  # in place: the closing reading
    )
    out = nepali.trial(llm, settings, _article())
    assert out["first_reading"] == [placed, lost]
    a, b = out["rewrite"], out["in_place"]
    assert a["piece"]["take"] == "पुनर्लेखन २" and len(a["second_reading"]) == 1 and a["closing_reading"] == [] and a["placed"] == 0
    assert b["piece"]["body_markdown"] == fixed_body and b["second_reading"] == [] and b["closing_reading"] == [] and b["placed"] == 1
    assert [r.role for r in a["records"]] == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"]
    assert [r.role for r in b["records"]] == ["nepali_writer", "nepali_editor", "nepali_editor"]
    writer_fix_for_b = llm.calls[6][1]
    assert writer_fix_for_b["fixes"] == [lost] and writer_fix_for_b["nepali"]["body_markdown"] == fixed_body  # from the same draft, placed fix in


def test_the_trial_can_fix_the_draft_one_way_only(tmp_path):
    settings = _settings(tmp_path)
    problem = {"passage": "चेतावनी प्रणाली असफल भयो।", "problem": "a fact without its source", "fix": "प्रहरीका अनुसार चेतावनी ढिलो आयो।"}
    llm = ScriptedLLM(
        PIECE,
        {"decision": "revise", "problems": [problem], "reason": "r"},  # the first reading
        {**PIECE, "take": "पुनर्लेखन १"},
        {"decision": "approve", "problems": [], "reason": "ok"},  # the second reading finds nothing, so no second fix
        {"decision": "approve", "problems": [], "reason": "ok"},  # the closing reading
    )
    llm.meter = UsageMeter(20)
    out = nepali.trial(llm, settings, _article(), ways=("rewrite",))
    assert "in_place" not in out and out["rewrite"]["piece"]["take"] == "पुनर्लेखन १" and out["rewrite"]["closing_reading"] == []
    assert [c[0] for c in llm.calls] == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor", "nepali_editor"]


def test_rejudge_reads_every_trials_pieces_again_with_one_editor(tmp_path, capsys):
    import json

    from newsroom import __main__ as cli
    from newsroom.config import load_settings

    story = publish.load_articles(load_settings(mock=True))[0].id
    paths = []
    for trial_id in ("111", "222"):
        folder = tmp_path / trial_id
        folder.mkdir()
        rows = [{"id": story, "rewrite": {"piece": PIECE}}, {"id": "no-such-story", "rewrite": {"piece": PIECE}}]
        (folder / "nepali-trial.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        paths += ["--pieces", str(folder / "nepali-trial.json")]
    before = sorted(p.name for p in (cli.ROOT / "data").rglob("*"))
    assert cli.main(["nepali-rejudge", "--mock", *paths, "--readings", "2", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "read again by one editor: 111, 222" in out and "Skipped 111 no-such-story" in out
    saved = json.loads((tmp_path / "out" / "nepali-rejudge.json").read_text(encoding="utf-8"))
    assert sorted((r["trial"], r["reading"]) for r in saved) == [("111", 1), ("111", 2), ("222", 1), ("222", 2)]
    assert sorted(p.name for p in (cli.ROOT / "data").rglob("*")) == before


def test_the_trial_command_saves_nothing(tmp_path, monkeypatch, capsys):
    import json

    from newsroom import __main__ as cli

    before = sorted(p.name for p in (cli.ROOT / "data").rglob("*"))
    assert cli.main(["nepali-trial", "--mock", "--limit", "1", "--out", str(tmp_path / "trial")]) == 0
    assert sorted(p.name for p in (cli.ROOT / "data").rglob("*")) == before
    assert "Nepali fix trial" in capsys.readouterr().out and (tmp_path / "trial" / "nepali-trial.json").exists()

    assert cli.main(["nepali-trial", "--mock", "--limit", "1", "--ways", "rewrite", "--out", str(tmp_path / "one")]) == 0
    out = capsys.readouterr().out
    assert "Nepali fix trial: rewrite, 1 story" in out and "In place" not in out
    [saved] = json.loads((tmp_path / "one" / "nepali-trial.json").read_text(encoding="utf-8"))
    assert "rewrite" in saved and "in_place" not in saved and "records" not in saved["rewrite"]
    assert sorted(p.name for p in (cli.ROOT / "data").rglob("*")) == before


WRONG = {"passage": "चेतावनी प्रणाली असफल भयो।", "problem": "the record attributes this to the police", "fix": "प्रहरीका अनुसार चेतावनी ढिलो आयो।", "severity": "fact"}
CLUMSY = {"passage": "नदी बढ्यो।", "problem": "reads like English word order", "fix": "नदी बढ्यो।", "severity": "language"}


def test_a_fact_still_wrong_at_the_final_reading_holds_the_piece(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=2)
    revise = {"decision": "revise", "problems": [WRONG, CLUMSY], "reason": "r"}
    llm = ScriptedLLM(PIECE, revise, PIECE, revise, PIECE, revise)
    ne = nepali.nepali_for(llm, settings, _article())
    assert [c[0] for c in llm.calls] == ["nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor", "nepali_writer", "nepali_editor"]
    assert ne["held"] is True and ne["approved"] is False and ne["passes"] == 3
    assert ne["fact_problems"] == ["the record attributes this to the police"] and ne["language_notes"] == 1
    assert ne["language_fixes"] == [{"passage": "नदी बढ्यो।", "problem": "reads like English word order", "fix": "नदी बढ्यो।"}]
    assert not nepali.usable(ne)  # no Nepali page, card, caption or Reel


def test_language_notes_alone_never_hold_a_piece(tmp_path):
    settings = _settings(tmp_path, nepali_rounds=1)
    llm = ScriptedLLM(PIECE, {"decision": "revise", "problems": [CLUMSY], "reason": "r"}, PIECE, {"decision": "revise", "problems": [CLUMSY], "reason": "r"})
    ne = nepali.nepali_for(llm, settings, _article())
    assert ne["held"] is False and ne["approved"] is True and ne["fact_problems"] == [] and ne["language_notes"] == 1
    assert nepali.usable(ne)


def test_an_unmarked_problem_counts_as_a_fact():
    assert nepali.facts([{"problem": "x"}]) == [{"problem": "x"}]
    assert nepali.facts([{"problem": "x", "severity": "language"}]) == []
    assert nepali.facts([{"problem": "x", "severity": "FACT"}]) == [{"problem": "x", "severity": "FACT"}]


HELD = {**PIECE, "checked": True, "approved": False, "held": True, "fact_problems": ["the record attributes this to the police"], "language_notes": 0, "passes": 3, "problems_fixed": 4, "editor": "r"}
TEXT = ("headline", "dek", "take", "body_markdown", "image_headline", "social_hook", "caption")


def test_a_recheck_reads_the_stored_piece_once_and_never_rewrites_it(tmp_path):
    settings = _settings(tmp_path)
    llm = ScriptedLLM({"decision": "approve", "problems": [], "reason": "ok"})
    ne = nepali.recheck(llm, settings, _article(nepali=copy.deepcopy(HELD)))
    assert [c[0] for c in llm.calls] == ["nepali_editor"]
    assert llm.calls[0][1]["nepali"]["body_markdown"] == HELD["body_markdown"]  # the editor reads what is stored
    assert ne["held"] is False and ne["approved"] is True and ne["fact_problems"] == [] and ne["passes"] == 4
    assert {k: ne[k] for k in TEXT} == {k: HELD[k] for k in TEXT} and ne["problems_fixed"] == 4
    assert nepali.usable(ne)


def test_a_recheck_that_still_finds_a_wrong_fact_keeps_the_piece_held(tmp_path):
    settings = _settings(tmp_path)
    llm = ScriptedLLM({"decision": "revise", "problems": [WRONG, CLUMSY], "reason": "still wrong"})
    ne = nepali.recheck(llm, settings, _article(nepali=copy.deepcopy(HELD)))
    assert ne["held"] is True and ne["approved"] is False and ne["editor"] == "still wrong"
    assert ne["fact_problems"] == ["the record attributes this to the police"] and ne["language_notes"] == 1
    assert [n["problem"] for n in ne["language_fixes"]] == ["reads like English word order"]
    assert {k: ne[k] for k in TEXT} == {k: HELD[k] for k in TEXT}
    # Language notes alone clear it.
    ne = nepali.recheck(ScriptedLLM({"decision": "revise", "problems": [CLUMSY], "reason": "r"}), settings, _article(nepali=copy.deepcopy(HELD)))
    assert ne["held"] is False and ne["language_notes"] == 1


def test_the_recheck_command_reads_only_held_stories_and_saves_the_verdict(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    held = _article(id="2026-10-01-held", slug="held", nepali=copy.deepcopy(HELD))
    fine = _article(id="2026-10-01-fine", slug="fine", nepali={**copy.deepcopy(HELD), "held": False, "approved": True, "fact_problems": []})
    for article in (held, fine):
        publish.save_article(settings, article)
    assert cli.main(["nepali", "--mock", "--recheck"]) == 0
    out = capsys.readouterr().out
    assert "reads 1 story again" in out and "cleared    2026-10-01-held" in out and "2026-10-01-fine" not in out
    stored = {a.id: a.nepali for a in publish.load_articles(settings)}
    assert stored["2026-10-01-held"]["held"] is False and stored["2026-10-01-held"]["passes"] == 4
    assert stored["2026-10-01-fine"]["passes"] == 3  # not read
    assert cli.main(["nepali", "--mock", "--recheck", "--article", "2026-10-01-nope"]) == 1
    assert "No stored story with id 2026-10-01-nope." in capsys.readouterr().out
