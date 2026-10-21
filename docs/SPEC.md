# SPEC: Night Scout, an overnight scout that turns the internet into next steps

| | |
|---|---|
| Status | Approved by Sai, 2026-10-21. Build now. |
| Contest | DEV **Hacktoberfest Open-Source AI Challenge, Week 3: "Slow and Steady"**. Entry period Oct 19, 9:00 AM PDT → **Oct 25, 2026, 11:59 PM PDT**. Hub: https://dev.to/challenges/hacktoberfest-week3-2026-10-19 · Rules: https://dev.to/page/hacktoberfest-week3-2026-10-19-contest-rules |
| Project dir | `~/projects/night-scout` (new; `git init` here). It's a brand-new project, so don't copy code from Snippbot or other private repos. |
| License | MIT |
| AI core | **Local Gemma via Ollama.** Open-weight model, local inference. This is the "open piece that makes it work", and it targets the **Best Use of Gemma** featured prize ($200). |

## 1. The contest (verified 2026-10-21; re-check the live pages before publishing)
- **Theme:** build something with open-weight models or open-source AI that *rewards laziness*: a tool that relieves a task you dread.
- **Eligibility:**
  - It must be a **brand-new project created and completed during the entry period**. Pull requests to existing projects don't count.
  - The open pieces must be what makes it work.
  - One entry per person, and a person can win at most one prize.
- **Prizes:**
  - Overall: $250 plus DEV++.
  - Six featured categories at $200: Render, TabPFN, Tinker, Arduino, DigitalOcean and **Gemma**.
  - Fourteen partner categories at $100.
- **Judging:** **Writing Quality (weighted most)**, then Relevance to Prompt and Theme, Creativity, Technical Execution, and Use of Partner Technology (partner categories only). Ties go to the post with the most positive reactions.
- **Submission:** a DEV post using the challenge template.
  - Front matter has tags `devchallenge` and `hf26challenge`.
  - Sections: **What I Built · Demo · Code · How I Built It · Why Does Open Innovation Matter? · My Agent Session (optional) · The Lazy Payoff (optional) · Prize Categories**.
  - The demo is a deployed link **or a video**. The code is an embedded public GitHub repo.
  - Optionally, a DevRelay agent session (judges "love" it). If it's cheap, save one coding session with DevRelay.

## 2. The idea
Before bed you do nothing. Overnight, Night Scout reads the parts of the internet you'd otherwise doom-scroll: YouTube channels, blogs, Hacker News, Reddit, Bluesky, Mastodon and GitHub releases. Local Gemma judges what matters **to your actual projects**. In the morning you get a short digest of **things worth doing**, each with one-click actions, not another feed of summaries.

**What sets it apart from prior art:**
- Existing open-source aggregators summarize and rank: AI News Brief (frankzch/ai-news-brief), Auto-News, huawolf/news-agent.
- Night Scout is **project-aware** (it reads your repos) and **action-first** (typed next steps). Say this plainly in the post, with links to that prior art.

## 3. Scope (MVP for Oct 25)

### 3.1 Sources
Official feeds and public APIs only. No logged-in scraping, and respect each source's rate limits and terms.

| Source | How |
|---|---|
| YouTube channels | Channel RSS (`https://www.youtube.com/feeds/videos.xml?channel_id=…`), plus transcripts via `youtube-transcript-api`. If no transcript, fall back to title and description. |
| Blogs / sites | RSS/Atom (`feedparser`) |
| Hacker News | Official Firebase API (top/new/show) or the Algolia search API for keywords |
| Reddit | Public `.json` listings for chosen subreddits, with a descriptive User-Agent and polite rate limiting |
| Bluesky | Public AppView search/feeds (`public.api.bsky.app`, no login) |
| Mastodon | Public hashtag timelines on chosen instances |
| GitHub | Releases of watched repos, plus a topic or language search for new repos (unauthenticated API with backoff; optional token from env) |

X/Twitter is out of scope because it has no free API. Sources are configured in `config.yaml`.

### 3.2 Knowing your projects (the profile)
- `config.yaml` lists project directories. For each, read the README (headings and first paragraphs) and the dependency manifests (`package.json`, `pyproject.toml`, `requirements*.txt`, `Cargo.toml`, `go.mod`), and detect the languages.
- From those, Gemma builds `profile.yaml`: per project, a one-line purpose, stack, key dependencies, "interests" and "would want to know about". It's **human-editable** and regenerated only on request.
- **Privacy:** the profile and everything else stays local. No data leaves the machine except the fetches to public sources.

