import dataclasses
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from newsroom import comments, social
from newsroom.config import load_settings
from newsroom.llm import MockLLM, UsageMeter

NOW = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
ENV = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABfaketoken_1234567890abcdefghijklmnop"}
QUESTION = "बाढीको चेतावनी कहिले आउँछ?"
STORY = "हाम्रो गाउँमा पनि राति पानी पस्यो।"
WRONG = "यो समाचार गलत छ।"
SPAM = "सस्तो लोन http://spam.example"


@pytest.fixture(autouse=True)
def _fresh_facebook_token_cache():
    social._PAGE_TOKENS.clear()
    yield


def _settings(tmp_path, **desk):
    s = load_settings(mock=True)
    raw = dict(s.raw)
    fb = dict((raw.get("social") or {}).get("facebook") or {})
    fb["comments"] = {**(fb.get("comments") or {}), **desk}
    raw["social"] = dict(raw.get("social") or {}, facebook=fb)
    return dataclasses.replace(s, raw=raw, root=tmp_path, data_dir=tmp_path / "data")


def _story(settings, article_id="2026-09-30-bagmati", minutes_ago=30, post_id="111_1", **post):
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    p = social.Post(network="facebook", status=post.pop("status", "posted"), text="प्रश्न?", id=post_id, url=f"https://www.facebook.com/{post_id}", posted_at=at, **post)
    social.save_record(settings, social.SocialRecord(article_id=article_id, posts=[p]))
    folder = settings.data_dir / "articles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{article_id}.json").write_text(json.dumps({"id": article_id, "headline": "Bagmati floods move 140 households", "dek": "d", "key_facts": ["140 households moved"], "body_markdown": "Police moved 140 households."}), encoding="utf-8")


def _graph(threads, refuse=False):
    """A Graph API stand in: `threads[post_id]` lists (comment_id, text, author_id)."""
    calls = []

    def handler(request):
        url = request.url
        parts = url.path.strip("/").split("/")
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if request.method == "GET" and parts[-1] == "comments":
            calls.append(("read", parts[-2]))
            assert url.params["filter"] == "toplevel"
            return httpx.Response(200, json={"data": [{"id": cid, "message": text, "created_time": "t", "from": {"id": author}} for cid, text, author in threads.get(parts[-2], [])]})
        if request.method == "POST" and parts[-1] == "comments":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            calls.append(("reply", parts[-2], form["message"]))
            if refuse:
                return httpx.Response(400, json={"error": {"message": "no", "code": 200}})
            return httpx.Response(200, json={"id": f"r_{parts[-2]}"})
        calls.append(("unexpected", request.method, url.path))
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _desk(settings, client, **kw):
    llm = MockLLM(settings, UsageMeter(10))
    return comments.run_desk(settings, ENV, llm, client=client, now=kw.pop("now", NOW), **kw), llm


def test_the_desk_answers_readers_and_leaves_the_rest_for_a_person(tmp_path):
    settings = _settings(tmp_path)
    _story(settings)
    thread = {"111_1": [("c1", QUESTION, "u1"), ("c2", STORY, "u2"), ("c3", WRONG, "u3"), ("c4", SPAM, "u4"), ("c5", "पेजको आफ्नै कमेन्ट", "111")]}
    client, calls = _graph(thread)

    result, llm = _desk(settings, client)

    assert [c[:2] for c in calls if c[0] == "reply"] == [("reply", "c1"), ("reply", "c2")]
    assert [(a, c) for a, c, _ in result["replied"]] == [("2026-09-30-bagmati", "question"), ("2026-09-30-bagmati", "answer")]
    assert result["flagged"] == [("2026-09-30-bagmati", "correction", "https://www.facebook.com/111_1")]
    assert result["skipped"] == {"abuse": 1}
    saved = (settings.data_dir / "social" / "2026-09-30-bagmati.json").read_text(encoding="utf-8")
    desk = json.loads(saved)["posts"][0]["comment_desk"]
    assert [(d["comment_id"], d["category"], d.get("reply_id", "")) for d in desk] == [("c1", "question", "r_c1"), ("c2", "answer", "r_c2"), ("c3", "correction", ""), ("c4", "abuse", "")]
    assert desk[2]["flag"] is True
    for words in (QUESTION, STORY, WRONG, SPAM):  # readers' words never reach the record
        assert words not in saved

    lines = "\n".join(comments.summary(result))
    assert "replied to a question: प्रहरीका अनुसार" in lines and "1 comment needs a person (correction): https://www.facebook.com/111_1" in lines
    assert "replied to an answer: " in lines
    for words in (QUESTION, STORY, WRONG, SPAM):  # nor the public run summary
        assert words not in lines

    # the next run finds nothing new and asks the model nothing
    calls.clear()
    result, llm = _desk(settings, client, now=NOW + timedelta(minutes=15))
    assert [c for c in calls if c[0] == "reply"] == [] and llm.calls == [] and result["replied"] == []


