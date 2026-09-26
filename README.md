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

1. **Add secrets** under *Settings → Secrets and variables → Actions*:
   - `ANTHROPIC_API_KEY` (required)
   - `OPENAI_API_KEY` (optional; turns on AI illustrations when no licensed photo fits. Without it the cover card is used.)
2. **Turn on GitHub Pages** under *Settings → Pages* and set *Source* to **GitHub Actions**. One time only.
3. **Run it once by hand**: *Actions → Daily edition → Run workflow*. Tick *mock* first if you want a dry run that touches nothing.
4. From then on the edition publishes every day at 06:15 Nepal time. Change the cron in `.github/workflows/daily.yml` if you want another hour.

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
- **Feed URLs.** The native RSS addresses in `config/sources.yaml` have not all been confirmed live from this project. Run the *Check sources* workflow: its summary lists which native feeds answer and which sources are running on the Google News fallback.
- **Openverse** allows unauthenticated requests with a low hourly limit. Commons is queried first, so this rarely matters, but registering for an Openverse key is an option if it does.

## Corrections

Open an issue. The full record behind every story is in `data/`, so an error can be traced to the source, the writer's draft, or a judge's ruling.
