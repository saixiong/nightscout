# What Night Scout does

Night Scout reads the internet while you sleep and turns it into a short morning Digest of the topics you follow, and, if you like, into next steps for the projects you're working on.

Every night it fetches new items from your sources. Gemma files each one under one of **your topics** and scores how well it fits. In the morning you get two pages:

- **Digest** (opens first): the night's best stories for your topics, with a summary, key points and, for videos, the player right in the card. No projects needed.
- **Actions** (with projects): a handful of picks, each tied to one of your projects and labelled with what it means for that project.

Everything runs on your own machine with an open model (Gemma 4 by default). Nothing is sent anywhere unless you add a cloud provider yourself and pick it for a role.

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

# Run now

**▶ Run now** (on the Digest and on Actions) sends the scout out straight away instead of waiting for tonight. It reads your sources for anything new since the last run, and what it finds joins today's Digest and Actions; what's already there stays. Choose **Digest and Actions**, or **Digest only**, which is a few minutes faster but leaves Actions as it is (new items from it get no next steps). The box tells you about how long it takes, from your last runs. While it works you can keep using the app: the header shows how far it is, **Watch it ›** follows it stage by stage, and a message tells you when it's back. One run at a time.

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

Projects are optional; they power Actions. Add one in Setup (step 2): **📁 Choose folder…** opens Finder or File Explorer, or pick a suggested folder, or use **or type a path**. Choosing a folder only works in a browser on the computer running Night Scout; from another device, type the path. The Projects page is what the scout judges Actions against. For each project folder, Gemma reads the README and dependency files and writes a profile: purpose, stack, key dependencies, interests, and what you'd want to know about. Every item Gemma reads is scored against every project. Edit anything; your edits are what tomorrow's run uses. **Rebuild with Gemma** rewrites the profile from the folder again and overwrites your edits.

# Sources

Night Scout reads official feeds and public APIs, with no logins. The one exception is opt-in: searching a whole YouTube channel for your key terms (below) uses the YouTube Data API if you've added a key, otherwise yt-dlp, which reads YouTube's public pages.

- **Websites and blogs**: paste the address. Night Scout finds the site's RSS or Atom feed. Sites without a feed can't be followed.
- **YouTube channels**: paste the channel address or @handle. Transcripts are included, so Gemma judges what's said, not just the title.
- **Newsletters and podcasts**: anything with an RSS feed (Substack, Ghost, Buttondown, most podcasts).
- **Hacker News, Reddit, Bluesky, Mastodon, GitHub, arXiv, dev.to**: lists, keywords, subreddits, hashtags, releases and repo searches.

**Look back.** When you add a YouTube channel, website, newsletter, podcast, arXiv category or subreddit, choose how many days back the first night reads (1 to 14, default 3). It's also how far a run catches up after nights the scout didn't run. YouTube's feed only lists a channel's 15 newest videos, so a busy channel may not reach that far.

**Scout key terms.** Add comma-separated terms to the same sources (up to 10). A term matches a whole word or phrase in the title or description, in any case.
- *Once, when you add the source* (or add new terms with **Edit**): a YouTube channel is searched as a whole, up to 10 videos per term, any age. Night Scout keeps only the videos that really mention the term; YouTube's own search is loose. A website, newsletter or subreddit has its feed checked, any age; posts the feed no longer lists can't be searched.
- *Every night*: new posts that mention a term go to the front of Gemma's queue, so they're read even on a busy night. Matches beyond the run's limit go first the next night.

Add a YouTube Data API key in **Settings → Chat & search** to search channels through the official API (the free quota covers about 100 channel searches a day). The key is kept in your system keychain. Without one, Night Scout uses yt-dlp if it's installed (`pip install yt-dlp`).

- **X (Twitter)** isn't supported. X has no free public API and its terms don't allow scraping. Many of the same people also post on Bluesky or Mastodon.

# Asking Gemma

**Ask** in the top bar opens a chat with Gemma on any page: docked beside the Digest, floating elsewhere, full screen on a phone. Ask how Night Scout works, about your setup and past runs, about anything the scout has fetched on any night, or about anything else. Gemma looks things up before it answers: the Guide, your setup, every night's items, and the web tools below. When an answer mentions a page of the app, such as **Sources ›**, click it to go there.

**Ask about tonight** in the Digest, and **Ask** on each story, open the same chat about that Digest or that one story. From there, **anything else** switches to the general chat. Each lookup shows up in the drawer as it happens; click one to see what came back. Answers link the pages they used and say plainly when something couldn't be checked.

**Adding from chat.** Ask Gemma to follow a channel or to add a topic and it shows a card under its answer with **Add source** or **Add topic**, and **Not now**. Nothing changes until you click. A source is checked first, so a card only appears for one that resolves, labelled with its real name.

**Attachments.** Paste a screenshot into the question box, click 📎, or drop files onto the drawer: images, text files (up to 200 KB) and PDFs (up to 10 MB), up to four per question. Images are shrunk on your machine first, and a PDF's text is read on your machine. They go to the chat model with your question, so they leave your machine only if the chat model is a cloud provider. Click a thumbnail to see it full size, or the text Gemma read.

**＋ New chat** starts a fresh conversation; the drawer otherwise keeps one running conversation for each thing you ask about.

**History ▾** in the drawer lists every saved conversation, newest first, with what it was about. Type to search them, or click one to pick it up where you left off. **All chats & search** opens the Chats page: search every question and answer, filter to Night Scout, Digest, Stories or chats with files, read a conversation in full, and **Continue in chat**. To delete one, click 🗑 (or **Delete**) twice; you get a few seconds to **Undo**, then its messages and attached files are removed from your machine. Conversations are kept on your machine until you delete them.

Web search is optional and off by default. Turn it on in **Settings → Chat & search** with your own SearXNG instance (no key; enable JSON output in its `settings.yml`) or a Brave Search API key, which is kept in your system keychain. Only Gemma's search words go to that service.

Pages are fetched from your machine, only from public addresses, and only when Gemma asks. What they say is treated as information, never as instructions. One answer uses at most five lookups.

# Models and providers

By default everything runs on **Ollama** on your machine: Gemma 4 12B judges items and writes briefs, Gemma 4 E4B is the fallback, and EmbeddingGemma finds near-duplicates. The chat uses the Judge model. **Settings → AI Providers** shows each model role (Judge, Chat, Fallback, Embeddings) and which model fills it.

You can also connect LM Studio or any OpenAI-compatible server (llama.cpp, vLLM, Jan, LocalAI), or a cloud provider: **Anthropic** (its own API), **Google Gemini** (through Google's OpenAI-compatible endpoint), **OpenAI**, **OpenRouter**, and under "+ more" DeepSeek, Groq, Mistral and xAI. Add a key, then open **Models ▾** to see the provider's models, listed live, and tick the ones to offer for roles. A custom server can be edited or removed later; it's only saved after its connection test passes.

Cloud providers are off by default and are used only for the roles you pick. If you pick one, the app marks that role "Leaves this machine": for the Judge and Fallback, item text and your project profiles go to that provider; for the Chat, your questions, their context and the research tools' results do. Cloud calls bill to your account with that provider. Embeddings always stay on your machine. Removing a provider or its key puts its roles back on Gemma.

API keys are stored in your operating system's keychain (macOS Keychain, Windows Credential Manager or the Linux Secret Service). They are never written to `config.yaml`, the database or the logs, and the app only ever shows the first and last four characters. An environment variable such as `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `OPENAI_API_KEY` overrides a stored key.

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
