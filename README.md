# Night Scout

**An overnight scout that turns the internet into a short morning Digest, and into next steps
for your own projects.**

Before bed you do nothing. Overnight, Night Scout reads the feeds you'd otherwise
doom-scroll: YouTube channels, blogs, Hacker News, Reddit, Bluesky, Mastodon and GitHub
releases. A local Gemma model, running through Ollama, files what matters under **the topics
you follow**, and, if you point it at the projects on your disk, judges what matters **to
those projects**. In the morning you get two pages:

- **Digest** (opens first): the night's best stories for your topics, 25 at most, each with a
  plain summary, the key points and the video right in the card. Everything else from the
  night is one click below, ranked by similarity, so nothing is hidden. No projects needed.
- **Actions** (with projects): a short list of things worth doing, each labelled Issue,
  Change, Improvement or Relevant with a typed next step: *Try it* in a sandbox, *Research
  more*, *Note for project*, *Watch later*. Most mornings it's three to seven cards; some
  mornings it says "nothing cleared the bar".

![Night Scout morning view](docs/screenshot-morning.png)

- **Your topics, your words.** Type the topics you follow (in Setup or on the Digest). Gemma
  files each item under one of them with a strict 0–100 % score; only 80 % and up make the
  Digest. Weak titles (a list of names, a wall of hashtags, another language) get a short
  Gemma headline, with the original underneath.
- **Project-aware, optionally.** Point it at project folders and it reads their READMEs and
  dependency files into a small, editable profile of what each project cares about.
- **Action-first.** Every pick is labelled for the project it fits (Issue, Change,
  Improvement or Relevant), shows the profile entry it matched and why, and comes with a
  next step plus one to three concrete actions. Gemma also suggests two small new project
  ideas each night.
- **Your models.** Ollama by default; LM Studio, any OpenAI-compatible server, OpenAI or
  OpenRouter if you want (Settings → AI Providers). API keys live in the OS keychain.
- **Local and free.** Open weights on your own machine: your project context never leaves
  the laptop, each run costs $0, and it can run every night forever.
- **Nothing runs on its own.** The nightly run only proposes. Try-it plans run in a
  throwaway sandbox, and only after you read the exact commands and confirm.

## 2-minute quick start

