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


def cmd_run(args) -> int:
    from .pipeline import run

    settings = _settings(args)
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
    """Prove the model credentials work without spending tokens."""
    from .llm import auth_mode, build_client, scrub_empty_credentials

    settings = _settings(args)
    scrub_empty_credentials()
    mode = auth_mode()
    model = settings.role_model("writer")
    print(f"credential source: {mode}")
    try:
        client = build_client(timeout=60.0, max_retries=1)
        info = client.models.retrieve(model)
    except Exception as exc:  # noqa: BLE001 - report every failure the same way
        print(f"FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        return 1
    print(f"ok: authenticated and found model {getattr(info, 'id', model)}")
    return 0


def cmd_social(args) -> int:
    from . import social

    settings = _settings(args)
    networks = [n.strip() for n in args.networks.split(",") if n.strip()] if args.networks else None
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
    p_run.set_defaults(func=cmd_run)

    p_dis = sub.add_parser("discover", help="only scan the feeds and print what was found")
    p_dis.add_argument("--mock", action="store_true")
    p_dis.add_argument("--fixtures")
    p_dis.add_argument("--json", action="store_true")
    p_dis.set_defaults(func=cmd_discover)

    p_auth = sub.add_parser("auth-check", help="verify the Anthropic credentials (API key or identity federation) without spending tokens")
    p_auth.set_defaults(func=cmd_auth_check)

    p_chk = sub.add_parser("check-sources", help="probe every live feed and image provider, print a health report")
    p_chk.add_argument("--no-probe", action="store_true", help="skip feed autodiscovery for failing sources")
    p_chk.set_defaults(func=cmd_check_sources)

    p_soc = sub.add_parser("social", help="post the latest edition's articles to every connected social network")
    p_soc.add_argument("--run-date", help="post the articles of this run (YYYY-MM-DD in newsroom time); default latest")
    p_soc.add_argument("--max-age-hours", type=float, help="only articles published within this many hours")
    p_soc.add_argument("--networks", help="comma separated subset, e.g. x,telegram")
    p_soc.add_argument("--wait", type=float, help="seconds to wait for the article page to go live first")
    p_soc.add_argument("--dry-run", action="store_true", help="compose the posts and print them, post nothing")
    p_soc.set_defaults(func=cmd_social)

    p_socchk = sub.add_parser("social-check", help="verify every connected social account without posting")
    p_socchk.set_defaults(func=cmd_social_check)

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
