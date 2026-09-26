# Nepal Wire

An automated newsroom for Nepal. Once a day it reads every major Nepali and English outlet, argues over what matters, writes the top stories, checks them against their sources, and publishes only what four judges approve. The site and the RSS feed live on GitHub Pages. The complete argument behind every story is kept in this repository.

## How an edition is made

```
 feeds + Google News (last 24 h)
          │
          ▼
   ┌─────────────┐
   │  Discovery  │  dedupe, language tag, cap per source
   └──────┬──────┘
          ▼
   ┌─────────────┐
   │  Clusterer  │  headlines → distinct stories
   └──────┬──────┘
          ▼
   ┌───────────────────────────┐   each story, in parallel
   │ Advocate  ⇄  Skeptic      │   both research with web search
   └──────┬────────────────────┘
          ▼
   Ranking judge 1 ──▶ Ranking judge 2 (final list)
          │
          ▼  top N stories
   ┌─────────────┐   ┌──────────────────────────────────────┐
   │   Writer    │──▶│ Picture desk: Commons/Openverse photo │
   └──────┬──────┘   │ checked by vision, else AI image,     │
          │          │ else cover card. Credits always.      │
          ▼          └──────────────────────────────────────┘
   ┌───────────────────────────┐
   │ Red team  ⇄  Defence      │   accuracy · relevance · defensibility · virality
   └──────┬────────────────────┘
          ▼
   Validation judge 1 ──(revise ≤ N times)──▶ Validation judge 2
          │
          ▼  approved by both
   data/articles/*.json ──▶ static site + rss.xml ──▶ GitHub Pages
```

Every model call is one request to the Claude API asking for a JSON object that matches a schema. Research roles get the server side web search tool. Every call's token usage is stored in the run log.

## Setup

1. **Give the workflow model credentials** (one of the two options under *Model credentials* below).
2. **Optional**: add an `OPENAI_API_KEY` secret to turn on AI illustrations when no licensed photo fits. Without it the cover card is used.
3. **Run it once by hand**: *Actions → Daily edition → Run workflow*. Tick *mock* first if you want a dry run that touches nothing. The workflow creates the GitHub Pages site on first deploy.
4. From then on the edition publishes every day at 06:15 Nepal time. Change the cron in `.github/workflows/daily.yml` if you want another hour.

## Model credentials

The workflow accepts either an API key or Anthropic's identity federation. If both are configured the key wins. *Actions → Check model credentials* verifies whichever you set up without spending tokens.

**Option A, API key.** Create a key in the Claude Console under *Settings → API keys* and store it as the repository secret `ANTHROPIC_API_KEY`.

**Option B, identity federation (no key to store).** GitHub Actions proves its identity to Anthropic with a short lived token on every run.
1. In the Claude Console open *Settings → Workload identity*, click *Connect workload* and choose the *GitHub Actions* tile.
2. Fill in the rule: repository `inquisitive013/nepal-news-rss`, branch `main` (subject `repo:inquisitive013/nepal-news-rss:ref:refs/heads/main`), audience `https://api.anthropic.com`, scope `workspace:inference` (or the default `workspace:developer`), token lifetime `3600`.
3. Copy the IDs the wizard shows and store them as repository **variables** (not secrets, they are not sensitive) under *Settings → Secrets and variables → Actions → Variables*: `ANTHROPIC_FEDERATION_RULE_ID` (`fdrl_...`), `ANTHROPIC_ORGANIZATION_ID` (the UUID from *Settings → Organization*), `ANTHROPIC_SERVICE_ACCOUNT_ID` (`svac_...`). Add `ANTHROPIC_WORKSPACE_ID` only if the rule covers more than one workspace.
4. Run *Check model credentials*. The wizard waits fifteen minutes for a first successful exchange, so run it soon after finishing the wizard.

The pipeline mints a fresh GitHub identity token for each exchange, which is what Anthropic requires: those tokens expire after about five minutes and each one is accepted once.

