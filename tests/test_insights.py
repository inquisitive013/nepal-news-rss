import dataclasses
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from newsroom import insights, social
from newsroom.config import load_settings

NOW = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
ENV = {"FACEBOOK_PAGE_ID": "111", "FACEBOOK_PAGE_TOKEN": "EAABfaketoken_1234567890abcdefghijklmnop"}


@pytest.fixture(autouse=True)
def _fresh_facebook_token_cache():
    social._PAGE_TOKENS.clear()
    yield


def _settings(tmp_path):
    s = load_settings(mock=True)
    return dataclasses.replace(s, root=tmp_path, data_dir=tmp_path / "data")


def _post(post_id, hours_ago, **kw):
    at = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return social.Post(network="facebook", status=kw.pop("status", "posted"), text="t", id=post_id, posted_at=at, **kw)


def _save(settings, article_id, *posts, story_id=""):
    social.save_record(settings, social.SocialRecord(article_id=article_id, posts=list(posts)))
    art_dir = settings.data_dir / "articles"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / f"{article_id}.json").write_text(json.dumps({"id": article_id, "story_id": story_id}), encoding="utf-8")


def _graph(numbers=None, fail=None, followers=10_300):
    """A Graph API stand in. `numbers[post_id]` holds its values; `fail` maps a metric or field to an error code."""
    numbers = numbers or {}
    fail = fail or {}
    calls = []

    def handler(request):
        url = request.url
        path = url.path.rsplit("/", 2)
        calls.append(f"{url.path}?{url.params.get('metric') or url.params.get('fields') or ''}")
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.path.endswith("/111"):
            if "followers_count" in fail:
                return httpx.Response(400, json={"error": {"message": "secret words", "code": fail["followers_count"]}})
            return httpx.Response(200, json={"followers_count": followers, "id": "111"})
        if path[-1] == "insights":
            post_id, metric = path[-2], url.params["metric"]
            if metric in fail:
                return httpx.Response(400, json={"error": {"message": "secret words", "code": fail[metric]}})
            if url.params.get("period") != "lifetime":  # as Meta does: a lifetime metric asked without its period
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": [{"name": metric, "period": "lifetime", "values": [{"value": numbers[post_id][metric]}]}]})
        post_id, fields = path[-1], url.params["fields"]
        if fields == "page_story_id":
            if "page_story_id" in fail:
                return httpx.Response(400, json={"error": {"message": "secret words", "code": fail["page_story_id"]}})
            return httpx.Response(200, json={"page_story_id": numbers[post_id]["page_story_id"], "id": post_id})
        name = fields.split(".")[0]
        if name in fail:
            return httpx.Response(400, json={"error": {"message": "secret words", "code": fail[name]}})
        n = numbers[post_id][name]
        if name == "shares":
            return httpx.Response(200, json={"id": post_id} if n == 0 else {"shares": {"count": n}, "id": post_id})
        return httpx.Response(200, json={name: {"data": [], "summary": {"total_count": n}}, "id": post_id})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _numbers(viewers, views, shares, comments, reactions):
    return {"post_total_media_view_unique": viewers, "post_media_view": views, "shares": shares, "comments": comments, "reactions": reactions}


def test_a_reading_is_due_from_its_mark_for_a_day_and_until_it_comes_back_whole():
    assert not insights.due(_post("111_1", 23), 24, NOW)
    assert insights.due(_post("111_1", 25), 24, NOW)
    assert not insights.due(_post("111_1", 49), 24, NOW)  # a day late: missed, never read at 49 hours as if at 24
    assert insights.due(_post("111_1", 73), 72, NOW)
    assert not insights.due(_post("111_1", 30, metrics={"24h": {"viewers": 5}}), 24, NOW)
    assert insights.due(_post("111_1", 30, metrics={"24h": {"shares": 1, "errors": {"viewers": "code 10"}}}), 24, NOW)
    assert not insights.due(_post("111_1", 30, status="removed"), 24, NOW)
    # a scheduled post counts from the hour Facebook released it, not the hour it was handed over
    scheduled = _post("111_1", 30, scheduled_for=(NOW - timedelta(hours=20)).isoformat(timespec="minutes"))
    assert not insights.due(scheduled, 24, NOW)
    assert insights.went_live(social.Post(network="facebook", status="posted", scheduled_for="2026-09-29T12:30+05:45")) == datetime(2026, 9, 29, 6, 45, tzinfo=timezone.utc)


