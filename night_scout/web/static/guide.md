# What Night Scout does

Night Scout reads the internet while you sleep and turns it into a short morning Digest of the topics you follow, and, if you like, into next steps for the projects you're working on.

Every night it fetches new items from your sources. Gemma files each one under one of **your topics** and scores how well it fits. In the morning you get two pages:

- **Digest** (opens first): the night's best stories for your topics, with a summary, key points and, for videos, the player right in the card. No projects needed.
- **Actions** (with projects): a handful of picks, each tied to one of your projects and labelled with what it means for that project.

Everything runs on your own machine with an open model (Gemma 4 by default). Nothing is sent anywhere unless you add a cloud provider yourself.

# Your first night

1. Install Ollama and pull the models: `ollama pull gemma4:12b` and `ollama pull embeddinggemma-2:270m`.
2. Clone the repo and run `pip install -e .`.
3. Run `night-scout setup`. The walkthrough in your browser picks the model, asks whether you want **Digest + My projects** or **Just the Digest**, then your topics, sources and schedule. (From the terminal: `night-scout init ~/code/my-app` with your project folders.)
4. Finish with **run now** (about 15 minutes for 40 judged items on Gemma 4 12B), or let it run every night at 04:30.
5. Later, open the app with `night-scout serve` and use the link it prints.

# The Digest

Gemma reads the night's most promising items against your topics. Every story that scores **80 % or more** makes the Digest, 25 at most, grouped under your topics on the left. The ring on each one is its score.

- Click a story in the list to jump to it in the feed on the right, five stories per page. **j** and **k** step through.
- Each story has a short summary written for you and three to five key points. **From the source** shows the original text.
- Weak titles (a list of names, a wall of hashtags, another language, a very long title) get a short headline from Gemma. The original title is right underneath.
- YouTube stories play in the card through YouTube's no-cookie player.
- **Everything else** at the bottom of the list is the rest of the night, ranked by similarity to your topics, so nothing is hidden. Items Gemma didn't have time to read say so.
- The day buttons at the top bring back earlier nights.

On each story, 🔖 saves it (it stays under **Saved** after the Digest rolls over), 👎 **Not for me** hides it and ranks items like it lower, and 🔇 mutes its source. Hiding and muting can be undone from the message that appears.

# Topics

Topics are what the Digest looks for. Type them in your own words: "Local LLMs", "AI policy", "Rust". Change them in Setup or with **✎ Topics** on the Digest. New topics fill from the next run. They're saved at the top of `profile.yaml` and survive profile rebuilds.

You need at least one topic or one project. Without topics, the Digest uses your project names.

# Actions

Actions come from your projects. The top of the Actions page shows the funnel: how many new items the scout found, how many it judged with Gemma, and how many it picked. Everything else was dropped on purpose. A story that is also an Action is tagged **In Actions** in the Digest, with a link to its card.

Each pick is labelled with a kind:

- **Issue**: a risk to check, such as a vulnerable dependency or a breaking bug.
- **Change**: a release or API change your project should track or adapt to.
- **Improvement**: something your project could adopt to get better.
- **Relevant**: worth knowing, no action needed yet.

Open a pick to see **why it matters**: the project it fits, the score for every project, and which of that project's interests it matched. Under the picks, **New project ideas** suggests small side projects Gemma thinks you'd enjoy, inspired by last night's items and your skills.

# Acting on a pick

- **Try it** runs a small, model-written plan (for example `pip install` plus a one-line smoke test) in a throwaway sandbox. You see the exact commands and confirm before anything runs. The sandbox has no keys or tokens, cannot touch your projects, is fenced off from your home folder on macOS, and is deleted afterwards. A safety check refuses plans with sudo, global installs, pipes to a shell and similar.
- **Research** has Gemma read the full article or video transcript and write a short brief for your project: what's new, what it means for you, and the caveats.
- **Note for a project** saves a markdown note into `notes/night-scout/` inside that project. Nothing else in the project is touched.