You need Python 3.11+ and [Ollama](https://ollama.com).

```sh
# 1. Ollama and the models (Gemma 4 12B judges, EmbeddingGemma pre-filters)
brew install --cask ollama-app      # or the installer from ollama.com
ollama pull gemma4:12b
ollama pull embeddinggemma-2:270m
ollama pull gemma4:e4b              # optional smaller fallback

# 2. Night Scout
git clone https://github.com/saixiong/nightscout && cd nightscout
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 3. Set it up in the browser: model, topics (and projects if you like), sources, schedule
night-scout setup        # opens http://127.0.0.1:8765 on the setup walkthrough
#    or from the terminal: night-scout init ~/code/project-a ~/code/project-b
night-scout run          # first run: ~10-15 min on an M-series Mac
night-scout serve        # the Digest, Actions and everything else

# 4. Make it nightly
night-scout schedule             # shows what will be installed
night-scout schedule --install   # launchd (macOS) / systemd timer (Linux)
```

> Use the official Ollama build (the cask or ollama.com). Homebrew's `ollama` *formula*
> currently ships without the grammar library that constrains output to a JSON schema.
> Night Scout still works on it (it falls back to schema-in-prompt plus validation), but
> constrained output is more reliable.

`night-scout init` writes `~/.night-scout/config.yaml` with a small, public-safe starter set
of sources. Edit it, or use the **Sources** page in the web UI. Set `NIGHT_SCOUT_HOME` to keep
everything somewhere else.

## How a night works

```
fetch ─► dedupe ─► pre-filter ─► Gemma judges ─► Gemma proposes ─► digest
 7 kinds  URL +     EmbeddingGemma   relevance 0–1   try_it /           digests/YYYY-MM-DD.md
 of feeds near-dup  + keywords +     per project,    research_more /    + the Morning page
          (cosine)  your 👍/👎       summary, topics note / watch_later
```

1. **Fetch** new items from official feeds and public APIs only: YouTube channel RSS (plus
   transcripts when available), RSS/Atom, Hacker News (Firebase + Algolia), Reddit
   subreddit RSS, the Bluesky public AppView, Mastodon hashtag timelines, and GitHub releases
   and repo search. Requests are spaced per host and use a descriptive User-Agent. A
   source that fails is reported ("Reddit r/x: HTTP 429, skipped") and the run goes on.
   Each feed-style source has a **look back** (1–14 days, default 3) and optional **Scout key
   terms**: new items that mention one go to the front of Gemma's queue. When a source with
   terms is added, its feed is checked once at any age; a YouTube channel is searched as a
   whole (YouTube Data API with your key, or [yt-dlp](https://github.com/yt-dlp/yt-dlp) without
   one; that's the one place Night Scout reads public web pages instead of a feed, and only
   when you ask for it).
2. **Dedupe** by canonical URL (tracking parameters stripped, YouTube URL forms folded)
   and by embedding similarity. Something seen on HN *and* Reddit counts as one item with
   a little extra buzz.
3. **Pre-filter** cheaply, so Gemma only reads likely-relevant items. Each project's
   interests become retrieval queries for EmbeddingGemma, and keyword hits on your
   dependencies plus your feedback nudge the order.
4. **Judge.** Gemma returns structured JSON (validated against a schema, retried once,
   skipped and logged if still invalid): a relevance score and one-line reason per project,
   a 2–3 sentence summary and topic tags.
5. **Propose.** For items above the bar, Gemma suggests one to three typed actions. A
   Try-it plan is install commands, a smoke test and the output that means "it works".
6. **Digest.** The top picks (seven by default) go to markdown and the web UI.

## The safety model

Night Scout reads untrusted text from the internet and asks a language model to suggest
commands. It's built so that this can't hurt you by itself.

- **Nothing runs automatically.** The nightly run only writes proposals.
- **The denylist runs at proposal time and again just before a run.** It refuses sudo,
  `curl | sh` and download-then-run, global or user installs, `rm -rf` outside the
  sandbox, credential paths (`~/.ssh`, `.aws`, `.env`…), your home directory, local
  services, uploads and backgrounded processes. A refused plan shows the reasons
  instead of a Run button.
- **Explicit confirm.** Before anything runs you see the exact commands, where they'll run,
  the time limit, the pass condition and a supply-chain warning. Then you tick a box.
- **A throwaway sandbox, every time:**
  - With Docker or Podman: a fresh container with no host mounts except the read-only
    script, all capabilities dropped, `no-new-privileges`, 2 GB RAM, 2 CPUs, 512 PIDs,
    removed afterwards.
  - Otherwise: a fresh virtualenv in a temp dir with a scrubbed environment (no tokens or
    keys, `HOME` inside the sandbox), fenced by the OS:
    - On macOS, `sandbox-exec` blocks reading your home folder and other drives, writing
      outside the sandbox, talking to local services (this UI, Ollama, Docker's socket)
      and launching apps.
    - On Linux, bubblewrap gives a read-only system with an empty home.
    - Where neither fence exists, venv runs are refused unless you opt in.
  - Always a timeout that kills the whole process group, captured logs, and a pass/fail
    against the expected output. The sandbox directory is deleted afterwards.
- **Feed text is treated as data.** Every prompt tells Gemma that item text is untrusted
  and must not be followed. *Research more* only fetches public addresses (checked on
  every redirect, connecting to the checked IP). The UI renders no remote images.
- **The local web UI** binds to 127.0.0.1 only, needs a per-launch session key, checks
  Host and Origin, puts a CSRF token on every form, and sends a strict Content-Security-Policy.
- **Notes** are written only when you click, only inside that project's
  `notes/night-scout/` folder, and never overwrite a file.

Packages from the internet are a supply-chain risk. The sandbox limits the damage; it
doesn't make a package trustworthy.

## Privacy

Your profile, the database, digests and votes live in `~/.night-scout/` (or
`$NIGHT_SCOUT_HOME`). The only network traffic is the fetches to public sources (and, if you
click Research more, to that item's page). Model calls go to Ollama on localhost.

## Ask Gemma (the chat drawer)

**✦ Ask** opens a drawer where Gemma answers questions about a day's Digest or one story.
It doesn't guess from memory: it calls research tools and shows each call as it happens
(click a call to see what came back), then answers with links to the pages it used.

| Tool | What it does |
|---|---|
| Tonight's items | Searches every item fetched that night: titles, headlines, topics, summaries |
| Read a page | Fetches one public web page and keeps the readable text |
| YouTube transcript | The English transcript of a video |
| Hacker News | Stories, points and comment counts from HN search |
| Web search | Optional: your SearXNG instance or the Brave Search API |

The chat uses the `chat` model role, which follows the Judge model unless you set
`roles.chat` in `config.yaml`. Conversations are kept in `scout.db`.

Safety: text from pages, transcripts and search results is data, never instructions (the
prompt says so, and tool output is passed back as tool results). One answer gets at most
five tool calls. Read a page only fetches public http(s) addresses, checked on every
redirect and pinned to the checked IP, with time, size and content-type limits; it can't
reach this computer or your network. Tools run from your machine; with a local model,
nothing but those fetches leaves it.

### Web search (optional)

Off by default; then Gemma isn't offered the tool at all. Pick a provider in
**Settings → Chat & search** and press **Test**.

- **SearXNG** (no key): run your own instance, e.g.
  `docker run -d -p 8888:8080 -v "$PWD/searxng:/etc/searxng" searxng/searxng`, then enable
  JSON output in `searxng/settings.yml` (under `search:`, set `formats: [html, json]`) and
  restart it. Enter `http://127.0.0.1:8888` in Settings. Without JSON output SearXNG answers
  403 and the Test button says how to fix it.
- **Brave Search API**: get a key at [brave.com/search/api](https://brave.com/search/api/)
  (there's a free tier) and paste it in Settings. It's stored in the OS keychain under
  `brave-search`, shown only masked, and never written to `config.yaml`.

Either way, only Gemma's search words go to the provider. In `config.yaml`:

```yaml
search:
  provider: searxng        # none | searxng | brave
  searxng_url: http://127.0.0.1:8888
```

## Configuration

`config.yaml` holds the models, thresholds, sources, project directories and schedule:

```yaml
model:
  name: gemma4:12b          # judge
  fallback: gemma4:e4b      # used if the default isn't installed or fails to load
  embed: embeddinggemma-2:270m
thresholds:
  max_scored: 40            # how many pre-filtered items Gemma reads per night
  relevance: 0.6            # bar for the digest
  digest_size: 7
projects:
  - path: ~/code/project-a
sources:
  youtube: [{name: Fireship, channel_id: UCsBjURrPoezykLs9EqgamOA}]
  rss: [{name: Simon Willison, url: https://simonwillison.net/atom/everything/}]
  hackernews: {lists: [top, show], keywords: [ollama, gemma]}
  reddit: {subreddits: [LocalLLaMA]}
  bluesky: {queries: [local llm], actors: [], feeds: []}
  mastodon: [{instance: mastodon.social, hashtags: [llm]}]
  github: {releases: [ollama/ollama], search: ["topic:llm language:python"]}
schedule:
  time: "04:30"
```

`profile.yaml` is yours to edit. The top-level `topics:` list drives the Digest; the
`projects:` entries drive Actions. `night-scout profile` regenerates the project entries from
your folders (overwriting your edits to them) and keeps your topics.

## How it compares

There's good prior art in open-source news aggregation:
[AI News Brief](https://github.com/frankzch/ai-news-brief),
[Auto-News](https://github.com/finaldie/auto-news) and
[news-agent](https://github.com/huawolf/news-agent). They collect, summarize and rank.
Night Scout differs in two ways:

- **It knows your projects.** Relevance is judged per project, from your actual READMEs and
  dependencies, not a list of keywords.
- **It ends in an action.** Each pick is a typed next step you can take in a click: a
  sandboxed trial, a research brief, or a note in the repo.

## Development

```sh
pip install -e '.[dev]'
pytest            # fixture feeds for every source, a fake Ollama, the denylist, the sandbox
ruff check . && mypy night_scout
```

The core is importable (`night_scout.core`); the CLI and web UI are thin layers over it.

Built with [Gemma](https://ai.google.dev/gemma) via [Ollama](https://ollama.com),
FastAPI, feedparser, httpx, pydantic and youtube-transcript-api.

## License

MIT. See [LICENSE](LICENSE).
