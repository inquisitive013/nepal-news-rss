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
   ┌──────────────┐
   │ Investigator │  what the coverage misses: the record, the numbers,
   └──────┬───────┘  who benefits, the missing question, the pattern
          ▼
   ┌─────────────┐   ┌──────────────────────────────────────┐
   │   Writer    │──▶│ Picture desk: Commons/Openverse photo │
   └──────┬──────┘   │ checked by vision, else AI image,     │
          │          │ else cover card. Credits always.      │
          ▼          └──────────────────────────────────────┘
   ┌───────────────────────────┐
   │ Red team  ⇄  Defence      │   accuracy · relevance · defensibility · virality
   └──────┬────────────────────┘
          ▼
   Validation judge 1 ──▶ Reviser ──▶ Validation judge 2 (final check, may send back once)
          │
          ▼  approved by both
   data/articles/*.json ──▶ static site + rss.xml ──▶ GitHub Pages
          │
          ▼  once the page is live
   X · Facebook · Instagram · Threads · Telegram · Bluesky · Mastodon
```

Every model call is one request to the Claude API asking for a JSON object that matches a schema. Research roles get the server side web search tool. Every call's token usage is stored in the run log.

## The investigation

Every story goes to an investigator before it goes to the writer. The investigator searches, in English and Nepali, for what the coverage misses: what the same officials or companies said or promised before, numbers that conflict between sources, who benefits from a decision, the question every report skips, and whether it has happened before. Each finding carries its evidence, a URL and the exact fact it supports, or it is dropped. The writer runs the findings under "What the coverage missed" and the open questions under "What we still do not know". The red team opens every cited source before the piece can publish, and the article page lists the findings with their links under the review record.

## The take

Every article ends the writer's call with a take: one paragraph, 50 to 90 words, the desk's own critical read of the story in the voice of a senior correspondent. Opinion is allowed, invention is not. Every fact in it must already sit in the body with a source, and an expert appears only when a listed source quotes them. The red team and both judges check it like a headline. On the article page it sits under the dek. On Facebook and Instagram it opens the post, because it is all a reader sees before they tap See more. `python -m newsroom social --dry-run` shows the posts as they would go out.

## The Nepali edition

Every approved story is published twice: in English at `/articles/<slug>/` and in Nepali at `/ne/articles/<slug>/`, with a Nepali front page at `/ne/`, a Nepali feed at `/ne/rss.xml`, and `hreflang` links between every pair so search engines serve each reader their language. The Nepali is not a translation. A Nepali writer (`newsroom/prompts/nepali_writer.md`, Sonnet, with three web searches for the original Nepali wording of quotes and the outlets' spellings) takes the verified record of the approved English story, its facts, numbers, names, quotes, attributions, sources and findings, and nothing else (the investigator's notes stay out, so the Nepali never carries a question, a paragraph or an outlet the judged English left out), and writes the story the way Nepali news is written: Nepali headline form, the lede in the news tense, Nepali sentence order, the right honorifics, Devanagari digits, the weekday first. The craft rules are `config/style_ne.md`, and the English house style still decides what a story may claim. A Nepali editor (`nepali_editor`, Opus) reads the piece against the record and the style guide, and the writer applies every fix; `pipeline.nepali_rounds` (2) caps the editor's passes: it reads the fixed piece again, because in every story so far the second reading still found errors the first fix had left or made. The editor marks each problem `fact` (fidelity to the record, the legal standard) or `language` (craft, completeness). After the last round's fixes the editor reads the piece once more, so the text that publishes is the text it last read. A piece with a `fact` problem at that final reading is held: no Nepali page, no Facebook post, no Reel, and the record lists the problems for a person to fix. Once the text is fixed from the record, *Actions → Daily edition → Run workflow* with **backfill_nepali** ticked and scope `recheck` (`python -m newsroom nepali --recheck`) has the editor read each held piece as stored, one call a story, rewriting nothing: no fact problem clears it, a fact still wrong keeps it held. Language notes never hold a piece; the record keeps up to ten of them with the editor's fix under `language_fixes`, so a person can apply them and have the piece read again. Until 30 September the last fixes went out unread, and every piece from 27 September on ended unapproved. A fix pass works from the editor's notes without the search tool. `pipeline.nepali_fix` decides how the fixes go in: `rewrite`, the default, has the writer apply them all, leave every sentence no fix touches as it was, and return the whole piece; `in_place` writes each fix in where the editor quoted its passage, moves nothing else, and leaves to the writer only the fixes whose passage it cannot find. Every fact keeps its source inside its own sentence, as `config/style_ne.md` sets out under स्रोत, and the editor reads the sources sentence by sentence. *Actions → Nepali fix trial* (`python -m newsroom nepali-trial`) writes stored stories again, fixes each draft both ways from the same first reading, reads each finished piece once more and prints the captions side by side. It saves nothing, and costs about 2.30 dollars a story at list prices; `--ways rewrite` (the workflow's **ways** input) runs one way, up to 6 calls a story instead of 10. The 28 September trial kept `rewrite`: in place pasted the editor's own instructions into two of three ledes. `python -m newsroom nepali-rejudge` (the workflow's **rejudge_runs** input) reads earlier trials' finished pieces again with today's editor, so two trials meet one reader: read that way, twice each, the same three stories carried 56 missing sources before the source rule and 15 after. The Nepali headline, dek, take, body, card headline, social hook and the Facebook caption's two line synopsis ride on the article under `nepali`, with the editor's verdict. Outlet names the writer misspells are fixed in code before the editor reads the piece (`newsroom/outlets.py`: रातोपाती becomes रातोपाटी). Two calls per story when the editor approves at once, up to six when both readings ask for fixes and the final reading follows (about 24 cents a reading at list prices); `pipeline.nepali_edition: false` publishes English only. Stories from before this stage get a version from *Actions → Daily edition → Run workflow* with **backfill_nepali** ticked (scope `missing`, or `all` to write every story again), or `python -m newsroom nepali --missing`.

## Setup

1. **Give the workflow model credentials** (one of the two options under *Model credentials* below).
2. **Optional**: add an `OPENAI_API_KEY` secret to turn on AI illustrations when no licensed photo fits. Without it the cover card is used.
3. **Run it once by hand**: *Actions → Daily edition → Run workflow*. Tick *mock* first if you want a dry run that touches nothing. The workflow creates the GitHub Pages site on first deploy.
4. From then on the edition starts every day near 06:00 Nepal time and posts its first story near 08:00. The cron in `.github/workflows/daily.yml` is set to 05:15, because GitHub starts scheduled runs about 45 minutes late; change it if you want another hour.

## Model credentials

The workflow accepts either an API key or Anthropic's identity federation. If both are configured the key wins. *Actions → Check model credentials* verifies whichever you set up without spending tokens.

**Option A, API key.** Create a key in the Claude Console under *Settings → API keys*, scoped to one workspace and with no expiration, and store it as the repository secret `ANTHROPIC_API_KEY`. A key that is not scoped to a workspace needs the repository variable `ANTHROPIC_WORKSPACE_ID` as well.

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
| `data/runs/<date>.json` | The whole run: feed health, candidates, stories, debates, verdicts, token usage. A second run on the same day gets `<date>-2.json` |
| `data/images/` | Stored images, downscaled, with a credit bar burned in |
| `data/social/<article id>.json` | Every social post made for the article: network, id, link, text, or the error |
| `rss.xml` | Copy of the live feed at the repo root, for automations that read the raw file |
| `site/` | Built site, not committed, deployed to Pages by the workflow |

## Configuration

Everything an editor would touch is in `config/`.

- `config/settings.yaml`: site name, **language** (`en` or `ne`), timezone, how many stories to debate and publish, red team rounds and revision limits, model per role, web search allowances, editorial exclusions, image licence allow list, image generation provider.
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
python -m newsroom photos --dry-run              # what the picture desk finds for stories without a real photo
ANTHROPIC_API_KEY=... python -m newsroom run --build --root-rss   # a real edition
python -m http.server -d site 8000               # look at the result
```

## Cost and safety valves

A live edition on the defaults makes about 48 model calls: one clustering call, two per debated story (three stories), two ranking judges, and per article one investigator, one writer, one or two picture checks, one red team pass, one defence, judge 1, one revision and judge 2, then the Nepali writer and up to two editor readings with a fix after each. Judge 2 may send an article back once more, which adds a reviser call and a second judge 2 call; the reviser is capped at `pipeline.max_revisions_per_run` calls a day. `pipeline.max_llm_calls` stops the run when it is reached. The run log records exact token counts, web search requests and which calls went through the batch queue, and `cost_usd`, the estimate at the list prices in `llm.prices`, which the run summary prints.

Measured so far, at list prices. The first live edition, on `claude-opus-5` at high effort with eight debated stories and two revision rounds, used 51 calls, about 675,000 input and 448,000 output tokens and 145 web searches, roughly 19 dollars, and published nothing because two drafts hit the output cap and the credit ran out on the third. The second edition, on `claude-sonnet-5` with `claude-opus-5-5` judges, six debated stories and two red team rounds, used 42 calls, about 402,000 input and 234,000 output tokens, 2.9 million cached input tokens and 84 web searches, roughly 6.40 dollars in 29 minutes. It published one of three drafts; the other two were rejected for a single misplaced fact each after the one revision round was spent, with no way to fix it.

The current defaults come from that run's per call costs: three debated stories, one red team pass per article, then judge 1, the reviser and judge 2 as the final check. That removes the second red team and defence passes, the most expensive calls of the day, and gives every draft a revision before it is judged for the last time. The first edition on these defaults, on 27 September, used 31 calls, about 343,000 input and 187,000 output tokens, 2.2 million cached input tokens and 46 web searches: about 5.30 dollars at list prices, plus a few cents per AI generated image on OpenAI. The Nepali edition adds a writer call and one or two editor readings per published story, with a fix after each reading that asks for one. On 28 September one story with every stage cost 6.03 dollars at list prices: the Nepali edition 35 percent of it, the Opus judges 19, choosing the day's stories 18, the writer and reviser 14, the red team and defence 11 and the investigator 4. Half of all spend was output, most of it thinking. A three story day with every check comes to about 12 dollars at list prices, and about 6.50 with batch mode on. Set `pipeline.investigate: false` to run without the investigator.

**Batch mode.** `llm.batch.enabled` sends every call through the Message Batches API, where every token costs half. Each call becomes a batch of one and waits in Anthropic's queue; the request is the one the normal way sends, with the same model, prompt, effort and searches, so the answer is too. Three valves keep an edition from stalling: a call that waits past `max_wait_minutes` (15) is cancelled and asked the normal way at full price on its own, while the calls after it still try the queue; after `run_minutes` (150) every new call goes the normal way, so even a slow queue day finishes inside the workflow's four hours; and if the batch API fails once, the run stops using it. The run record keeps every wait under `batch_waits` (role, model, searches allowed, when the call joined the queue, how long it waited and what came of it), and the run summary names the longest waits, so each edition shows where the queue stalled. On 29 September two calls hit the old 25 minute limit together, the old valve shut the queue for the rest of the run, and 37 of 52 calls paid full price: 12.63 dollars. A refusal in the queue is asked again the normal way, where the server side fallback can answer, and a research turn that pauses continues the normal way. It is on. *Actions → Time the batch queue* (`python -m newsroom batch-probe`) sends two tiny requests per round for a few cents and reports how long each waited and whether web search works inside a batch. On 28 September its four requests waited 125 to 758 seconds, all answered, and web search worked, so an edition of about 17 calls in a row takes about an hour longer; the daily workflow starts near 06:00 to post near 08:00. Run the probe again after any change to the shape of the requests.

Two account settings can stop an edition part way: an empty credit balance, and a monthly spend limit (Claude Console → Settings → Billing → Spend limits; the usage tier's own monthly cap sits above any limit you set). The newsroom recognises both, stops at once instead of retrying, and keeps every draft it wrote under `data/rejected/`, marked "cut off" with the reason. The workflow still commits, deploys and posts whatever passed every check, then its last job fails the run with the reason and the fix at the top of the run page, so GitHub notifies you. Nothing unchecked is ever published. Set models and effort per role under `llm.roles`. Requests opt into the server side refusal fallback (beta) so a safety decline on one role is retried on the recommended fallback model instead of killing the story; set `llm.refusal_fallback: false` to turn that off.

## The card and the content engine

Every article with a picture gets a 1080x1400 card at build time, `newsroom/graphic.py`: the photo full bleed with a bottom gradient, the Nepal Wire header with the theme and the date, the country and theme chip, the card headline in two lines, a gold underline, the source line and the footer with the photo credit or the illustration label. Each story gets two cards: the English one for the English pages, and a Nepali one for Facebook and the Nepali pages, carrying the Nepali editor's checked card headline in Mukta ExtraBold (SIL Open Font License, in `newsroom/fonts`), autosized up to 110px, line two in gold, over a darker fade (`social.facebook.card: ne`). Devanagari needs Raqm to join its letters; where Pillow lacks it the Nepali card is skipped, the English card stands in, and a test fails in CI so the gap is seen. Facebook posts the card with a caption of three parts in native Nepali and nothing else: the hook, what the story changes for the reader; the angle, the one fact the coverage missed; and the trigger, a question about the reader's own life, left out when a story has none. The sources are on the card, and only there (`social.facebook.caption: short`; `synopsis` posts the older two line summary); `full` brings back the long form: the headline, a hook, 80 to 150 words of body, one line of friction, then "Sources available in graphic.", "Follow Nepal Wire." and the hashtags. The writer produces the card headline, the theme, the country and the caption with the article, and the red team and both judges check them under the same standard as the body. The whole standard, from the two absolutes to the correction protocol, is `docs/content-engine.md`.

## Social media

Every approved article is announced on the networks you connect, right after the site deploys. On Facebook the day's stories are spread out: the best ranked story goes out at once and the others are handed to Facebook as scheduled posts for the slots in `social.facebook.slots` (12:30 and 18:30 Kathmandu time by default), because three posts in one minute reach fewer people than three across the day. *Actions → Post to social networks* runs the same stage on demand, with a dry run option, a network filter, and an article id to post one story outright. A network is switched on by its secrets alone, added under *Settings → Secrets and variables → Actions → New repository secret*. No secrets, no posts, no error. *Actions → Check social accounts* verifies every connected account without posting anything. Each post is recorded in `data/social/`, one file per article, so a rerun never posts the same article twice. `python -m newsroom social --dry-run` prints what would go out.

The text comes from the article's caption, take, headline, dek, social hook and tags, cut to each network's limit, hashtags last. Facebook gets the card with the engine caption, and when the story has a checked Nepali version the caption opens in Nepali and carries the English under a rule, one post for both audiences, with the close in both languages (`social.facebook.languages`, `["ne", "en"]` by default; `["en"]` for English only); an article without one falls back to the take, a rule and the label THE STORY, then the whole article, the sources and the link (`social.facebook.mode: photo`; set `link` for a short post with a link card instead). Instagram, Threads and Telegram receive the picture with a shorter caption. X, Bluesky and Mastodon show the link card that the article page's Open Graph tags describe. Only articles published in the last `social.max_age_hours` (36) are announced, so connecting a new account never floods it with the archive; `--article <id>` posts an older one on purpose.

| Network | Secrets | Where they come from |
|---|---|---|
| X | `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | developer.x.com: create a project and an app. Under *User authentication settings* choose *Read and write* first, then in *Keys and tokens* generate the API key and secret and the access token and secret. A token generated before the permission change stays read only. The free tier allows posting, with a monthly cap that three posts a day stays well under. |
| Facebook Page | `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_TOKEN` | It must be a Page. Facebook's API cannot post to a personal profile, however many followers it has; a profile in professional mode is still a profile. If your audience sits on a profile, create a Page and invite them (Facebook shows the option in the Page's *Professional dashboard*), and the Page ID is under the Page's *About → Page transparency*. Then developers.facebook.com: create an app (type *Business*). In *Tools → Graph API Explorer* pick the app, choose *Get Page Access Token*, select your Page and grant `pages_manage_posts` and `pages_read_engagement`. That token lasts an hour. Open it in *Tools → Access Token Debugger*, click *Extend Access Token*, then in the Explorer call `me/accounts` with the extended token: the `access_token` it returns for your Page does not expire, and `id` is the Page ID. If the user token lands in the secret by mistake, the newsroom still posts: it fetches the Page's token through `me/accounts` on every run and the account check says so, but only until that user token expires after about 60 days. A stale `FACEBOOK_PAGE_ID` is tolerated too when that token manages exactly one Page: the newsroom uses that Page and the check names it. An app still in development mode can post to Pages you administer. |
| Instagram | `INSTAGRAM_USER_ID`, `INSTAGRAM_ACCESS_TOKEN` | The Instagram account must be Professional (Business or Creator) and linked to that Facebook Page (Instagram app: *Settings → Business tools and controls*, or the Page's settings). When generating the Page token above also grant `instagram_basic` and `instagram_content_publish`; the same token then works here. `INSTAGRAM_USER_ID` is optional: with only the token set, the newsroom asks the Page which professional account is linked to it, and keeps the id out of every log. *Check social accounts* says when the Page has no linked account yet. Captions cannot carry a clickable link, so every caption ends with "Full story at the link in our bio" and the site address. Put the site address in the profile bio. Pictures must be JPEG between 4:5 and 1.91:1; the newsroom's generated pictures and cover cards fit, a very tall archive photo is refused and recorded as failed. The same secrets send every Reel the *Post a Reel* workflow publishes to Instagram as a Reel. |
| YouTube (Reels only) | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | 1. On YouTube, signed in with the owner's Google account, create a channel named exactly like the site (*Settings → Add or manage your channel(s) → Create a channel*). The uploader refuses a token for any channel with another name, so news never lands on a personal channel. 2. console.cloud.google.com: create a project and enable **YouTube Data API v3** under *APIs & Services → Library*. 3. Set up the OAuth consent screen: user type External, the owner's address as support and developer contact. Then **publish the app** so its status reads *In production*: left in Testing, Google expires the refresh token after seven days. Only the owner uses it, so continue past Google's unverified app warning with *Advanced*. 4. *Credentials → Create credentials → OAuth client ID*, type *Web application*, with `https://developers.google.com/oauthplayground` as an authorized redirect URI. The client ID and secret are the first two secrets. 5. developers.google.com/oauthplayground: under the gear tick *Use your own OAuth credentials* and paste both. In step 1 enter the scopes `https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly`, authorize, and pick the site's channel when Google asks which account or channel. In step 2 exchange the code for tokens; the refresh token is the third secret. 6. Google keeps every upload from an unaudited project private. Ask for the audit with the YouTube API Services form (support.google.com/youtube/contact/yt_api_form); until it passes, each Short goes up private and the run says so. *Check social accounts* confirms the channel. |
| Threads | `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN` | In the same Meta app add the *Threads API* use case, add your Threads account as a *Threads Tester* and accept the invite in the Threads app (*Settings → Account → Website permissions → Invites*). Generate a long lived token with `threads_basic` and `threads_content_publish`. The user ID comes from `https://graph.threads.net/v1.0/me?access_token=...`. Long lived Threads tokens expire after 60 days; the check workflow tells you when one has, and a new one takes two minutes. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Message @BotFather in Telegram, send `/newbot`, copy the token. Create a public channel, add the bot as an administrator with permission to post, and use `@yourchannelname` as the chat ID. Ready in five minutes, no approval. |
| Bluesky | `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | Bluesky app: *Settings → Privacy and security → App passwords → Add app password*. The handle is the account name, for example `nepalwire.bsky.social`. No developer account needed. |
| Mastodon | `MASTODON_BASE_URL`, `MASTODON_ACCESS_TOKEN` | On your instance: *Preferences → Development → New application*, scope `write:statuses`, copy the access token. The base URL is the instance, for example `https://mastodon.social`. |

Meta's Graph API version defaults to `v23.0`; set the repository variable `META_GRAPH_VERSION` to move it.

### Answering comments

*Actions → Answer comments* (`.github/workflows/comments.yml`, `python -m newsroom comments`) runs every quarter hour in the two hours after each posting slot and reads the new top level comments under Facebook posts younger than `social.facebook.comments.window_minutes` (120). One model call (`comment_desk`, `newsroom/prompts/comment_desk.md`, with both style guides) sorts them:
- **Replied to:** a reader answering the post's question or sharing their experience, a question the story answers, and thanks. The reply is one or two short sentences in the reader's language, from the record only.
- **Left for a person, and listed in the run summary with the post's link:** a correction, a legal complaint, or a comment exposing a private person.
- **Left alone:** politics, anything about the monarchy, abuse, questions the story cannot answer, and the rest.

Code checks every reply again before it goes out: no links, hashtags or tagging, no repeats, at most 280 characters, and caps per post and per run. The record keeps each comment's id and category and Nepal Wire's own reply, never the reader's words, and the public run summary prints only Nepal Wire's replies. `reply: false` sorts and lists without replying; the workflow's **dry_run** drafts without posting. The Page token needs `pages_read_user_content` to read comments and `pages_manage_engagement` to reply.

### Reels

Photo posts reach the people Facebook shows the Page to, and on 30 September that was one or two viewers a post against 10,334 followers. Facebook shows Reels to people who do not follow the Page. *Actions → Post a Reel* (`.github/workflows/reel.yml`, `python -m newsroom reel --article <id>`) turns a stored story into a 1080x1920 Reel at 30 frames a second: the story's picture zooming slowly, its lines one beat at a time, then an end card with every outlet and the picture's credit.

The beats live in `data/reels/<story id>.json`, written by the editor from the verified record only: the checked card headline, sourced numbers and names, the caption's question. The hook is on the first frame, because that frame is what people see before they tap. A test holds every beats file to Meta's limits, 4 to 60 seconds. With **publish** off the workflow renders the Reel and keeps it as a download to watch first; with it on, the Reel goes to the Page in Meta's three steps (open an upload, send the file, publish) and the record gains a `facebook_reel` entry. `ffmpeg` comes from the `imageio-ffmpeg` package, so every runner encodes the same way. The file follows Instagram's Reels rules, the strictest of the three places: the index at the front, no edit lists, no B frames, AAC sound at 128 kbps and 48 kHz. Facebook took a Reel with two edit lists and 160 kbps sound that Instagram refused on 3 October.

**Sound.** A beat may carry a narration clip and the words it says (`audio` and `say` in the beats file); the beat stretches to fit its clip, so the words never run into the next lines. `end_audio` and `end_say` sign off over the end card. `music` names a bed from `data/reels/music/`, which ducks under the voice and fills the gaps between sentences. The mix is set to -14 LUFS, where phone feeds play. The clips and the beds are made on the Page owner's ElevenLabs account: the voice comes from its Nepali library and each clip is transcribed back word for word before use. `data/reels/music/README.json` records how each bed was made and what stories it suits.

**Instagram and YouTube.** With **publish** on, the same video also goes to Instagram as a Reel when the Instagram secrets are set, and to YouTube as a Short when the YouTube secrets are set (both in the table above). The Page goes first. Instagram fetches the video from the site: with **publish** on and Instagram connected, the workflow deploys the site with the Reel at `reels/<story id>.mp4` and hands Instagram that address, Meta's documented way. Meta's direct upload refused this app's Page token on 3 October, and the Facebook Reel's own file is the last resort. The file stays on the site until the next deploy replaces it; the daily deploy and this one queue behind each other. YouTube's title is the checked Nepali headline, its description the Reel's caption and the article's address. Each place is recorded in the story's social record (`instagram_reel`, `youtube_short`). A place that fails is recorded as failed and the others still get the Reel; running the workflow again without **again** posts only where the story's Reel is still missing. Google keeps every upload from an API project it has not audited private, whatever the upload asks, so until the audit passes each Short goes up private and the run says so.

### Measuring what posts do

*Actions → Read post insights* (`.github/workflows/insights.yml`, `python -m newsroom insights`) runs every six hours. It reads every live Facebook post twice, 24 and 72 hours after it went live, each reading taken within a day of its mark: unique viewers and views from Meta's post insights (`post_total_media_view_unique` and `post_media_view`, the names Meta has used since June 2026, in `social.facebook.insights.metrics`), and shares, comments and reactions from the post. The readings go on the post in `data/social/<article id>.json`. Each run also notes the Page's follower count in `data/insights/followers.json`, and the report in the run summary sets each post of the last week beside the reach score the ranking judges forecast for its story (`reach` in the verdict) and the follower change over its first day. Posts that go live within a day of each other share that follower window, so read the column as a day's change, not one post's. A scheduled photo is recorded by the photo's id, and its shares and reactions came back as error code 100 until 1 October: they belong to the post it became, so the run asks the photo for its `page_story_id` and reads the counts there. A Reel is read at the same hours from its video's insights (`/{video-id}/video_insights`): plays, unique viewers, the average watch time and the follows the Reel brought in, by the names in `social.facebook.insights.reel_metrics`. The probe of 1 October found all four as numbers in Meta's answer. The report's second table lists the week's Reels. Each run also asks the Page for its own totals by day, across everything it has posted: views, viewers, follows and unfollows (`social.facebook.insights.page_metrics`). Business Suite counted 173 viewers for the 28 days to 2 October while every post read one or two, so the Page total is the number to watch. The last eight days are asked again on every run, because Meta settles the newest days late, and kept in `data/insights/page.json`; the report's third table shows the week. *Read post insights* with **probe** ticked lists every metric name Meta returns for the newest Reel, with each answer's shape and never its value, so a renamed metric shows there.

The token needs two more permissions than posting does: `read_insights` for the post insights and `pages_read_engagement` for the counts and the follower count. Make it as described for `FACEBOOK_PAGE_TOKEN` above with both ticked as well as `pages_manage_posts`, and replace the secret. Until then each run records the Graph error code, stops at the first post, and says what the token lacks. Records carry error codes only, never Meta's messages.

Not covered, and why: LinkedIn company pages need a partner programme approval and personal posting needs a browser login every 60 days; WhatsApp channels have no public posting API; TikTok and YouTube want video. For anything else, the feed at `/rss.xml` works with Zapier, IFTTT, Buffer, dlvr.it and similar tools.

## When two editions collide

The edition job fast forwards its checkout to the branch tip before it starts, so a run that queued behind another sees what that one published. Items already cited by an article from the last three days are dropped before clustering, and the clusterer and both ranking judges get the recent headlines under `recently_published`, so a story runs again only for a real development. If the commit at the end still collides with another push, the run fails and keeps `data/` and `rss.xml` as the workflow artifact `edition-data-<run id>` for 14 days, so nothing the models wrote is lost.

## Money and trust: newsletter, members, sponsors, ads, search

The site carries the layer that turns an audience into income, all switched on from `config/settings.yaml` and empty until the publisher fills them in:

- `newsletter.signup_url` or `newsletter.embed_html`: the sign up box on the homepage, every article page, the newsletter page and the footer. `newsletter.members_url`: the paid tier for the investigations, linked wherever the box appears and on the investigations page.
- `ads.adsense_client`: the Google AdSense publisher id. When set, the AdSense script loads on every page, two responsive ad units render on each article page (after the take and before the sources), and `ads.txt` is written.
- `site.google_site_verification`: the Search Console HTML tag token. `site.contact_email`: shown on the sponsor and standards pages. `site.facebook_followers`: a verified line for the sponsor page.
- Pages: `investigations.html` lists every story whose investigation produced evidenced angles, `standards.html` is the public standard, `sponsor.html` the media kit, `newsletter.html` the sign up page. `news-sitemap.xml` carries the last two days for Google News and `robots.txt` points at both sitemaps.

## A private repository

The site never needs the repository to be public: GitHub Pages deploys from the workflow whatever the repository's visibility. Keeping the code, the prompts and the data private closes the easiest route to copying the newsroom or gaming its judges. With `site.repo_url` empty (the default) no page links to the repository or its issue tracker; readers report errors through the contact email when `site.contact_email` is set, otherwise through the Facebook Page (`site.facebook_url`). Set `site.repo_url` to link the code again. Private repositories need a paid GitHub plan for Pages: *Settings → General → Danger zone → Change visibility*.

## Your own domain

A github.io address ties the brand to a GitHub username. Buy a domain (nepalwire.com or similar), then at the registrar add four A records for `@` pointing at `185.199.108.153`, `185.199.109.153`, `185.199.110.153` and `185.199.111.153`, and a CNAME record for `www` pointing at `<owner>.github.io`. Set `site.custom_domain` in `config/settings.yaml` to the bare domain and merge: every link the newsroom writes, the feed, the sitemap and the CNAME file switch to it on the next build. Then in the repository open *Settings → Pages → Custom domain*, enter the domain, save, and tick *Enforce HTTPS* once the certificate shows (up to an hour after the DNS records go live). Old github.io links redirect to the new address. A repository variable `SITE_URL` still overrides everything, for a staging copy.

## Rebuilding the site without a new edition

*Actions → Daily edition → Run workflow* with **rebuild_only** ticked rebuilds the site from the data already in the repository and deploys it. No model calls, nothing committed. Use it after a template change or when the site needs redeploying. The workflow also makes sure the Pages source is GitHub Actions: a site left on "deploy from a branch" rebuilds itself after every edition commit and overwrites the deployed site with a Jekyll rendering of the repository, which makes every article link a 404.

## Images and credits

The card carries a real photograph of the story's subject whenever one passes the checks. An AI illustration is the last resort before the cover card, never the first choice.

The writer names two to four subjects the way a photo library files them: the public figure the story is about, the institution, the building, the place. For each subject the picture desk looks in this order and stops when it has enough:

1. the image Wikidata keeps for the subject,
2. Commons files whose structured data says they depict it,
3. the subject's Commons category,
4. a Commons full text search, shortening the name until something turns up,
5. Openverse.

Every candidate goes to the model with its documentation: title, description, categories, date and what it depicts. Identity is confirmed by that documentation, never by resemblance. The model rejects a different event of the same kind, children, private people, logos, maps, small or blurred frames, and anything from another country. It sees up to six pictures a round for up to two rounds (`images.picker_rounds`). Logos, maps and photos too small to cover the card without being blown up more than 2.2 times are dropped before the model sees them. A photo a story used in the last 30 days is not used again (`images.rotation_days`).

Only licences in `images.allowed_licenses` pass: CC0, public domain, CC BY and CC BY-SA. Author, source page, licence and licence link are stored with the article, shown under the image, embedded in the RSS `media:credit`, and burned into the bottom of the stored image. The card's footer says "File photo", names the author, the licence and the library, and says the picture was adapted, as CC BY 4.0 and BY-SA 4.0 require. An illustration says "Illustration: AI generated for Nepal Wire. Not a photograph." When neither is available the branded cover card is used.

Each article's review record says what the desk looked for, what each library gave, how many pictures the model saw and why the story carries what it carries (`review.picture`).

`python -m newsroom photos --dry-run` shows what the desk finds for the stories that carry an illustration or a cover card, with no model calls and no changes. CI runs it for every stored story on each push. `python -m newsroom photos` looks again for those stories and swaps in a real photo when one passes, and the *Daily edition* workflow does the same with **find_photos** ticked; add an article id in **photos_article** to look again for that one story even when it carries a real photo, after changing its `image_brief.search_queries` if the desk searched the wrong subjects.

## Decisions to confirm

- **Language.** Articles are written and judged in English (`site.language: en`) and published in English and Nepali (see "The Nepali edition"). Sources are read in both either way.
- **Image generation provider.** OpenAI's image API is wired in because it is the most common choice. Any other provider can be added in `newsroom/images.py::generate_image`.
- **Feed coverage.** On 2026-09-26 a GitHub Actions runner confirmed 14 of the 17 native feeds. Kantipur, The Himalayan Times and Setopati English expose no reachable feed and run on the Google News fallback, which delivered 27, 1 and 17 items respectively that day. The *Check sources* workflow repeats this check weekly and suggests feed URLs for anything that breaks.
- **Openverse** throttles unauthenticated callers. The desk spaces its Openverse requests just over a second apart and asks Openverse last, so this rarely matters, but registering for an Openverse key is an option if it does.
- **Share alike photos.** CC BY-SA asks that an adaptation be shared under the same licence. The card crops a BY-SA photo and sets a headline across it, so the media lawyer's one time review of the card template should settle whether a BY-SA card needs its own licence line. Until then the picture editor prefers CC0, public domain and CC BY when two pictures are equally good.

## Corrections

A story that moves on after it ran carries a dated note under its headline, in English and Nepali: add an entry to the article's `updates` list (`date`, `kind` update or correction, `text`, `text_ne`, and `link` to the follow-up such as `articles/<slug>/`), raise its `version`, and rebuild. A card with an error is replaced, not left to circulate as a screenshot. Edit the article's JSON, then run *Actions → Post to social networks* with the article id and **replace** ticked: it takes the story's Facebook post down and posts the corrected card and caption. Nothing new goes out if the takedown fails, so two copies never stand side by side. A takedown costs the post every reaction, comment and share it has, so a fix that lives only in the caption takes **edit** instead: the live post gets the caption the article's JSON gives now, in place, and keeps all three. A failed edit leaves the post as it was and says so in the record.

Readers reach the newsroom through the channel the site shows: the contact email when set, the issue tracker when `site.repo_url` points at a public repository, the Facebook Page otherwise. The full record behind every story is in `data/`, so an error can be traced to the source, the writer's draft, or a judge's ruling.
