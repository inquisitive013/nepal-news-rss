"""Command line entry point: python -m newsroom <command>."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

from . import discovery, images, publish
from .config import ROOT, load_settings
from .models import to_dict


def _settings(args):
    settings = load_settings(mock=(True if getattr(args, "mock", False) else None))
    fixtures = getattr(args, "fixtures", None)
    if fixtures:
        settings = dataclasses.replace(settings, fixtures_dir=Path(fixtures))
    elif settings.mock:
        settings = dataclasses.replace(settings, fixtures_dir=ROOT / "tests" / "fixtures" / "feeds")
    return settings


def with_articles(settings, count):
    """The settings with this run's article count. The judges still rank every debated story and publish the top ones."""
    if count is None:
        return settings
    if count < 1:
        raise SystemExit("--articles must be 1 or more")
    raw = dict(settings.raw)
    raw["pipeline"] = dict(raw.get("pipeline") or {}, articles_per_day=count)
    return dataclasses.replace(settings, raw=raw)


def cmd_run(args) -> int:
    from .pipeline import run

    settings = with_articles(_settings(args), args.articles)
    if args.articles is not None:
        logging.info("this run publishes at most %d stor%s", args.articles, "y" if args.articles == 1 else "ies")
    if not settings.mock:
        from .llm import auth_mode, scrub_empty_credentials

        scrub_empty_credentials()
        mode = auth_mode()
        if mode == "sdk_default":
            logging.warning("No ANTHROPIC_API_KEY and no federation variables set. The SDK will look for an `ant auth login` profile.")
        else:
            logging.info("model access via %s", mode)
    run_log = run(settings)
    totals = run_log.usage_totals()
    print(json.dumps({"run_date": run_log.run_date, "status": run_log.status, "published": run_log.published, "rejected": run_log.rejected, "errors": run_log.errors[:5], "usage": totals}, ensure_ascii=False, indent=2))
    if args.build:
        out = publish.build_site(settings, Path(args.out))
        if args.root_rss:
            publish.copy_root_rss(settings, out)
    return 0 if run_log.status == "ok" else 1


def cmd_discover(args) -> int:
    settings = _settings(args)
    candidates, health = discovery.discover(
        settings.sources,
        settings.google_news,
        window_hours=int(settings.get("pipeline.window_hours", 24)),
        max_per_source=int(settings.get("pipeline.max_per_source", 20)),
        max_total=int(settings.get("pipeline.max_candidates", 120)),
        fixtures_dir=settings.fixtures_dir,
    )
    if args.json:
        print(json.dumps({"candidates": to_dict(candidates), "feed_health": to_dict(health)}, ensure_ascii=False, indent=2))
        return 0
    print(_health_table(health))
    print(f"\n{len(candidates)} candidates in the window:\n")
    for c in candidates:
        print(f"- [{c.language}] {c.published or 'undated':25} {c.source:28} {c.title}")
    return 0


def _health_table(health) -> str:
    lines = ["| Source | Kind | OK | Entries | In window | Error |", "|---|---|---|---|---|---|"]
    for h in health:
        lines.append(f"| {h.source} | {h.kind} | {'yes' if h.ok else 'no'} | {h.entries} | {h.in_window} | {h.error.replace('|', '/')} |")
    return "\n".join(lines)


def cmd_check_sources(args) -> int:
    settings = _settings(args)
    candidates, health = discovery.discover(
        settings.sources,
        settings.google_news,
        window_hours=int(settings.get("pipeline.window_hours", 24)),
        max_per_source=int(settings.get("pipeline.max_per_source", 20)),
        max_total=int(settings.get("pipeline.max_candidates", 120)),
    )
    report = ["## Feed health", "", _health_table(health), "", f"**{len(candidates)} candidates** in the last {settings.get('pipeline.window_hours', 24)} hours.", ""]
    native_ok = [h.source for h in health if h.kind == "rss" and h.ok and h.in_window > 0]
    native_bad = [h.source for h in health if h.kind == "rss" and (not h.ok or h.in_window == 0)]
    report.append(f"Native feeds delivering items: {', '.join(native_ok) or 'none'}.")
    report.append(f"Native feeds failing or empty (Google News fallback used): {', '.join(native_bad) or 'none'}.")
    if native_bad and not args.no_probe:
        report += ["", "## Feed suggestions for failing sources", ""]
        by_name = {s["name"]: s for s in settings.sources}
        for name in native_bad:
            domain = by_name.get(name, {}).get("domain", "")
            if not domain:
                continue
            found = [r for r in discovery.probe_feeds(domain) if r["ok"]]
            if found:
                best = sorted(found, key=lambda r: (-r["in_window"], -r["entries"]))[:4]
                report.append(f"- **{name}** ({domain}): " + "; ".join(f"`{r['url']}` [{r['user_agent']} UA, {r['entries']} entries, {r['in_window']} in window]" for r in best))
            else:
                report.append(f"- **{name}** ({domain}): no feed found by autodiscovery or common paths")
    report += ["", "## Image providers", ""]
    allowed = list(settings.get("images.allowed_licenses", []))
    for name, url_fn, parser in (("Wikimedia Commons", images.commons_search_url, images.parse_commons), ("Openverse", images.openverse_search_url, images.parse_openverse)):
        try:
            data = images.fetch_json(url_fn("Kathmandu Durbar Square", 5))
            found = parser(data, allowed)
            report.append(f"- {name}: reachable, {len(found)} usable candidates for a test query.")
        except Exception as exc:  # noqa: BLE001
            report.append(f"- {name}: FAILED ({type(exc).__name__}: {str(exc)[:120]})")
    text = "\n".join(report)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