### 3.3 Pipeline (the nightly run)
1. **Fetch:** new items since the last run, into SQLite (`~/.night-scout/scout.db` or the XDG equivalent).
2. **Normalize + dedupe:** by URL/canonical id. Optionally, near-duplicates by embeddings: Ollama `embeddinggemma` or `nomic-embed-text`, whichever is current.
3. **Cheap pre-filter:** keyword/embedding similarity to the profile, so Gemma only sees likely-relevant items.
4. **Gemma scoring (structured JSON):** per item, relevance 0–1 per project with a short reason, plus a 2–3 sentence summary.
5. **Action proposals (structured JSON):** for items above a threshold, 1–3 typed actions:
   - `try_it`: a new tool, library or model worth testing. Gemma drafts an install + smoke-test plan (commands plus the expected success signal).
   - `research_more`: fetch the full article or transcript and write a deeper brief.
   - `note_for_project`: a markdown note to drop into that project (it's written only when the user clicks).
   - `watch_later` / `ignore`.
6. **Digest:** a ranked morning list (top N, default 7) as `digests/YYYY-MM-DD.md` plus the web page.

### 3.4 Actions and safety (non-negotiable)
- **Nothing runs automatically.** The nightly run only proposes.
- **Try it:**
  - Shows the exact commands. The user clicks **Run in sandbox**, then confirms.
  - Execution order of preference: (1) a throwaway Docker/Podman container if available; (2) else a throwaway venv / temp dir with no access to the user's projects. Always a timeout, captured logs, and the result judged against the expected signal.
  - Never `sudo`, never global installs, never touches the user's projects. Clean up afterwards.
  - Generated commands are checked against a denylist (curl-pipe-to-shell, `rm -rf` outside the sandbox, `sudo`, credential paths) and refused with an explanation.
  - The UI warns that new packages from the internet are a supply-chain risk.
- **Research more** runs on demand and has a cost of $0 (local).
- **Feedback:** 👍/👎/ignore on an item, or a whole source/topic, adjusts the pre-filter weights and is stored locally.

### 3.5 Interfaces
- **CLI:** `night-scout init` (writes the config, scans projects, builds the profile) · `night-scout run` (one nightly pass) · `night-scout serve` (local web UI on 127.0.0.1) · `night-scout schedule` (prints or installs a nightly job: launchd on macOS, cron/systemd user timer on Linux, Task Scheduler instructions on Windows).
- **Web UI:**
  - **Morning** view: the digest cards. Each card shows source, why it matters to which project, a summary and action buttons.
  - **Item detail:** the research brief, transcript excerpt, and Try-it plan and logs.
  - **Sources** and **Profile** editors.
  - Keep it small: FastAPI + server-rendered HTML (htmx is fine). No heavy frontend build. Make it look good in screenshots, since judges read the post.
- **Config:** `config.yaml` with the model name, thresholds, sources, project dirs and schedule time.

### 3.6 Model setup
- Install Ollama (free). Set `OLLAMA_MODELS=/Volumes/Workspace/ollama-models`, because the internal disk is small; see the disk rules below.
- Use the **current** Gemma release on Ollama. Check `ollama.com/library` for the newest Gemma and size that runs well on an Apple Silicon Mac. Pick a default (e.g. a ~12B model for quality) and a fallback (~4B for speed), both configurable.
- Use Ollama's JSON / structured-output mode for every scoring and action call. Validate against a schema and retry once on invalid output. A failure is logged and skipped, never fatal.

### 3.7 Later: a Snippbot package
**Not in the contest entry.** Keep the core importable (`night_scout.core`) and the CLI/UI thin, so a later Snippbot package can wrap it:
- a page at `/apps/night-scout` per Snippbot's package UI standard;
- a future Dashboard "Feed" tab;
- a chosen research agent;
- actions handed to Snippbot chat, loops or swarms.

Don't import or copy any Snippbot code. Snippbot is source-available, not open source.

## 4. Quality bar
- **Tests (pytest):** fixture feeds for every source adapter (no network in tests), dedupe, the profile builder, JSON-schema validation and retry with a fake Ollama client, the command denylist, and sandbox runner argument building.
- Lint with `ruff`. Type hints throughout.
- **Cross-OS paths:** use `pathlib` and no hardcoded user paths in code. The config holds the paths.
- **Errors:** every source failure or model error is logged, and the run continues with the others. Show a clear status line in the UI ("Reddit: rate-limited, skipped").
- **README:** what it is, a screenshot, the 2-minute quick start (install Ollama, pull Gemma, `pip install -e .`, `night-scout init`, `run`, `serve`), the safety model, the prior-art comparison and the MIT license.

## 5. Demo and post (writing quality is weighted most, so budget real time)
- **Demo video, 2–3 minutes:**
  1. Bedtime: `night-scout schedule`.
  2. Morning: open the digest.
  3. A real card, for example "new open model/tool X → relevant to <project> because …".
  4. Click **Try it** → sandbox run → pass/fail.
  5. **Research more** → brief.
  6. A 👎 tuning the feed.

  Record on the real machine. Use a **demo profile of public-safe projects** (no private repo names or paths on screen), or blur them.
- **DEV post:** the template sections from §1, written in first person for Sai.
  - Hook: we don't want another summarizer; we want the internet's best *next steps* waiting with the coffee.
  - **The Lazy Payoff:** minutes saved per morning (honest estimate) and items acted on.
  - **Why open innovation matters:** local open weights mean private project context never leaves the laptop, $0 per run, and it can run every night forever.
  - **Prize Categories:** Best Use of Gemma.
- Put the post draft at `docs/DEV_POST.md`. **Sai publishes it from his DEV account.**

## 6. Timeline
| Date | Milestone |
|---|---|
| Oct 21 | Repo + config + source adapters + SQLite + dedupe; Ollama + Gemma installed |
| Oct 22 | Profile builder, Gemma scoring/summaries, digest markdown; tests |
| Oct 23 | Actions (Try it sandbox, Research more, notes, feedback), web UI, schedule command |
| Oct 24 | Audit → fixes; README; real overnight run; demo video; post draft |
| Oct 25 | Sai reviews; publish repo + post with buffer (target 6 PM PDT) |

## 7. Rules for the building session
- Cadence: implement each milestone → audit it against this spec (correctness, safety, silent failures) → fix everything → next. Full E2E (a real run plus the UI) at the end.
- **Spend $0.** No paid APIs.
- **Sai approves outward actions:** creating the **public** GitHub repo, pushing, and publishing the DEV post. Prepare everything; ask before doing these.
- **Disk:** the internal disk is small (~46 GB free on 2026-10-21). Ollama models go to `/Volumes/Workspace/ollama-models`. Clean up sandbox containers, venvs and temp dirs after each Try-it, and leave no large files behind.
- Commit early and often (local git). Keep `docs/PROGRESS.md` updated after each milestone.