Packages from the internet can be malicious. The sandbox limits the damage; it does not make a package trustworthy. Read the commands before you run them.

# Teaching the scout

In the Digest, 👎 and 🔇 work as described above. On an Action, 👍 and 👎 nudge its source and topics up or down for tonight's pre-filter. ✕ hides the pick for good. A source you down-vote three times is muted. You can see everything it has learned under **Learned from your votes** on the Sources page.

# Projects

Projects are optional; they power Actions. The Projects page is what the scout judges Actions against. For each project folder, Gemma reads the README and dependency files and writes a profile: purpose, stack, key dependencies, interests, and what you'd want to know about. Every item Gemma reads is scored against every project. Edit anything; your edits are what tomorrow's run uses. **Rebuild with Gemma** rewrites the profile from the folder again and overwrites your edits.

# Sources

Night Scout only uses official feeds and public APIs. No logins, no scraping.

- **Websites and blogs**: paste the address. Night Scout finds the site's RSS or Atom feed. Sites without a feed can't be followed.
- **YouTube channels**: paste the channel address or @handle. Transcripts are included, so Gemma judges what's said, not just the title.
- **Newsletters and podcasts**: anything with an RSS feed (Substack, Ghost, Buttondown, most podcasts).
- **Hacker News, Reddit, Bluesky, Mastodon, GitHub, arXiv, dev.to**: lists, keywords, subreddits, hashtags, releases and repo searches.
- **X (Twitter)** isn't supported. X has no free public API and its terms don't allow scraping. Many of the same people also post on Bluesky or Mastodon.

# Asking Gemma

**✦ Ask** opens a chat drawer about the day's Digest or one story. Gemma looks things up before it answers: it can search everything the scout fetched that night, read a web page, pull a YouTube transcript and search Hacker News. Each lookup shows up in the drawer as it happens; click one to see what came back. Answers link the pages they used and say plainly when something couldn't be checked.

Web search is optional and off by default. Turn it on in **Settings → Chat & search** with your own SearXNG instance (no key; enable JSON output in its `settings.yml`) or a Brave Search API key, which is kept in your system keychain. Only Gemma's search words go to that service.

Pages are fetched from your machine, only from public addresses, and only when Gemma asks. What they say is treated as information, never as instructions. One answer uses at most five lookups.

# Models and providers

By default everything runs on **Ollama** on your machine: Gemma 4 12B judges items and writes briefs, Gemma 4 E4B is the fallback, and EmbeddingGemma finds near-duplicates. Settings → Providers shows each model role and which model fills it.

You can also connect LM Studio or any OpenAI-compatible server (llama.cpp, vLLM, Jan, LocalAI), or a cloud provider that speaks the same API: OpenAI or OpenRouter. Native Anthropic and Google Gemini support comes next. Cloud providers are off by default. If you pick one for a role, item text and your project profiles are sent to that provider, and the app marks that role "leaves this machine".

API keys are stored in your operating system's keychain (macOS Keychain, Windows Credential Manager or the Linux Secret Service). They are never written to `config.yaml`, the database or the logs, and the app only ever shows the first and last four characters. An environment variable such as `ANTHROPIC_API_KEY` overrides a stored key.

# Troubleshooting

- **"Ollama isn't running"**: start the Ollama app, or run `ollama serve`.
- **A source shows as rate-limited or failed**: the run skips it and carries on. It will be retried tomorrow night.
- **A quiet morning**: nothing cleared the 80 % bar. **Everything else** still has the whole night. If it happens often, add or broaden topics, add interests on the Projects page, or add sources.
- **Runs are slow**: lower `thresholds.max_scored` in `config.yaml`, or use Gemma 4 E4B as the judge.

# Command line

- `night-scout setup`: open the setup walkthrough in your browser
- `night-scout init`: write a starter config, scan projects, build profiles
- `night-scout run`: one nightly pass
- `night-scout serve`: the local web app on 127.0.0.1
- `night-scout schedule`: print or install the nightly job (launchd, cron or systemd)
- `night-scout profile`: rebuild the project profiles