def test_readings_land_on_the_post_and_the_followers_are_noted(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-floods", _post("111_1", 25), _post("111_0", 26, status="removed"))
    _save(settings, "2026-09-28-rana", _post("111_2", 74, metrics={"24h": {"viewers": 900}}))
    _save(settings, "2026-09-30-fresh", _post("111_3", 3))
    client, calls = _graph({"111_1": _numbers(4200, 6100, 0, 31, 250), "111_2": _numbers(9000, 15000, 40, 120, 800)})

    out = insights.take_readings(settings, ENV, client=client, now=NOW)

    assert not out["failed"] and not out["stopped"] and out["followers"] == 10_300
    floods = social.read_record(settings.data_dir / "social" / "2026-09-30-floods.json")
    assert floods.posts[0].metrics["24h"] == {"at": NOW.isoformat(timespec="seconds"), "hours": 25.0, "viewers": 4200, "views": 6100, "shares": 0, "comments": 31, "reactions": 250}
    assert floods.posts[1].metrics == {}  # a post that was taken down is not read
    rana = social.read_record(settings.data_dir / "social" / "2026-09-28-rana.json")
    assert rana.posts[0].metrics["24h"] == {"viewers": 900} and rana.posts[0].metrics["72h"]["viewers"] == 9000
    assert social.read_record(settings.data_dir / "social" / "2026-09-30-fresh.json").posts[0].metrics == {}
    assert not any("111_3" in c or "111_0" in c for c in calls)
    assert json.loads((settings.data_dir / "insights" / "followers.json").read_text())["readings"] == [{"at": NOW.isoformat(timespec="seconds"), "followers": 10_300}]

    # an hour later nothing is due again, and the follower count is not asked for twice in an hour
    calls.clear()
    out = insights.take_readings(settings, ENV, client=client, now=NOW + timedelta(minutes=30))
    assert out["read"] == [] and calls == []


def test_a_retired_metric_is_recorded_by_code_and_read_again_while_the_day_lasts(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-floods", _post("111_1", 25))
    client, _ = _graph({"111_1": _numbers(0, 6100, 2, 31, 250)}, fail={"post_total_media_view_unique": 100})

    insights.take_readings(settings, ENV, client=client, now=NOW)
    reading = social.read_record(settings.data_dir / "social" / "2026-09-30-floods.json").posts[0].metrics["24h"]
    assert reading["views"] == 6100 and reading["errors"] == {"viewers": "code 100"}
    assert "secret words" not in json.dumps(reading)  # codes only, never Meta's message

    fixed, _ = _graph({"111_1": _numbers(4200, 6200, 2, 33, 260)})
    insights.take_readings(settings, ENV, client=fixed, now=NOW + timedelta(hours=6))
    reading = social.read_record(settings.data_dir / "social" / "2026-09-30-floods.json").posts[0].metrics["24h"]
    assert reading["viewers"] == 4200 and "errors" not in reading and reading["hours"] == 31.0


def test_a_token_without_read_permission_stops_at_the_first_post(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-a", _post("111_1", 25))
    _save(settings, "2026-09-30-b", _post("111_2", 26))
    fail = {k: 10 for k in ("post_total_media_view_unique", "post_media_view", "shares", "comments", "reactions", "followers_count")}
    client, calls = _graph({}, fail=fail)

    out = insights.take_readings(settings, ENV, client=client, now=NOW)

    assert out["read"] == [] and len(out["failed"]) == 1 and "code 10" in out["stopped"]
    assert out["followers"] is None and out["followers_error"] == "code 10"
    assert not any("111_2" in c for c in calls)  # the second post was never asked for
    assert social.read_record(settings.data_dir / "social" / "2026-09-30-a.json").posts[0].metrics == {}


def test_the_forecast_comes_from_the_final_verdict_and_old_runs_carry_it_in_the_reason(tmp_path):
    settings = _settings(tmp_path)
    runs = settings.data_dir / "runs"
    runs.mkdir(parents=True)
    old = {"started_at": "2026-09-29T00:00:00+00:00", "ranking": [
        {"judge": "ranking_judge_1", "ranked": [{"story_id": "s_flood", "rank": 1, "score": 70, "reason": "Reach is about 50 (hook 5)."}]},
        {"judge": "ranking_judge_2", "ranked": [{"story_id": "s_flood", "rank": 1, "score": 64, "reason": "Nepal Press reports 27 dead. Reach is about 59 (hook 6, stakes 7), and the weakest dimension is comment friction."}]},
    ]}
    new = {"started_at": "2026-09-30T00:00:00+00:00", "ranking": [
        {"judge": "ranking_judge_2", "ranked": [{"story_id": "s_rana", "rank": 1, "score": 78, "reach": 69, "reason": "Primary record."}]},
    ]}
    (runs / "2026-09-29.json").write_text(json.dumps(old))
    (runs / "2026-09-30.json").write_text(json.dumps(new))
    assert insights.load_forecasts(settings) == {"s_flood": {"score": 64, "reach": 59}, "s_rana": {"score": 78, "reach": 69}}


def test_the_report_sets_each_post_beside_its_forecast_and_the_follower_change(tmp_path):
    settings = _settings(tmp_path)
    runs = settings.data_dir / "runs"
    runs.mkdir(parents=True)
    (runs / "r.json").write_text(json.dumps({"started_at": "x", "ranking": [{"judge": "ranking_judge_2", "ranked": [{"story_id": "s_flood", "score": 64, "reach": 59}]}]}))
    reading = {"at": "x", "hours": 25.0, "viewers": 4200, "views": 6100, "shares": 3, "comments": 31, "reactions": 250}
    _save(settings, "2026-09-30-floods", _post("111_1", 30, metrics={"24h": reading}), story_id="s_flood")
    _save(settings, "2026-09-20-old", _post("111_9", 24 * 11))
    folder = settings.data_dir / "insights"
    folder.mkdir(parents=True)
    counts = [(NOW - timedelta(hours=32), 10_280), (NOW - timedelta(hours=6), 10_311), (NOW, 10_320)]
    (folder / "followers.json").write_text(json.dumps({"readings": [{"at": t.isoformat(timespec="seconds"), "followers": n} for t, n in counts]}))

    lines = insights.report(settings, now=NOW)

    assert len(lines) == 7  # header, rule, the one post of the last week; then the Reels table with none
    assert "| 2026-09-30-floods | 59 | 4,200 | 6,100 | 3 | 31 | 250 | – | +31 over 26.0h |" in lines[2]
    assert lines[6].startswith("| – | no Reel went live")
    assert insights.report(settings, now=NOW + timedelta(days=30))[2].startswith("| – | no Facebook post went live")


def test_the_command_says_so_when_facebook_is_not_connected(tmp_path, monkeypatch, capsys):
    from newsroom import __main__ as cli

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_TOKEN", raising=False)
    assert cli.main(["insights"]) == 0
    assert "Facebook is not connected" in capsys.readouterr().out


def test_the_judges_reach_forecast_is_kept_and_held_to_the_scale():
    from newsroom import ranking

    verdict = ranking._verdict("ranking_judge_2", {"ranked": [{"story_id": "a", "rank": 1, "score": 80, "reach": 140, "reason": "r"}, {"story_id": "b", "rank": 2, "score": 60, "reason": "no forecast"}]}, {"a", "b"})
    assert verdict.ranked[0]["reach"] == 100 and "reach" not in verdict.ranked[1]


def test_an_answer_without_a_number_says_what_shape_it_had():
    def answer(payload):
        return httpx.Response(200, json=payload)

    assert insights._metric_value(answer({"data": [{"values": [{"value": 4200}]}]})) == (4200, "")
    assert insights._metric_value(answer({"data": []})) == (0, "empty")
    assert insights._metric_value(answer({"data": [{"name": "m"}]})) == (0, "no values")
    assert insights._metric_value(answer({"data": [{"values": [{"value": {"organic": 3}}]}]})) == (0, "a breakdown, not a number")
    assert insights._metric_value(answer({"data": [{"values": [{"value": None}]}]})) == (0, "NoneType, not a number")
    assert insights._metric_value(httpx.Response(200, text="<html>")) == (0, "not JSON")


def test_the_probe_names_permissions_and_answer_shapes_never_ids_or_values(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-29-old", _post("111_5", 30))
    _save(settings, "2026-09-30-new", _post("111_7", 3))
    asked = []

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"type": "PAGE", "is_valid": True, "expires_at": 0, "user_id": "9876543", "profile_id": "111", "scopes": ["read_insights", "pages_show_list"]}})
        asked.append((url.path.rsplit("/", 2)[-2], url.params["metric"], url.params["period"]))
        if url.params["metric"] == "post_impressions_unique":
            return httpx.Response(400, json={"error": {"message": "secret words", "code": 100}})
        if url.params["metric"] == "post_reactions_by_type_total":
            return httpx.Response(200, json={"data": [{"values": [{"value": 4242}]}]})
        return httpx.Response(200, json={"data": []})

    lines = insights.probe(settings, ENV, client=httpx.Client(transport=httpx.MockTransport(handler)))
    text = "\n".join(lines)
    assert lines[0] == "- Token: page token, valid, never expires. Permissions: pages_show_list, read_insights"
    assert "- Newest post, asked on the post, post_media_view: empty" in lines
    assert "- Newest post, asked on the photo, post_reactions_by_type_total: a number" in lines
    assert "- Newest post, asked on the post, post_impressions_unique: code 100" in lines
    assert "- The Page, page_media_view by day: empty" in lines
    assert ("111_7", "post_media_view", "lifetime") in asked and ("7", "post_media_view", "lifetime") in asked  # the newest post, and its photo
    assert not any(a[0] in ("111_5", "5") for a in asked)
    assert "9876543" not in text and "4242" not in text and "secret words" not in text


def test_a_scheduled_photo_reads_its_counts_from_the_post_it_became(tmp_path):
    """Until 1 October a scheduled photo's shares and reactions came back as code 100: they belong to the post."""
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-rana", _post("555", 25, scheduled_for=(NOW - timedelta(hours=25)).isoformat(timespec="seconds")))
    numbers = {"555": {**_numbers(40, 90, 0, 0, 0), "page_story_id": "111_777"}, "111_777": _numbers(0, 0, 2, 5, 17)}
    client, calls = _graph(numbers)

    insights.take_readings(settings, ENV, client=client, now=NOW)

    reading = social.read_record(settings.data_dir / "social" / "2026-09-30-rana.json").posts[0].metrics["24h"]
    assert reading["viewers"] == 40 and reading["views"] == 90  # insights from the photo
    assert reading["shares"] == 2 and reading["comments"] == 5 and reading["reactions"] == 17 and "errors" not in reading  # counts from the post
    assert any(c.endswith("/555?page_story_id") for c in calls) and not any(c.endswith("/555?shares") for c in calls)

    # When Meta will not say which post it became, the photo is read as before.
    settings2 = _settings(tmp_path / "two")
    _save(settings2, "2026-09-30-rana", _post("555", 25))
    client, _ = _graph({"555": _numbers(40, 90, 0, 0, 0)}, fail={"page_story_id": 100})
    insights.take_readings(settings2, ENV, client=client, now=NOW)
    assert social.read_record(settings2.data_dir / "social" / "2026-09-30-rana.json").posts[0].metrics["24h"]["viewers"] == 40


def _reel(video_id, hours_ago, **kw):
    at = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return social.Post(network="facebook_reel", status=kw.pop("status", "posted"), text="t", id=video_id, posted_at=at, **kw)


def _video_graph(default, alone=None, fail=None):
    """video_insights as Meta answers it: a default set with no metric named, one metric when named."""
    alone = alone or {}
    calls = []

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.path.endswith("/111"):
            return httpx.Response(200, json={"followers_count": 10_300, "id": "111"})
        metric = url.params.get("metric")
        calls.append(f"{url.path}?{metric or ''}")
        if fail is not None:
            return httpx.Response(400, json={"error": {"message": "secret words", "code": fail}})
        rows = default if metric is None else {k: v for k, v in alone.items() if k == metric}
        return httpx.Response(200, json={"data": [{"name": k, "period": "lifetime", "values": [{"value": v}], "title": "t", "id": f"x/{k}"} for k, v in rows.items()]})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_a_reel_is_read_from_its_video_insights_by_name(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-pal", _reel("999", 25), _reel("998", 30, status="removed"))
    default = {"blue_reels_play_count": 500, "post_video_avg_time_watched": 6400, "post_video_likes_by_reaction_type": {"like": 3}}
    client, calls = _video_graph(default, alone={"post_impressions_unique": 300})

    out = insights.take_readings(settings, ENV, client=client, now=NOW)

    reading = social.read_record(settings.data_dir / "social" / "2026-09-30-pal.json").posts[0].metrics["24h"]
    assert reading == {"at": NOW.isoformat(timespec="seconds"), "hours": 25.0, "plays": 500, "viewers": 300, "avg_watch_ms": 6400}
    assert [c.split("/", 2)[2] for c in calls] == ["999/video_insights?", "999/video_insights?post_impressions_unique"]  # the removed Reel is not read
    assert out["read"][0][0] == "2026-09-30-pal"
    lines = insights.report(settings, now=NOW)
    assert any(line.endswith("| 2026-09-30-pal | 500 | 300 | 6.4 s | – | – |") for line in lines)


def test_a_reel_metric_meta_does_not_return_is_named_and_asked_again_next_run(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-pal", _reel("999", 25))
    client, _ = _video_graph({"blue_reels_play_count": 500, "post_video_avg_time_watched": {"a": 1}})
    insights.take_readings(settings, ENV, client=client, now=NOW)
    reading = social.read_record(settings.data_dir / "social" / "2026-09-30-pal.json").posts[0].metrics["24h"]
    assert reading["plays"] == 500
    assert reading["errors"] == {"viewers": "not returned", "avg_watch_ms": "no value (a breakdown, not a number)"}
    assert insights.due(social.read_record(settings.data_dir / "social" / "2026-09-30-pal.json").posts[0], 24, NOW + timedelta(hours=6))

    refused, _ = _video_graph({}, fail=10)
    out = insights.take_readings(_settings(tmp_path / "two"), ENV, client=refused, now=NOW)
    assert out["read"] == []


def test_the_probe_lists_the_names_a_reel_answers_with_never_values(tmp_path):
    settings = _settings(tmp_path)
    _save(settings, "2026-09-30-pal", _reel("999", 25), _post("555", 26))

    def handler(request):
        url = request.url
        if url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "111"})
        if url.path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"type": "PAGE", "is_valid": True, "expires_at": 0, "scopes": ["read_insights"]}})
        if url.path.endswith("/555") and url.params.get("fields") == "page_story_id":
            return httpx.Response(200, json={"page_story_id": "111_777", "id": "555"})
        if url.path.endswith("/999/video_insights"):
            if url.params.get("metric") == "post_impressions_unique":
                return httpx.Response(400, json={"error": {"message": "secret words", "code": 100}})
            return httpx.Response(200, json={"data": [{"name": "blue_reels_play_count", "values": [{"value": 4242}]}, {"name": "post_video_likes_by_reaction_type", "values": [{"value": {"like": 7}}]}]})
        return httpx.Response(200, json={"data": []})

    lines = insights.probe(settings, ENV, client=httpx.Client(transport=httpx.MockTransport(handler)))
    text = "\n".join(lines)
    assert "- Newest scheduled photo, page_story_id: a post id" in lines
    assert "- Newest Reel, video_insights by default: blue_reels_play_count (a number), post_video_likes_by_reaction_type (a breakdown, not a number)" in lines
    assert "- Newest Reel, post_impressions_unique asked on its own: code 100" in lines
    assert "- Newest Reel, post_video_avg_time_watched asked on its own: not returned" in lines
    assert "4242" not in text and "777" not in text and "secret words" not in text