def cmd_auth_check(args) -> int:
    """Prove the model credentials work without spending tokens: Anthropic, then OpenAI for images."""
    from .llm import auth_mode, build_client, scrub_empty_credentials

    settings = _settings(args)
    scrub_empty_credentials()
    mode = auth_mode()
    model = settings.role_model("writer")
    print(f"Anthropic credential source: {mode}")
    failed = False
    try:
        client = build_client(timeout=60.0, max_retries=1)
        info = client.models.retrieve(model)
        print(f"Anthropic ok: authenticated and found model {getattr(info, 'id', model)}")
    except Exception as exc:  # noqa: BLE001 - report every failure the same way
        print(f"Anthropic FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        failed = True
    status, note = images.check_openai_key(settings)
    print(f"OpenAI {status}: {note}")
    failed = failed or status == "failed"
    return 1 if failed else 0


def cmd_batch_probe(args) -> int:
    """Time the Message Batches queue with two tiny requests per round, before batch mode goes on.

    One request has the shape most calls have (Sonnet, a JSON schema, the web search tool), the
    other the judges' shape (Opus, a JSON schema, no tools). Each is its own batch of one, as in
    an edition. A few cents in all.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .llm import WEB_SEARCH_TOOL_TYPE, BatchLane, auth_mode, build_client, scrub_empty_credentials, strict_schema, usage_cost
    from .models import UsageRecord

    settings = _settings(args)
    scrub_empty_credentials()
    print(f"Anthropic credential source: {auth_mode()}")
    client = build_client(timeout=120.0, max_retries=2)

    def fmt(props: dict) -> dict:
        return {"type": "json_schema", "schema": strict_schema({"type": "object", "properties": props})}

    def system(text: str) -> list[dict]:
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # The edition's own output cap and cache marker, so the probe proves the queue takes them.
    cap = int(settings.get("llm.max_tokens", 64000))

    shapes = {
        "search": {
            "model": settings.role_model("writer"),
            "max_tokens": cap,
            "system": system("You check facts for a Nepali newsroom. Answer with one JSON object."),
            "messages": [{"role": "user", "content": "Search the web once for today's front page of the Kathmandu Post and give its top headline and the page you read it on."}],
            "output_config": {"effort": "low", "format": fmt({"headline": {"type": "string"}, "url": {"type": "string"}})},
            "tools": [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 1}],
        },
        "judge": {
            "model": settings.role_model("validation_judge"),
            "max_tokens": cap,
            "system": system("You are a news judge. Answer with one JSON object."),
            "messages": [{"role": "user", "content": "Reply with the word ready."}],
            "output_config": {"effort": "low", "format": fmt({"answer": {"type": "string"}})},
        },
    }

    def one(round_no: int, shape: str) -> dict:
        lane = BatchLane(client, max_wait=60 * args.max_wait_minutes, run_seconds=float("inf"), poll=10.0)
        msg = lane.send(f"probe-{shape}", shapes[shape])
        wait = lane.waits[-1] if lane.waits else {}
        row = {"round": round_no, "shape": shape, "model": shapes[shape]["model"], "seconds": wait.get("seconds", 0)}
        if msg is None:
            return {**row, "result": f"no answer: {lane.off_reason or wait.get('outcome') or 'unknown'}"}
        usage = msg.usage
        stu = getattr(usage, "server_tool_use", None)
        rec = UsageRecord(
            role=shape, model=getattr(msg, "model", row["model"]) or row["model"], batch=True,
            input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            web_search_requests=int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
        )
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
        try:
            json.loads(text)
            result = f"answered ({msg.stop_reason}), valid JSON"
        except ValueError:
            result = f"answered ({msg.stop_reason}), not JSON"
        return {**row, "result": result, "record": rec}

    rows = []
    with ThreadPoolExecutor(max_workers=len(shapes)) as pool:
        for round_no in range(1, args.rounds + 1):
            rows += list(pool.map(lambda s: one(round_no, s), shapes))
    print("\n| Round | Shape | Model | Seconds in the queue | Result | Searches |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        rec = r.get("record")
        print(f"| {r['round']} | {r['shape']} | {r['model']} | {r['seconds']} | {r['result']} | {rec.web_search_requests if rec else ''} |")
    answered = [r for r in rows if r.get("record") and "valid JSON" in r["result"]]
    waits = sorted(r["seconds"] for r in rows)
    cost, _ = usage_cost(settings, [r["record"] for r in rows if r.get("record")])
    print(f"\n{len(answered)} of {len(rows)} batch requests answered with valid JSON. Seconds in the queue: shortest {waits[0]}, median {waits[len(waits) // 2]}, longest {waits[-1]}.")
    print(f"Cost at batch prices: ${cost:.3f}")
    searched = any(r.get("record") and r["shape"] == "search" and r["record"].web_search_requests > 0 for r in rows)
    print("Web search inside a batch request: " + ("works" if searched else "not seen"))
    return 0 if len(answered) == len(rows) and searched else 1


def cmd_nepali_trial(args) -> int:
    """Fix stored stories' Nepali one or both ways, from one draft and one first reading, and compare. Saves nothing."""
    from concurrent.futures import ThreadPoolExecutor

    from . import nepali
    from .llm import UsageMeter, make_llm, scrub_empty_credentials, usage_cost

    settings = _settings(args)
    if not settings.mock:
        scrub_empty_credentials()
    articles = publish.load_articles(settings)
    wanted = [a for a in articles if a.id in set(args.article or [])] if args.article else articles[: args.limit]
    if not wanted:
        print("No stored story matches.")
        return 1
    ways = nepali.WAYS if args.ways == "both" else (args.ways,)
    labels = {"rewrite": "Rewrite", "in_place": "In place"}
    print(f"## Nepali fix trial: {' against '.join(labels[w].lower() for w in ways)}, {len(wanted)} stor{'y' if len(wanted) == 1 else 'ies'}\n")
    print("One draft and one first reading per story. Then each way fixes, reads again and fixes again, as two readings do in an edition. A closing reading counts what each finished piece still gets wrong, which is what an edition would publish unread.\n")

    def one(article):
        # Each story gets its own client and meter, so each way's cost is its own.
        return nepali.trial(make_llm(settings, UsageMeter(20)), settings, article, ways=ways)


    with ThreadPoolExecutor(max_workers=max(1, int(settings.get("pipeline.concurrency", 1) or 1))) as pool:
        results = list(pool.map(one, wanted))
    heads = [f"{labels[w]}: second, closing" + (", placed word for word" if w == "in_place" else "") for w in ways]
    print("| Story | First reading | " + " | ".join(heads) + " |")
    print("|---|---|" + "---|" * len(ways))
    for r in results:
        cells = [f"{len(r[w]['second_reading'])}, {len(r[w]['closing_reading'])}" + (f", {r[w]['placed']}" if w == "in_place" else "") for w in ways]
        print(f"| {r['id']} | {len(r['first_reading'])} | " + " | ".join(cells) + " |")
    costs = [f"{labels[w].lower()} ${usage_cost(settings, [x for r in results for x in r[w]['records']])[0]:.2f}" for w in ways]
    print(f"\nFixing cost after the shared draft and first reading, closing reading included: {', '.join(costs)}.")
    for r in results:
        print(f"\n### {r['id']}")
        for way in ways:
            label = labels[way]
            piece = r[way]["piece"]
            cap = piece.get("caption") or {}
            print(f"\n**{label}: the closing reading finds {len(r[way]['closing_reading'])}**")
            for p in r[way]["closing_reading"]:
                print(f"- {p.get('problem', '').strip()} (\"{p.get('passage', '').strip()[:120]}\")")
            print(f"\n{label}, the card headline and the Facebook caption:\n")
            for line in (piece.get("image_headline", ""), *(cap.get(k, "") for k in ("hook", "angle", "trigger", "synopsis"))):
                if line.strip():
                    print("> " + line.strip().replace("\n", "\n> ") + "\n>")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        slim = [{**r, **{w: {k: v for k, v in r[w].items() if k != "records"} for w in ways}} for r in results]
        (out / "nepali-trial.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_nepali_rejudge(args) -> int:
    """Read the finished pieces of earlier Nepali trials again with today's editor. Saves nothing.

    A trial's closing reading is done by the editor of its own day, so two trials run on
    different prompts were judged by different readers. This reads each trial's finished pieces
    again with one editor, each piece `--readings` times, so the trials can be compared.
    """
    import copy
    from concurrent.futures import ThreadPoolExecutor

    from . import nepali
    from .llm import UsageMeter, make_llm, scrub_empty_credentials, usage_cost

    settings = _settings(args)
    if not settings.mock:
        scrub_empty_credentials()
    # A handful of calls: the normal way, not the batch queue, which can hold one call 15 minutes.
    raw = copy.deepcopy(settings.raw)
    raw.setdefault("llm", {}).setdefault("batch", {})["enabled"] = False
    settings = dataclasses.replace(settings, raw=raw)
    articles = {a.id: a for a in publish.load_articles(settings)}
    runs = []
    for path in args.pieces:
        label = Path(path).parent.name or Path(path).stem
        runs.append((label, json.loads(Path(path).read_text(encoding="utf-8"))))
    jobs = []
    for label, results in runs:
        for r in results:
            piece = (r.get(args.way) or {}).get("piece")
            if r.get("id") not in articles or not piece:
                print(f"Skipped {label} {r.get('id')}: no stored story or no {args.way} piece.")
                continue
            jobs += [(label, r["id"], n, piece) for n in range(args.readings)]
    if not jobs:
        print("Nothing to read.")
        return 1
    llm = make_llm(settings, UsageMeter(len(jobs) + 5))

    def one(job):
        label, story, n, piece = job
        return label, story, n, nepali.read_again(llm, settings, articles[story], piece)

    with ThreadPoolExecutor(max_workers=max(1, int(settings.get("pipeline.concurrency", 1) or 1))) as pool:
        found = list(pool.map(one, jobs))
    labels = [label for label, _ in runs]
    stories = list(dict.fromkeys(story for _, story, _, _ in found))
    counts = {(label, story): [] for label in labels for story in stories}
    for label, story, n, problems in found:
        counts[(label, story)].append(len(problems))
    print(f"## Nepali trials read again by one editor: {', '.join(labels)}, the {args.way} pieces, {args.readings} reading{'s' if args.readings != 1 else ''} each\n")
    print("| Story | " + " | ".join(labels) + " |")
    print("|---|" + "---|" * len(labels))
    for story in stories:
        print(f"| {story} | " + " | ".join(", ".join(str(c) for c in counts[(label, story)]) or "not read" for label in labels) + " |")
    print(f"\nCost: ${usage_cost(settings, llm.meter.records)[0]:.2f}.")
    for label, story, n, problems in found:
        print(f"\n### {label}: {story}, reading {n + 1} finds {len(problems)}")
        for p in problems:
            print(f"- {p.get('problem', '').strip()} (\"{p.get('passage', '').strip()[:120]}\")")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        rows = [{"trial": label, "id": story, "reading": n + 1, "problems": problems} for label, story, n, problems in found]
        (out / "nepali-rejudge.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_social(args) -> int:
    from . import social

    settings = _settings(args)
    networks = [n.strip() for n in args.networks.split(",") if n.strip()] if args.networks else None
    if getattr(args, "again", False) and not args.article:
        print("--again needs --article <id>: it posts a story again even though the record says it went out.")
        return 2
    if getattr(args, "replace", False) and not args.article:
        print("--replace needs --article <id>: it takes that story's Facebook post down and posts the corrected one.")
        return 2
    if getattr(args, "edit", False) and not args.article:
        print("--edit needs --article <id>: it rewrites the caption of that story's live Facebook post in place.")
        return 2
    if sum(bool(getattr(args, k, False)) for k in ("again", "replace", "edit")) > 1:
        print("Choose one of --again, --replace and --edit.")
        return 2
    configured = social.configured_networks(settings, os.environ)
    if networks is None and not configured:
        paused = sorted(social.paused_networks(settings))
        if paused:
            print(f"Nothing to post to. Paused in config/settings.yaml: {', '.join(paused)}. Other networks have no secrets yet.")
        else:
            print("No social accounts connected. Add the secrets listed in the README under \"Social media\" to switch a network on.")
        return 0
    records = social.post_articles(
        settings,
        os.environ,
        run_date=args.run_date,
        max_age_hours=args.max_age_hours,
        networks=networks,
        dry_run=args.dry_run,
        wait_seconds=args.wait,
        article_ids=[a.strip() for a in (args.article or []) if a.strip()] or None,
        again=bool(getattr(args, "again", False)),
        replace=bool(getattr(args, "replace", False)),
        edit=bool(getattr(args, "edit", False)),
    )
    if not records:
        print(f"Nothing to post. Networks connected: {', '.join(configured) or 'none'}.")
        return 0
    print("## Social posts" + (" (dry run)" if args.dry_run else ""))
    print()
    print("| Article | Network | Result | Link |")
    print("|---|---|---|---|")
    failed = 0
    for rec in records:
        for post in rec.posts:
            if post.status == "failed":
                failed += 1
            detail = post.url or post.id or post.error.replace("|", "/")[:120]
            if post.scheduled_for:
                detail = f"scheduled for {post.scheduled_for} · {detail}"
            if post.edited_at:
                detail = f"caption edited {post.edited_at} · {detail}"
            print(f"| {rec.article_id} | {post.network} | {post.status} | {detail} |")
    if args.dry_run:
        for rec in records:
            for post in rec.posts:
                print(f"\n--- {post.network} · {rec.article_id} ---\n{post.text}")
    return 1 if failed else 0


def cmd_social_check(args) -> int:
    from . import social

    settings = _settings(args)
    rows = social.check_networks(settings, os.environ)
    print("## Social media accounts")
    print()
    print("| Network | Configured | Works | Account | Note |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['network']} | {r['configured']} | {r['ok']} | {r['account']} | {r['note'].replace('|', '/')} |")
    broken = [r for r in rows if r["configured"] == "yes" and r["ok"] != "yes"]
    print()
    if broken:
        print(f"{len(broken)} connected network(s) failed the check: {', '.join(r['network'] for r in broken)}.")
        return 1
    connected = [r for r in rows if r["ok"] == "yes"]
    print(f"{len(connected)} network(s) ready: {', '.join(r['network'] for r in connected) or 'none'}.")
    return 0


def cmd_insights(args) -> int:
    """Read what each Facebook post did at 24 and 72 hours, note the followers, print the week."""
    from . import insights, social

    settings = _settings(args)
    # Secrets, not the posting list: a paused network's posts are still read.
    if not all(os.environ.get(k, "").strip() for k in social.ENV_KEYS["facebook"]):
        print("Facebook is not connected, so there is nothing to read. Add FACEBOOK_PAGE_ID and FACEBOOK_PAGE_TOKEN (README, \"Social media\").")
        return 0
    if args.probe:
        print("## What the Facebook token can read")
        print()
        try:
            print("\n".join(insights.probe(settings, os.environ)))
        except social.SocialError as exc:
            print(f"::warning::Facebook refused the token: {str(exc)[:200]}")
            return 1
        return 0
    print("## Post readings")
    print()
    try:
        result = insights.take_readings(settings, os.environ)
    except social.SocialError as exc:
        print(f"::warning::Facebook refused the token, so nothing was read: {str(exc)[:200]}")
        return 1
    for article_id, hour, values, errors in result["read"]:
        got = ", ".join(f"{k} {v:,}" for k, v in values.items())
        missing = f"; not read: {', '.join(f'{k} ({v})' for k, v in errors.items())}" if errors else ""
        print(f"- {article_id}, {hour} hours: {got}{missing}")
    for article_id, hour, errors in result["failed"]:
        print(f"- {article_id}, {hour} hours: nothing came back ({', '.join(sorted(set(errors.values())))}); the next run tries again")
    if not result["read"] and not result["failed"]:
        print("- No post was due for a reading.")
    if result["followers"] is not None:
        print(f"- Page followers: {result['followers']:,}")
    elif result["followers_error"]:
        print(f"- Page followers not read ({result['followers_error']})")
    if result["stopped"]:
        print(f"\n::warning::Stopped early: {result['stopped']}. Replace FACEBOOK_PAGE_TOKEN with a Page token that also grants read_insights and pages_read_engagement (README, \"Measuring what posts do\").")
    print(f"\n## The last {args.days} days\n")
    print("\n".join(insights.report(settings, days=args.days)))
    return 0


def cmd_comments(args) -> int:
    """Answer readers under the Facebook posts still inside their reply window; list what needs a person."""
    import copy

    from . import comments, social
    from .llm import UsageMeter, make_llm, scrub_empty_credentials

    settings = _settings(args)
    if not all(os.environ.get(k, "").strip() for k in social.ENV_KEYS["facebook"]):
        print("Facebook is not connected, so there are no comments to read.")
        return 0
    if not settings.mock:
        scrub_empty_credentials()
    # Replies belong in the first hour: the normal way, never the batch queue.
    raw = copy.deepcopy(settings.raw)
    raw.setdefault("llm", {}).setdefault("batch", {})["enabled"] = False
    settings = dataclasses.replace(settings, raw=raw)
    llm = make_llm(settings, UsageMeter(int(settings.get("social.facebook.comments.max_calls", 6))))
    try:
        result = comments.run_desk(settings, os.environ, llm, dry_run=args.dry_run)
    except social.SocialError as exc:
        print(f"::warning::Facebook refused the token, so no comment was read: {str(exc)[:200]}")
        return 1
    print("\n".join(comments.summary(result, dry_run=args.dry_run)))
    return 0


def cmd_nepali(args) -> int:
    """Write the Nepali edition of stored stories. By default only the ones without one."""
    from . import nepali
    from .llm import UsageMeter, auth_mode, make_llm, scrub_empty_credentials

    settings = _settings(args)
    if not settings.mock:
        scrub_empty_credentials()
        logging.info("model access via %s", auth_mode())
    wanted = nepali.wanting(settings, only=[a.strip() for a in (args.article or []) if a.strip()] or None, everything=args.all, limit=args.limit or 0)
    if not wanted:
        print("Nothing to write: every stored story already has a Nepali edition.")
        return 0
    rounds = int(settings.get("pipeline.nepali_rounds", 2) or 1)
    print(f"Writing {len(wanted)} stor{'y' if len(wanted) == 1 else 'ies'} in Nepali: a writer call, then up to {rounds} editor reading{'s' if rounds != 1 else ''} with a fix after each.")
    # Writer, then editor and fix per round, at most. Stories are written a few at a time.
    workers = int(settings.get("pipeline.concurrency", 1) or 1)
    llm = make_llm(settings, UsageMeter((1 + 2 * rounds) * len(wanted)))
    results = nepali.backfill(settings, llm, wanted, workers=workers)
    for article_id, status, detail in results:
        print(f"{status:10} {article_id}  {detail}")
    failed = sum(1 for _, status, _ in results if status == "failed")
    skipped = len(wanted) - len(results)
    print(f"\n{len(results) - failed} written, {failed} failed, {skipped} not attempted.")
    # The daily run record carries its own usage; a backfill has no record, so it says what it used here.
    from .models import RunLog

    usage = RunLog(run_date="", usage=list(llm.meter.records)).usage_totals()
    print("Model usage: " + ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in usage.items()))
    return 1 if failed or skipped else 0


def _photo_targets(settings, args):
    """The stored stories to look at: named ones, every one, or by default the ones without a real photo."""
    articles = publish.load_articles(settings)
    only = [a.strip() for a in (args.article or []) if a.strip()]
    if only:
        wanted = [a for a in articles if a.id in only]
        for missing in sorted(set(only) - {a.id for a in wanted}):
            print(f"No stored story with id {missing}.")
    elif args.all:
        wanted = articles
    else:
        wanted = [a for a in articles if not a.image or a.image.credit.kind != "found"]
    return wanted[: args.limit] if args.limit else wanted


def _old_way_count(settings, queries: list[str]) -> int:
    """What the desk found before it learned to look: one full text search per query and library."""
    allowed = list(settings.get("images.allowed_licenses", []))
    seen: set[str] = set()
    for query in queries[:4]:
        for url_fn, parser in ((images.commons_search_url, images.parse_commons), (images.openverse_search_url, images.parse_openverse)):
            try:
                seen.update(c.url for c in parser(images.fetch_json(url_fn(query, 6)), allowed))
            except Exception:  # noqa: BLE001
                continue
    return len(seen)


def _photos_dry_run(settings, wanted, compare: bool) -> int:
    """Search the libraries for each story and print what the desk would show the model. No model calls, no changes."""
    allowed = list(settings.get("images.allowed_licenses", []))
    report = ["## Picture desk dry run", ""]
    thumbs_checked = 0
    for art in wanted:
        queries = [q for q in (art.image_brief or {}).get("search_queries") or [] if str(q).strip()] or [art.headline]
        stats: dict = {}
        cands, trail = images.find_candidates(
            queries,
            allowed,
            images.fetch_json,
            country=(art.country or "Nepal").title(),
            exclude=images.recent_photo_keys(settings, art.id, int(settings.get("images.rotation_days", 30))),
            max_requests=int(settings.get("images.max_search_requests", 48)),
            max_upscale=float(settings.get("images.max_upscale", 2.2)),
            stats=stats,
        )
        now = art.image.credit.kind if art.image else "none"
        report.append(f"### {art.id} (carries: {now})")
        report.append("")
        for step in trail:
            found = ", ".join(f"{via} {n}" for via, n in step["found"].items()) or "nothing"
            entity = step["entity"] or "no Wikidata item"
            errors = f" Errors: {'; '.join(step['errors'])}." if step.get("errors") else ""
            report.append(f"- \"{step['subject']}\": {entity}. Found: {found}.{errors}")
        line = f"- **{len(cands)} candidates** from {stats.get('requests', 0)} library requests."
        if compare:
            line += f" The old one search per query way found {_old_way_count(settings, queries)}."
        report.append(line)
        for i, c in enumerate(cands[:8], 1):
            size = f"{c.width}x{c.height}" if c.width and c.height else "size unknown"
            depicts = f" Depicts: {c.depicts}." if c.depicts else ""
            report.append(f"  {i}. [{c.found_via}] {c.file_name or c.title} | {c.license} | {size} | {c.page_url}{depicts}")
        if cands and thumbs_checked < 2:
            thumbs_checked += 1
            try:
                data, ctype = images.fetch_bytes(cands[0].thumb_url)
                report.append(f"- First picture download: {ctype or 'no type'}, {len(data) // 1024} KB.")
            except Exception as exc:  # noqa: BLE001
                report.append(f"- First picture download FAILED: {type(exc).__name__}: {str(exc)[:160]}")
        report.append("")
    text = "\n".join(report)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


def cmd_photos(args) -> int:
    """Look again for a licensed real photo for stored stories. By default the ones carrying an illustration or a cover card."""
    import copy

    settings = _settings(args)
    # One call after another, the normal way. On 29 September a one story run's only picker call
    # waited 935 seconds in the batch queue, then went at full price anyway.
    raw = copy.deepcopy(settings.raw)
    raw.setdefault("llm", {}).setdefault("batch", {})["enabled"] = False
    settings = dataclasses.replace(settings, raw=raw)
    wanted = _photo_targets(settings, args)
    if not wanted:
        print("Nothing to do: every stored story carries a real photo.")
        return 0
    if args.dry_run:
        return _photos_dry_run(settings, wanted, args.compare)

    from .llm import BudgetExceeded, UsageMeter, auth_mode, make_llm, scrub_empty_credentials
    from .models import RunLog

    if not settings.mock:
        scrub_empty_credentials()
        logging.info("model access via %s", auth_mode())
    rounds = max(1, int(settings.get("images.picker_rounds", 2)))
    print(f"Looking for a real photo for {len(wanted)} stor{'y' if len(wanted) == 1 else 'ies'}: up to {rounds} picture editor round{'s' if rounds != 1 else ''} each.")
    llm = make_llm(settings, UsageMeter(rounds * len(wanted)))
    found = kept = failed = 0
    for art in wanted:
        try:
            asset, record = images.find_real_photo(llm, settings, art, images.fetch_json, images.fetch_bytes)
        except BudgetExceeded as exc:
            print(f"stopped    {art.id}  {exc}")
            failed += 1
            break
        except Exception as exc:  # noqa: BLE001 - one story's failure must not stop the rest
            print(f"failed     {art.id}  {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if asset is not None:
            old = art.image
            art.image = asset
            art.review.picture = record
            publish.save_article(settings, art)
            if old and old.path != asset.path:
                (settings.root / old.path).unlink(missing_ok=True)
            print(f"photo      {art.id}  {asset.credit.line()}")
            found += 1
        else:
            record["decision"] = f"kept the {art.image.credit.kind.replace('_', ' ')}" if art.image else "no picture"
            art.review.picture = record
            publish.save_article(settings, art)
            print(f"kept       {art.id}  {record['reason']}")
            kept += 1
    print(f"\n{found} given a real photo, {kept} kept what they had, {failed} failed.")
    usage = RunLog(run_date="", usage=list(llm.meter.records)).usage_totals()
    print("Model usage: " + ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in usage.items()))
    return 1 if failed else 0


def cmd_build(args) -> int:
    settings = _settings(args)
    out = publish.build_site(settings, Path(args.out))
    if args.root_rss:
        publish.copy_root_rss(settings, out)
    print(f"built {out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="newsroom", description="Adversarial, judged Nepal news pipeline")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the full daily pipeline")
    p_run.add_argument("--mock", action="store_true", help="no network, no keys, deterministic outputs")
    p_run.add_argument("--fixtures", help="directory of fixture feeds instead of live fetching")
    p_run.add_argument("--build", action="store_true", help="build the site after the run")
    p_run.add_argument("--out", default="site")
    p_run.add_argument("--root-rss", action="store_true", help="also copy rss.xml to the repository root")
    p_run.add_argument("--articles", type=int, help="publish at most this many stories this run, instead of pipeline.articles_per_day")
    p_run.set_defaults(func=cmd_run)

    p_dis = sub.add_parser("discover", help="only scan the feeds and print what was found")
    p_dis.add_argument("--mock", action="store_true")
    p_dis.add_argument("--fixtures")
    p_dis.add_argument("--json", action="store_true")
    p_dis.set_defaults(func=cmd_discover)

    p_auth = sub.add_parser("auth-check", help="verify the Anthropic credentials (API key or identity federation) without spending tokens")
    p_auth.set_defaults(func=cmd_auth_check)

    p_bp = sub.add_parser("batch-probe", help="time the Message Batches queue with a few tiny requests (a few cents) before batch mode is switched on")
    p_bp.add_argument("--rounds", type=int, default=2, help="pairs of requests, one round after the other")
    p_bp.add_argument("--max-wait-minutes", type=float, default=30, help="give up on a request after this long in the queue")
    p_bp.set_defaults(func=cmd_batch_probe)

    p_chk = sub.add_parser("check-sources", help="probe every live feed and image provider, print a health report")
    p_chk.add_argument("--no-probe", action="store_true", help="skip feed autodiscovery for failing sources")
    p_chk.set_defaults(func=cmd_check_sources)

    p_soc = sub.add_parser("social", help="post the latest edition's articles to every connected social network")
    p_soc.add_argument("--run-date", help="post the articles of this run (YYYY-MM-DD in newsroom time); default latest")
    p_soc.add_argument("--max-age-hours", type=float, help="only articles published within this many hours")
    p_soc.add_argument("--networks", help="comma separated subset, e.g. x,telegram")
    p_soc.add_argument("--wait", type=float, help="seconds to wait for the article page to go live first")
    p_soc.add_argument("--article", action="append", help="post this article id regardless of age; repeatable")
    p_soc.add_argument("--dry-run", action="store_true", help="compose the posts and print them, post nothing")
    p_soc.add_argument("--again", action="store_true", help="post the given --article again even though its record says it went out")
    p_soc.add_argument("--replace", action="store_true", help="take the given --article's Facebook post down and post the corrected one; nothing goes out if the takedown fails")
    p_soc.add_argument("--edit", action="store_true", help="rewrite the caption of the given --article's live Facebook post in place; it keeps its reactions, comments and shares")
    p_soc.set_defaults(func=cmd_social)

    p_ins = sub.add_parser("insights", help="read what each Facebook post did at 24 and 72 hours, note the Page's followers, print the week against the judges' forecasts")
    p_ins.add_argument("--days", type=int, default=7, help="how many days of posts the report covers (default 7)")
    p_ins.add_argument("--probe", action="store_true", help="read nothing; say what the token may read: its permissions, and how Meta answers each view metric for the newest post")
    p_ins.set_defaults(func=cmd_insights)

    p_com = sub.add_parser("comments", help="answer readers under Facebook posts still inside their reply window; corrections and legal complaints are left for a person")
    p_com.add_argument("--dry-run", action="store_true", help="sort the comments and draft the replies, post and save nothing")
    p_com.set_defaults(func=cmd_comments)

    p_socchk = sub.add_parser("social-check", help="verify every connected social account without posting")
    p_socchk.set_defaults(func=cmd_social_check)

    p_ne = sub.add_parser("nepali", help="write the Nepali edition of stored stories (the ones without one, by default)")
    p_ne.add_argument("--mock", action="store_true", help="no network, no keys, deterministic outputs")
    p_ne.add_argument("--missing", action="store_true", help="only stories without a Nepali edition (the default)")
    p_ne.add_argument("--all", action="store_true", help="write every stored story again, replacing what it has")
    p_ne.add_argument("--article", action="append", help="write this article id; repeatable")
    p_ne.add_argument("--limit", type=int, help="stop after this many stories")
    p_ne.set_defaults(func=cmd_nepali)

    p_nt = sub.add_parser("nepali-trial", help="fix stored stories' Nepali one or both ways, rewrite and in place, from one draft and one first reading, and compare; saves nothing")
    p_nt.add_argument("--article", action="append", help="a stored story id; repeat for more")
    p_nt.add_argument("--limit", type=int, default=3, help="without --article, the newest this many stories")
    p_nt.add_argument("--out", help="folder for nepali-trial.json, with both finished pieces for every story")
    p_nt.add_argument("--ways", choices=["both", "rewrite", "in_place"], default="both", help="which ways to fix each draft; one way makes up to 6 calls a story instead of 10")
    p_nt.add_argument("--mock", action="store_true", help="no network, no keys, deterministic outputs")
    p_nt.set_defaults(func=cmd_nepali_trial)

    p_nr = sub.add_parser("nepali-rejudge", help="read the finished pieces of earlier Nepali trials again with today's editor, so trials meet one reader; saves nothing")
    p_nr.add_argument("--pieces", action="append", required=True, help="a trial's nepali-trial.json; repeat for each trial")
    p_nr.add_argument("--way", choices=["rewrite", "in_place"], default="rewrite", help="which finished piece to read")
    p_nr.add_argument("--readings", type=int, default=2, choices=[1, 2, 3], help="readings per piece")
    p_nr.add_argument("--out", help="folder for nepali-rejudge.json")
    p_nr.add_argument("--mock", action="store_true", help="no network, no keys, deterministic outputs")
    p_nr.set_defaults(func=cmd_nepali_rejudge)

    p_ph = sub.add_parser("photos", help="look again for a licensed real photo for stored stories (by default the ones with an illustration or a cover card)")
    p_ph.add_argument("--all", action="store_true", help="every stored story, photos included")
    p_ph.add_argument("--article", action="append", help="this article id; repeatable")
    p_ph.add_argument("--limit", type=int, help="stop after this many stories")
    p_ph.add_argument("--dry-run", action="store_true", help="search the libraries and print what the model would see; no model calls, nothing changed")
    p_ph.add_argument("--compare", action="store_true", help="with --dry-run, also count what the old one search per query way finds")
    p_ph.set_defaults(func=cmd_photos)

    p_build = sub.add_parser("build", help="build the static site from data/")
    p_build.add_argument("--out", default="site")
    p_build.add_argument("--root-rss", action="store_true")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