Optional: a repository variable `SITE_URL` if the site lives on a custom domain. Otherwise `https://<owner>.github.io/<repo>` is assumed for absolute links in the feed and social cards.

## What ends up where

| Path | What |
|---|---|
| `data/articles/<date>-<slug>.json` | Every published article with body, sources, image credit and the full review record |
| `data/rejected/` | Articles that failed validation, kept for audit |
| `data/runs/<date>.json` | The whole day: feed health, candidates, stories, debates, verdicts, token usage |
| `data/images/` | Stored images, downscaled, with a credit bar burned in |
| `rss.xml` | Copy of the live feed at the repo root, for automations that read the raw file |
| `site/` | Built site, not committed, deployed to Pages by the workflow |

## Configuration

Everything an editor would touch is in `config/`.

- `config/settings.yaml`: site name, **language** (`en` or `ne`), timezone, how many stories to debate and publish, revision limit, model per role, web search allowances, editorial exclusions, image licence allow list, image generation provider.
- `config/sources.yaml`: the outlets. Each has a native RSS URL and a domain. When a native feed fails or is empty, the pipeline falls back to a Google News RSS search restricted to that domain, so a broken feed URL never blanks a source.
- `config/style.md`: the house style. The writer, reviser and judges read it verbatim.
- `newsroom/prompts/*.md`: one prompt per role. Edit freely. `_common.md` is prepended to all of them.

## Running locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest -q                              # unit tests, no network
python -m newsroom run --mock --build            # full pipeline with a mock model and fixture feeds
python -m newsroom discover                      # live scan, prints feed health and today's candidates
python -m newsroom check-sources                 # probe every feed and image provider
ANTHROPIC_API_KEY=... python -m newsroom run --build --root-rss   # a real edition
python -m http.server -d site 8000               # look at the result
```

## Cost and safety valves

A live edition with the defaults makes roughly 40 to 60 model calls: one clustering call, two per debated story, two ranking judges, and per article one writer, one picture check, and two to three validation rounds of red team, defence and judges. `pipeline.max_llm_calls` in `settings.yaml` stops the run when it is reached, and the run log records exact token counts and web search requests so you can see what a day costs before changing anything.

The default model for every role is `claude-opus-5`. Set a cheaper model for individual roles under `llm.roles`. Requests opt into the server side refusal fallback (beta) so a safety decline on one role is retried on the recommended fallback model instead of killing the story; set `llm.refusal_fallback: false` to turn that off.

## Images and credits

Found photos come only from Wikimedia Commons and Openverse, filtered to CC0, public domain, CC BY and CC BY-SA. The model looks at the candidate pictures and rejects anything that does not show the story, anything with an identifiable private person, logos, maps and screenshots. Author, source page, licence and licence link are stored with the article, shown under the image, embedded in the RSS `media:credit`, and burned into the bottom of the image file itself. AI generated images are labelled as such in the caption and in the file. When neither is available a branded cover card is used.

## Decisions to confirm

- **Language.** Articles are written in English by default (`site.language: en`). Switch to `ne` for Nepali. Sources are read in both either way.
- **Image generation provider.** OpenAI's image API is wired in because it is the most common choice. Any other provider can be added in `newsroom/images.py::generate_image`.
- **Feed coverage.** On 2026-09-26 a GitHub Actions runner confirmed 14 of the 17 native feeds. Kantipur, The Himalayan Times and Setopati English expose no reachable feed and run on the Google News fallback, which delivered 27, 1 and 17 items respectively that day. The *Check sources* workflow repeats this check weekly and suggests feed URLs for anything that breaks.
- **Openverse** allows unauthenticated requests with a low hourly limit. Commons is queried first, so this rarely matters, but registering for an Openverse key is an option if it does.

## Corrections

Open an issue. The full record behind every story is in `data/`, so an error can be traced to the source, the writer's draft, or a judge's ruling.