def test_only_live_posts_inside_the_window_are_read(tmp_path):
    settings = _settings(tmp_path)
    _story(settings, "2026-09-29-old", minutes_ago=180, post_id="111_2")
    _story(settings, "2026-09-30-gone", minutes_ago=20, post_id="111_3", status="removed")
    client, calls = _graph({"111_2": [("c9", QUESTION, "u9")], "111_3": [("c8", QUESTION, "u8")]})
    result, _ = _desk(settings, client)
    assert calls == [] and result["posts"] == 0
    assert comments.summary(result) == ["## Comment desk", "", "- No post is inside its reply window."]


def test_the_guard_holds_links_tags_repeats_and_long_replies():
    assert comments.guard("धन्यवाद, तपाईंको अनुभवले धेरैलाई छोयो।", []) == ""
    assert comments.guard("हेर्नुहोस् https://x.example", []) == "a link, a tag or a hashtag"
    assert comments.guard("@someone धन्यवाद", []) == "a link, a tag or a hashtag"
    assert comments.guard("#नेपाल धन्यवाद", []) == "a link, a tag or a hashtag"
    assert comments.guard("धन्यवाद  सबैलाई", ["धन्यवाद सबैलाई"]) == "a repeat"
    assert comments.guard("क" * 281, []) == "too long"
    assert comments.guard("   ", []) == "empty"


def test_a_dry_run_posts_and_saves_nothing(tmp_path):
    settings = _settings(tmp_path)
    _story(settings)
    before = (settings.data_dir / "social" / "2026-09-30-bagmati.json").read_text(encoding="utf-8")
    client, calls = _graph({"111_1": [("c1", QUESTION, "u1"), ("c2", STORY, "u2")]})
    result, _ = _desk(settings, client, dry_run=True)
    assert [c for c in calls if c[0] == "reply"] == [] and len(result["replied"]) == 2
    assert (settings.data_dir / "social" / "2026-09-30-bagmati.json").read_text(encoding="utf-8") == before
    assert "would reply to a question" in "\n".join(comments.summary(result, dry_run=True))


def test_with_replies_off_the_desk_sorts_and_posts_nothing(tmp_path):
    settings = _settings(tmp_path, reply=False)
    _story(settings)
    client, calls = _graph({"111_1": [("c1", QUESTION, "u1"), ("c3", WRONG, "u3")]})
    result, _ = _desk(settings, client)
    assert [c for c in calls if c[0] == "reply"] == [] and result["replied"] == []
    assert result["skipped"] == {"question": 1} and [f[1] for f in result["flagged"]] == ["correction"]


def test_a_refused_reply_is_tried_again_and_the_run_cap_holds(tmp_path):
    settings = _settings(tmp_path, max_replies_per_run=1)
    _story(settings)
    thread = {"111_1": [("c1", QUESTION, "u1"), ("c2", STORY, "u2")]}
    client, calls = _graph(thread, refuse=True)
    result, _ = _desk(settings, client)
    assert result["replied"] == [] and "Facebook refused a reply" in result["held"][0][1]
    assert json.loads((settings.data_dir / "social" / "2026-09-30-bagmati.json").read_text())["posts"][0]["comment_desk"] == []

    client, calls = _graph(thread)
    result, _ = _desk(settings, client, now=NOW + timedelta(minutes=15))
    assert [c[1] for c in calls if c[0] == "reply"] == ["c1"]  # one reply a run
    result, _ = _desk(settings, client, now=NOW + timedelta(minutes=30))
    assert [c[1] for c in calls if c[0] == "reply"] == ["c1", "c2"]  # the other next time


def test_the_command_says_so_when_facebook_is_not_connected(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_TOKEN", raising=False)
    assert cli.main(["comments"]) == 0
    assert "Facebook is not connected" in capsys.readouterr().out


def test_a_model_failure_records_nothing_and_a_spent_budget_stops_the_run(tmp_path):
    from newsroom.llm import BudgetExceeded, LLMRefusal

    settings = _settings(tmp_path)
    _story(settings, "2026-09-30-a", post_id="111_1")
    _story(settings, "2026-09-30-b", post_id="111_2")
    client, calls = _graph({"111_1": [("c1", QUESTION, "u1")], "111_2": [("c2", QUESTION, "u2")]})

    class Failing:
        def __init__(self, exc):
            self.exc, self.asked = exc, 0

        def structured(self, *a, **k):
            self.asked += 1
            raise self.exc

    refusing = Failing(LLMRefusal("no"))
    result = comments.run_desk(settings, ENV, refusing, client=client, now=NOW)
    assert refusing.asked == 2 and [h[1] for h in result["held"]] == ["the model could not sort the comments (LLMRefusal)"] * 2
    assert all(not json.loads(p.read_text())["posts"][0]["comment_desk"] for p in (settings.data_dir / "social").glob("*.json"))

    spent = Failing(BudgetExceeded("cap"))
    result = comments.run_desk(settings, ENV, spent, client=client, now=NOW)
    assert spent.asked == 1 and "model calls ran out" in result["held"][0][1]  # the second post waits for the next run


def test_a_wider_window_is_for_dry_runs_only(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.setenv("FACEBOOK_PAGE_ID", ENV["FACEBOOK_PAGE_ID"])
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", ENV["FACEBOOK_PAGE_TOKEN"])
    assert cli.main(["comments", "--window-minutes", "600"]) == 2
    assert "dry runs only" in capsys.readouterr().out
