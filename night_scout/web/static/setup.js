/* Night Scout: the setup walkthrough (#/setup). Model → Projects → Sources → Interests →
   Schedule → Finish. Opens by itself on first launch; Settings → Setup and `night-scout setup`
   bring it back with the current settings filled in. Uses helpers from app.js (window.NS). */
(() => {
"use strict";
const { $, $$, esc, toast, go, wireTilt, chrome, api, failToast, fx, loading, errorView, badge } = NS;
const B = () => NS.B;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const STEPS = [
  ["model", "Model", "Where Gemma thinks"],
  ["projects", "Projects", "What you're building"],
  ["sources", "Sources", "What it reads overnight"],
  ["interests", "Interests", "What it looks for"],
  ["schedule", "Schedule", "When it goes out"],
  ["finish", "Finish", "First run"],
];
const CLOUD = { openrouter: ["OpenRouter", "⇄", "google/gemma-3-27b-it"], openai: ["OpenAI", "◎", "a model id from platform.openai.com/docs/models"] };
const TYPE = { youtube: "YouTube channels", rss: "RSS / Atom feeds", hackernews: "Hacker News", reddit: "Reddit", bluesky: "Bluesky", mastodon: "Mastodon", github: "GitHub" };
const ADD = [
  { k: "website", icon: "rss", name: "Website or blog", hint: "finds its RSS feed", ph: "https://example.com", discover: 1 },
  { k: "youtube", icon: "youtube", name: "YouTube channel", hint: "transcripts included", ph: "@fireship or a channel URL" },
  { k: "newsletter", icon: "rss", name: "Newsletter", hint: "Substack, Ghost, Buttondown", ph: "https://name.substack.com", discover: 1 },
  { k: "podcast", icon: "rss", name: "Podcast", hint: "show notes and titles", ph: "https://example.com/podcast.xml" },
  { k: "reddit", icon: "reddit", name: "Subreddit", hint: "public, no login", ph: "LocalLLaMA" },
  { k: "hackernews", icon: "hackernews", name: "Hacker News", hint: "keyword search", ph: "mcp" },
  { k: "bluesky", icon: "bluesky", name: "Bluesky", hint: "search or account", ph: "mcp server  or  @simonwillison.net" },
  { k: "mastodon", icon: "mastodon", name: "Mastodon", hint: "hashtag", ph: "#ollama" },
  { k: "github", icon: "github", name: "GitHub", hint: "releases or search", ph: "modelcontextprotocol/typescript-sdk" },
  { k: "arxiv", icon: "rss", name: "arXiv", hint: "paper category", ph: "cs.CL" },
  { k: "x", icon: "x", name: "X (Twitter)", hint: "not available", off: 1 },
];
const ISECS = [["interests", "Interests", "Topics Gemma looks for in every item."], ["would_want_to_know", "Would want to know about", "News that should always make the cut."], ["key_dependencies", "Key dependencies", "Releases and advisories for these come up as Changes or Issues."]];
const LISTS = ["stack", "key_dependencies", "interests", "would_want_to_know"];

/* ---------- state ---------- */
let D = null;  // loaded data: setup, providers, sources, projects (profiles), schedule
let U = null;  // walkthrough state
const runMins = () => [12, 19]; // typical on Gemma 4 12B; the Morning run view shows the real one

async function loadAll() {
  const [st, pv, src, pj, sc] = await Promise.all([api("GET", "/api/setup"), api("GET", "/api/providers"), api("GET", "/api/sources"), api("GET", "/api/projects"), api("GET", "/api/schedule")]);
  const bad = [st, pv, src, pj, sc].find(r => !r.ok);
  if (bad) return bad;
  D = { setup: st.data, pv: pv.data, src: src.data, pj: pj.data, sc: sc.data };
  return { ok: true };
}
const reload = async (...keys) => {
  const url = { setup: "/api/setup", pv: "/api/providers", src: "/api/sources", pj: "/api/projects", sc: "/api/schedule" };
  const rs = await Promise.all(keys.map(k => api("GET", url[k])));
  rs.forEach((r, i) => { if (r.ok) D[keys[i]] = r.data; });
};
function freshState() {
  const rerun = !D.setup.needed, j = B().judge || {};
  const pid = String(j.ref || "").split("/")[0];
  U = {
    rerun, step: 0, reached: rerun ? 5 : 0, dirty: {}, saved: {}, busy: false,
    where: j.local === false ? "cloud" : "local",
    cloud: { prov: CLOUD[pid] ? pid : "openrouter", model: j.local === false ? j.model : "", test: null },
    pulls: {}, olCheck: null, confirm: null, readQ: null, ipj: 0, addType: "website",
    time: D.sc.time, nightly: rerun ? D.sc.installed !== false : true, plist: false, go: "now",
  };
}
const step = () => STEPS[U.step][0];
// Projects, Sources and Interests save the moment you change them; Model's role and the
// Schedule save on Continue. The rail says "saved" or "changed" accordingly.
const INSTANT = ["projects", "sources", "interests"];
const mark = (...ks) => { (ks.length ? ks : [step()]).forEach(k => { U.dirty[k] = true; if (INSTANT.includes(k)) U.saved[k] = true; }); drawRail(); drawBar(); };

/* ---------- model helpers ---------- */
const ollama = () => D.pv.providers.find(p => p.id === "ollama");
const NEED = () => [[B().model.name, "Judge · Research"], [B().model.fallback, "Fallback"], [B().model.embed, "Embeddings"]];
const have = n => (ollama()?.models || []).some(m => m.name === n || m.name === `${n}:latest`);
const sizeOf = n => (ollama()?.models || []).find(m => m.name === n)?.size || "";
const cloudProv = () => D.pv.providers.find(p => p.id === U.cloud.prov);
const cloudModel = () => U.cloud.model || cloudProv()?.models?.[0]?.name || "";
const judgeWanted = () => U.where === "local" ? `ollama/${B().model.name}` : `${U.cloud.prov}/${cloudModel()}`;
const olUp = () => ollama()?.status === "connected";

/* ---------- summaries and gates ---------- */
const projRows = () => D.setup.projects;
const profiles = () => D.pj.projects;
function summary(k) {
  if (k === "model") return U.where === "local" ? (olUp() && NEED().every(([n]) => have(n)) ? `${B().model.name} · on this machine` : "Ollama · models to pull") : (cloudProv()?.key?.configured ? `${CLOUD[U.cloud.prov][0]} · leaves this machine` : "Cloud · key needed");
  if (k === "projects") return projRows().length ? projRows().map(p => p.name).join(", ") : "None yet";
  if (k === "sources") { const c = D.src.catalog; return `${c.filter(x => x.enabled !== false).length} of ${c.length} on`; }
  if (k === "interests") { const n = profiles().reduce((a, p) => a + p.interests.length, 0); return profiles().length ? `${n} interests · ${profiles().length} project${profiles().length > 1 ? "s" : ""}` : "Needs a project first"; }
  if (k === "schedule") return U.nightly ? `Every night at ${U.time}` : "Manual runs only";
  return U.go === "now" ? "Run once now" : U.nightly ? `Wait for ${U.time}` : "Not now";
}
function canNext() {
  const k = step();
  if (U.busy) return false;
  if (k === "model") return U.where === "local" ? olUp() && NEED().every(([n]) => have(n)) : !!(cloudProv()?.key?.configured && cloudModel() && olUp() && have(B().model.embed));
  if (k === "projects") return projRows().some(p => p.profiled) && projRows().every(p => p.profiled || !p.exists) && !U.readQ;
  return true;
}
function blockHint() {
  const k = step();
  if (k === "model") return !olUp() ? "Start Ollama, then press Check." : U.where === "local" ? "Pull the models to continue." : "Save a key and pull the embeddings model.";
  if (k === "projects") return U.readQ ? "Gemma is reading…" : projRows().length ? "Let Gemma read your projects to continue." : "Add a project to continue.";
  return "";
}

/* ---------- page ---------- */
NS.pages.setup = async () => {
  chrome("setup");
  if (!D) {
    loading("Loading setup…");
    const r = await loadAll();
    if (!r.ok) return errorView(r);
    freshState();
  }
  if ((location.hash.split("/")[1] || "") !== "setup") return;
  render();
};
NS.setupReset = () => { D = null; U = null; };
// Leaving Setup drops its state, so coming back (Settings → Setup) starts from what is saved now.
window.addEventListener("hashchange", () => { if ((location.hash.split("/")[1] || "") !== "setup") NS.setupReset(); });

function render() {
  $("#app").innerHTML = `<div class="frame">
    <div class="col-left">
      <section class="hero">
        <p class="eyebrow">${U.rerun ? "Setup · again" : "Welcome · Setup"}</p>
        <h1>${U.rerun ? "Tune your<br><em>scout.</em>" : "Get the scout<br><em>ready.</em>"}</h1>
        <p class="sub">${U.rerun ? "Your current settings are filled in. Change what you want and skip the rest." : "Six short steps. Everything stays on this machine unless you choose a cloud model."}</p>
      </section>
      <div class="progress-strip" id="strip"></div><span class="strip-cap" id="stripCap"></span>
      <ol class="steps-rail" id="rail"></ol>
      <p class="rail-foot">Run this again any time from <b>Settings → Setup</b> or <code>night-scout setup</code>. Every step is also its own page (Projects, Sources, Settings).</p>
    </div>
    <div class="stage" id="stage"></div>
  </div>`;
  drawRail(); drawCard(true);
}
function drawRail() {
  const rail = $("#rail"); if (!rail) return;
  rail.innerHTML = STEPS.map(([k, t], i) => {
    const locked = i > U.reached, cur = i === U.step, done = !cur && !locked && (U.saved[k] || U.rerun || i < U.reached);
    const st = U.rerun && U.saved[k] ? `<span class="st saved">saved</span>` : U.rerun && U.dirty[k] ? `<span class="st changed">changed</span>` : !U.rerun && U.saved[k] ? `<span class="st saved">saved</span>` : "";
    return `<li class="srail ${cur ? "cur" : ""} ${locked ? "locked" : ""} ${done ? "done" : ""}" data-i="${i}">
      <span class="n">${done ? "✓" : i + 1}</span><div><b>${esc(t)}</b><small>${esc(locked ? STEPS[i][2] : summary(k))}</small></div>${st}</li>`;
  }).join("");
  $$(".srail", rail).forEach(li => li.onclick = () => { const i = +li.dataset.i; if (i <= U.reached && i !== U.step && !U.busy) goStep(i); });
  $("#strip").innerHTML = STEPS.map((_, i) => `<i class="${i < U.step ? "done" : i === U.step ? "cur" : ""}"></i>`).join("");
  $("#stripCap").textContent = `Step ${U.step + 1} of 6 · ${STEPS[U.step][1]}`;
}
function goStep(i) { U.step = i; U.reached = Math.max(U.reached, i); U.confirm = null; drawRail(); drawCard(); }
function drawCard(first) {
  const stage = $("#stage"); if (!stage) return;
  const old = $(".big", stage);
  const card = document.createElement("div");
  card.className = "big setup-card enter";
  card.innerHTML = `<div class="big-scroll"><div id="sbody" class="sbody"></div><div class="stepbar" id="sbar"></div></div>`;
  if (old && !first) {
    $$("[id]", old).forEach(el => el.removeAttribute("id")); // the leaving card mustn't catch lookups
    old.classList.remove("enter"); old.classList.add("leave"); setTimeout(() => old.remove(), 450);
  } else if (old) old.remove();
  stage.appendChild(card);
  wireTilt(stage);
  drawBody(); drawBar();
}
function drawBar() {
  const bar = $("#sbar"); if (!bar) return;
  const k = step(), last = U.step === 5, ok = canNext();
  const pending = (k === "model" && judgeWanted() !== D.pv.roles?.judge) || (k === "schedule" && (U.dirty.schedule || !U.rerun) && !U.saved.schedule);
  const label = last ? (U.rerun ? "Done" : U.go === "now" ? "Finish and run now" : "Finish setup") : pending ? "Save & continue" : "Continue";
  const hint = ok ? (U.rerun && !U.dirty[k] && !last ? "No changes on this step." : "") : blockHint();
  bar.innerHTML = `${U.step > 0 ? `<button class="btn ghost" id="sBack">‹ Back</button>` : `<button class="btn ghost" id="sExit">${U.rerun ? "Exit setup" : "Skip setup for now"}</button>`}
    <span class="hint">${esc(hint)}</span><span class="spacer"></span>
    ${!last && !["model", "projects"].includes(k) ? `<button class="btn ghost" id="sSkip">${U.rerun ? "Keep as is" : "Skip this step"}</button>` : ""}
    <button class="btn primary" id="sNext" ${ok ? "" : "disabled"}>${esc(label)}${last ? "" : " ›"}</button>`;
  $("#sBack") && ($("#sBack").onclick = () => goStep(U.step - 1));
  $("#sExit") && ($("#sExit").onclick = () => { sessionStorage.setItem("nsSetupSkipped", "1"); go("#/morning"); });
  $("#sSkip") && ($("#sSkip").onclick = () => { if (k === "schedule") { U.time = D.sc.time; U.nightly = U.rerun ? D.sc.installed !== false : U.nightly; U.saved.schedule = U.rerun; delete U.dirty.schedule; } goStep(U.step + 1); });
  $("#sNext").onclick = next;
}
async function next() {
  if (!canNext()) return;
  const k = step();
  U.busy = true; drawBar();
  try {
    if (k === "model" && judgeWanted() !== D.pv.roles?.judge) {
      const r = await api("PUT", "/api/roles", { judge: judgeWanted() });
      if (!r.ok) return failToast(r, "Choosing models");
      await Promise.all([reload("pv"), NS.loadBoot()]); chrome("setup");
      U.saved.model = true;
      if (U.where === "cloud") toast(`<b>Heads up:</b> judging now sends item text and project profiles to ${esc(CLOUD[U.cloud.prov][0])}.`);
    }
    if (k === "schedule" && (U.dirty.schedule || !U.rerun)) {
      const r = await api("PUT", "/api/schedule", { time: U.time, nightly: U.nightly });
      if (!r.ok) return failToast(r, "Scheduling");
      await Promise.all([reload("sc"), NS.loadBoot()]); chrome("setup"); U.saved.schedule = true;
      const msg = r.data.message || "";
      toast(/said:|isn't supported/.test(msg) ? `<b>Schedule saved, but:</b> ${esc(msg)}` : U.nightly ? `<b>Scheduled.</b> The scout goes out every night at ${esc(U.time)}.` : "<b>Saved.</b> No nightly run.");
    }
    if (U.rerun && U.dirty[k] && !["model", "schedule"].includes(k)) U.saved[k] = true;
    if (!U.rerun) U.saved[k] = true;
    if (k === "finish") return await finish();
    U.busy = false; goStep(U.step + 1);
  } finally { U.busy = false; drawBar(); }
}
function drawBody() {
  const k = step();
  const head = (title, lead) => `<div class="big-head"><span class="step-k">Step ${U.step + 1} of 6 · ${esc(STEPS[U.step][1])}</span></div>
    <h2 class="serif">${title}</h2><p class="step-lead">${lead}</p>
    ${U.rerun && k !== "finish" ? `<div class="rerun-note">↺ <span>These are your current settings. Change anything, or press <b>Continue</b> to keep them. Nothing is removed unless you remove it.</span></div>` : ""}`;
  ({ model: stepModel, projects: stepProjects, sources: stepSources, interests: stepInterests, schedule: stepSchedule, finish: stepFinish })[k](head);
}
const here = () => (B().platform === "darwin" ? "this Mac" : "this machine");
const keychain = () => ({ darwin: "macOS Keychain", win32: "Windows Credential Manager" }[B().platform] || "system keychain");

/* ---------- 1. model ---------- */
function modelRows(list) {
  return list.map(([n, role]) => {
    const p = U.pulls[n], ok = have(n);
    return `<li><span class="ic ${ok ? "good" : p ? "now" : "wait"}">${ok ? "✓" : p ? "◌" : "○"}</span>
      <span><span class="mono">${esc(n)}</span> <span class="muted small">· ${esc(role)}</span></span>
      <span class="r">${ok ? `installed${sizeOf(n) ? " · " + esc(sizeOf(n)) : ""}` : p ? `<span class="meter"><i data-w="${Math.round(p.percent || 0)}"></i></span>${p.percent != null ? Math.round(p.percent) + "%" : esc(p.status || "starting")}` : olUp() ? `<button class="btn ghost" data-pull="${esc(n)}">Pull</button>` : ""}</span></li>`;
  }).join("");
}
function stepModel(head) {
  const body = $("#sbody"); const ol = ollama();
  const missing = NEED().filter(([n]) => !have(n) && !U.pulls[n]);
  const olCard = `<section class="pcard prov"><div class="prow"><span class="tile">🦙</span><div class="pmain"><div class="pname">Ollama <span class="badge local">Local</span>${olUp() ? `<span class="badge ok">Connected</span>` : U.olCheck === "now" ? `<span class="badge off">Checking…</span>` : `<span class="badge off">Not running</span>`}</div>
      <div class="muted small mono">${esc(ol?.base_url || B().model.host)}${ol?.version ? ` · ollama ${esc(ol.version)}` : ""}</div></div>
      <button class="btn ghost" id="olCheck">${olUp() ? "Check again" : "Check"}</button></div>
    ${olUp() ? "" : `<p class="muted small m-8">Not installed? Get it from <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer">ollama.com/download</a>, open it, then press Check.</p>`}
    ${olUp() ? `<ul class="clist">${modelRows(U.where === "local" ? NEED() : NEED().slice(2))}</ul>` : ""}
    ${olUp() && U.where === "local" && missing.length > 1 ? `<div class="row"><button class="btn" id="pullAll">Pull all ${missing.length}</button><span class="muted small">Several GB; it can take a while. You can leave this page open.</span></div>` : ""}
  </section>`;
  const cp = cloudProv(), [cname, ctile, cph] = CLOUD[U.cloud.prov], key = cp?.key?.configured ? cp.key : null;
  const cloudCard = `<section class="pcard prov"><div class="prow"><span class="tile">${ctile}</span><div class="pmain">
      <div class="pname">${esc(cname)} ${key ? `<span class="badge ok">Configured</span>` : `<span class="badge off">Not configured</span>`}</div>
      <div class="muted small">Anthropic and Gemini get native clients after the contest; OpenRouter reaches many of the same models today.</div></div>
      <div class="seg" id="cprov">${Object.entries(CLOUD).map(([id, [n]]) => `<button data-p="${id}" class="${U.cloud.prov === id ? "on" : ""}">${esc(n)}</button>`).join("")}</div></div>
    ${key ? `<div class="small mt10"><span class="keypill mono">${esc(key.masked)}</span> <span class="muted">${key.source === "env" ? "from an environment variable" : `🔒 in ${esc(keychain())}`}</span></div>
       <div class="addrow"><input type="text" id="cModel" value="${esc(cloudModel())}" placeholder="Model for judging, e.g. ${esc(cph)}" autocomplete="off"><button class="btn ghost" id="cTest">Test</button></div>
       <div id="cTestOut" class="mt10"></div>`
      : `<div class="keyform"><div class="keyin"><input type="password" id="keyVal" placeholder="Paste your ${esc(cname)} API key" autocomplete="off"><button class="eye" id="eye" title="Show">👁</button></div>
       <input type="text" id="keyModel" placeholder="Model for judging, e.g. ${esc(cph)}" autocomplete="off">
       <div class="row mt0"><button class="btn primary" id="keySave">Save to ${esc(keychain())} &amp; test</button><span class="muted small">Never written to config.yaml, the logs or the sandbox.</span></div></div>`}
  </section>
  <div class="supply mt12"><strong>Leaves this machine:</strong> item text and your project profiles go to ${esc(cname)} for judging. Embeddings stay local, so Ollama with <span class="mono">${esc(B().model.embed)}</span> is still needed.</div>`;
  body.innerHTML = head("Where should Gemma <em>think?</em>", "Night Scout reads every item against your projects with a language model. Local is free and private; a cloud model can be faster on a small machine.") + `
    <div class="choices">
      <button class="choice ${U.where === "local" ? "on" : ""}" data-where="local"><span class="tile">🦙</span><b>On ${here()} <span class="rec">RECOMMENDED</span></b><small>Ollama runs Gemma locally. Nothing leaves your machine.</small><span class="pros">free · private · works offline</span></button>
      <button class="choice ${U.where === "cloud" ? "on" : ""}" data-where="cloud"><span class="tile">↗</span><b>Cloud provider</b><small>Use an OpenRouter or OpenAI key. Item text is sent to them.</small><span class="pros">no big download · pay per use</span></button>
    </div>
    ${U.where === "local" ? olCard : cloudCard + olCard}
    <p class="muted small mt16">Fine-tune every model role later in <a href="#/settings/providers">Settings → AI Providers</a>.</p>`;
  fx(body);
  $$(".choice", body).forEach(b => b.onclick = () => { if (U.where === b.dataset.where) return; U.where = b.dataset.where; mark(); drawBody(); });
  $("#olCheck").onclick = async () => { U.olCheck = "now"; drawBody(); await reload("pv"); U.olCheck = null; drawBody(); drawBar(); drawRail(); };
  $$("[data-pull]", body).forEach(b => b.onclick = () => pull(b.dataset.pull));
  $("#pullAll") && ($("#pullAll").onclick = () => missing.forEach(([n]) => pull(n)));
  $$("#cprov button").forEach(b => b.onclick = () => { U.cloud.prov = b.dataset.p; U.cloud.model = ""; mark(); drawBody(); });
  $("#eye") && ($("#eye").onclick = () => { const i = $("#keyVal"); i.type = i.type === "password" ? "text" : "password"; });
  $("#cModel") && ($("#cModel").onchange = e => { U.cloud.model = e.target.value.trim(); mark(); });
  $("#cTest") && ($("#cTest").onclick = () => testCloud());
  $("#keySave") && ($("#keySave").onclick = async () => {
    const input = $("#keyVal"), v = input.value.trim(), model = $("#keyModel").value.trim();
    if (v.length < 12) return toast("That doesn't look like an API key.");
    if (!model) return toast("Add the model to judge with, too.");
    const r = await api("PUT", `/api/keys/${encodeURIComponent(U.cloud.prov)}`, { key: v, model });
    input.value = "";
    if (!r.ok) return failToast(r, "Saving keys");
    U.cloud.model = model; mark(); await reload("pv"); drawBody(); drawBar(); drawRail(); testCloud();
  });
}
async function testCloud() {
  const out = $("#cTestOut"); if (!out) return;
  out.innerHTML = `<div class="checks"><span class="now">◌ Health</span><span>○ Auth</span><span>○ Chat</span></div>`;
  const r = await api("POST", `/api/providers/${encodeURIComponent(U.cloud.prov)}/test`, {});
  if (!r.ok) { out.innerHTML = ""; return failToast(r, "Testing providers"); }
  const steps = r.data?.steps || [], failed = steps.find(s => !s.ok);
  if (!$("#cTestOut")) return;
  $("#cTestOut").innerHTML = `<div class="checks">${steps.map(s => `<span class="${s.ok ? "good" : "bad"}">${s.ok ? "✓" : "✕"} ${esc(s.name)}</span>`).join("")}</div>` +
    (failed ? `<p class="small neg testmsg">Test failed at ${esc(failed.name)}: ${esc(failed.detail || "")}</p>` : `<p class="small testmsg good">Connection test passed.</p>`);
}
async function pull(n) {
  if (U.pulls[n]) return;
  U.pulls[n] = { percent: 0, status: "starting" }; if (step() === "model") drawBody();
  const r = await api("POST", "/api/ollama/pull", { name: n });
  if (!r.ok) { delete U.pulls[n]; if (step() === "model") drawBody(); return failToast(r, "Pulling models"); }
  for (;;) {
    await sleep(1000);
    const s = await api("GET", `/api/ollama/pull?name=${encodeURIComponent(n)}`), d = s.data || {};
    if (d.state === "error") { delete U.pulls[n]; toast(`<b>Pull failed.</b> ${esc(d.error || "")}`); break; }
    if (d.state === "done") { delete U.pulls[n]; await reload("pv"); toast(`<b>Pulled ${esc(n)}.</b>`); mark("model"); break; }
    U.pulls[n] = { percent: d.percent, status: d.status };
    if (step() === "model") { drawBody(); }
  }
  if (step() === "model") { drawBody(); drawBar(); } drawRail();
}

/* ---------- 2. projects ---------- */
function stepProjects(head) {
  const body = $("#sbody");
  const rows = projRows(), prof = Object.fromEntries(profiles().map(p => [p.name, p]));
  const unread = rows.filter(p => !p.profiled && p.exists);
  body.innerHTML = head("Which projects should it <em>scout for?</em>", "Add the folders of things you're building. Gemma reads each one (README, manifests, file types; never your secrets) and writes a short profile you can edit.") + `
    <div class="projlist">${rows.length ? rows.map(p => {
      const reading = U.readQ === p.name, pp = prof[p.name], conf = U.confirm;
      const status = reading ? `<span class="badge off">Gemma is reading…</span>` : !p.exists ? `<span class="badge off">Folder missing</span>` : p.profiled ? `<span class="badge ok">Profiled</span>` : `<span class="badge off">Not read yet</span>`;
      return `<div class="pj ${reading ? "reading" : ""}"><div class="pjn">${esc(p.name)} ${status}</div>
        <div class="pja">${p.profiled && U.rerun && !reading ? `<button class="btn ghost" data-reread="${esc(p.name)}">${conf === "re:" + p.name ? "Replace its profile?" : "Re-read"}</button>` : ""}
          <button class="btn ghost danger" data-rm="${esc(p.name)}" ${U.readQ ? "disabled" : ""}>${conf === "rm:" + p.name ? "Remove? Click again" : "Remove"}</button></div>
        <div class="pjp">${esc(p.path)}</div>
        ${p.profiled && pp ? `<div class="pjw">${esc(pp.purpose)}</div><div class="chips">${pp.stack.map(s => `<span class="chip">${esc(s)}</span>`).join("")}</div>` : p.exists ? `<div class="found">✓ found ${esc(p.found.join(" · "))}</div>` : ""}
      </div>`; }).join("") : `<div class="empty-box">No projects yet. Add a folder below; one is enough to start.</div>`}</div>
    <div class="addrow"><input type="text" id="pjPath" list="pjSugg" placeholder="Folder path, e.g. ~/projects/my-app" autocomplete="off"><button class="btn" id="pjAdd">Add folder</button></div>
    <datalist id="pjSugg">${D.setup.suggestions.map(s => `<option value="${esc(s.path)}">`).join("")}</datalist>
    ${D.setup.suggestions.length ? `<div class="chips mt10"><span class="muted small">Found on ${here()}:</span>${D.setup.suggestions.slice(0, 8).map(s => `<button class="chip add" data-add="${esc(s.path)}">+ ${esc(s.name)}</button>`).join("")}</div>` : ""}
    ${unread.length && !U.readQ ? `<div class="row"><button class="btn primary" id="readAll">✦ Read ${unread.length === 1 ? "it" : `${unread.length} projects`} with Gemma</button><span class="muted small">About 20–40 s each with a local model.</span></div>` : ""}
    <p class="muted small mt16">${U.rerun ? "Re-reading a project replaces its profile, including your edits to it. Removing one drops it from the scout; its folder isn't touched." : "Profiles are plain YAML (profile.yaml). You'll review the interests in step 4."}</p>`;
  const add = async v => {
    if (!v) return $("#pjPath").focus();
    const r = await api("POST", "/api/projects/add", { path: v });
    if (!r.ok) return toast(`<b>Not added.</b> ${esc(r.error)}`);
    await reload("setup"); mark("projects"); drawBody(); drawRail(); drawBar();
  };
  $("#pjAdd").onclick = () => add($("#pjPath").value.trim());
  $("#pjPath").onkeydown = e => { if (e.key === "Enter") $("#pjAdd").click(); };
  $$("[data-add]", body).forEach(b => b.onclick = () => add(b.dataset.add));
  $$("[data-rm]", body).forEach(b => b.onclick = async () => {
    const n = b.dataset.rm;
    if (U.confirm !== "rm:" + n) { U.confirm = "rm:" + n; drawBody(); setTimeout(() => { if (U.confirm === "rm:" + n) { U.confirm = null; if (step() === "projects") drawBody(); } }, 4000); return; }
    U.confirm = null;
    const wasProfiled = projRows().find(p => p.name === n)?.profiled;
    const r = await api("DELETE", `/api/projects/${encodeURIComponent(n)}`);
    if (!r.ok) return failToast(r, "Removing projects");
    await reload("setup", "pj"); mark("projects", ...(wasProfiled ? ["interests"] : [])); drawBody(); drawRail(); drawBar();
    toast(`<b>${esc(n)}</b> removed from the scout. Its folder isn't touched.`);
  });
  $$("[data-reread]", body).forEach(b => b.onclick = () => {
    const n = b.dataset.reread;
    if (U.confirm !== "re:" + n) { U.confirm = "re:" + n; drawBody(); return; }
    U.confirm = null; readProjects([n]);
  });
  $("#readAll") && ($("#readAll").onclick = () => readProjects(unread.map(p => p.name)));
}
async function readProjects(names) {
  let done = 0;
  for (const n of names) {
    U.readQ = n; if (step() === "projects") { drawBody(); drawBar(); }
    const r = await api("POST", `/api/projects/${encodeURIComponent(n)}/read`, {});
    if (!r.ok) { failToast(r, "Reading projects"); continue; }
    let s = r.data;
    while (s.state === "running") { await sleep(1200); const q = await api("GET", `/api/projects/${encodeURIComponent(n)}/read`); if (q.ok) s = q.data; }
    if (s.state === "error") toast(`<b>Couldn't read ${esc(n)}.</b> ${esc(s.error || "")}`); else done++;
    await reload("setup", "pj");
  }
  U.readQ = null; mark("projects", "interests");
  if (step() === "projects") { drawBody(); drawBar(); } drawRail();
  if (done) toast(`<b>Profiled.</b> Gemma read ${done} project${done > 1 ? "s" : ""}.`);
}

/* ---------- 3. sources ---------- */
function stepSources(head) {
  const body = $("#sbody");
  const groups = {}; D.src.catalog.forEach(r => (groups[r.type] ||= []).push(r));
  const t = ADD.find(x => x.k === U.addType);
  body.innerHTML = head("What should it <em>read</em> overnight?", `${U.rerun ? "These are the sources the scout reads now." : "A starter set for local-AI and developer news is on."} Switch off what you don't want and add your own. Official feeds and public APIs only: no logins, no scraping.`) + `
    <div class="src-compact">${Object.entries(groups).map(([ty, list]) => `<div class="group"><div class="group-h">${badge(ty, 1)}<h4>${esc(TYPE[ty] || ty)}</h4><span class="muted">${list.filter(r => r.enabled !== false).length} of ${list.length} on</span></div>
      ${list.map(r => `<div class="feed ${r.enabled === false ? "off" : ""}"><div><div>${esc(r.label)}</div><div class="what">${esc(r.detail)}</div></div>
        <button class="toggle ${r.enabled === false ? "" : "on"}" data-k="${esc(r.key)}" title="${r.enabled === false ? "Off" : "On"}"></button></div>`).join("")}</div>`).join("")}</div>
    <div class="group" id="addSrc"><h4 class="sec-h">Add a source</h4>
      <div class="types">${ADD.map(a => `<button class="type ${U.addType === a.k ? "on" : ""} ${a.off ? "off" : ""}" data-t="${a.k}">${badge(a.icon)}<b>${esc(a.name)}</b><small>${esc(a.hint)}</small></button>`).join("")}</div>
      ${t.off ? `<div class="supply mt12"><strong>Not available.</strong> X has no free public API and its terms don't allow scraping. Many of the same people post on Bluesky or Mastodon; add them there.</div>`
        : `<div class="addform"><input type="text" id="aVal" placeholder="${esc(t.ph)}"><button class="btn primary" id="aGo">Add ${esc(t.name.toLowerCase())}</button></div>`}
    </div>`;
  $$(".toggle", body).forEach(b => b.onclick = async () => {
    const it = D.src.catalog.find(x => x.key === b.dataset.k); if (!it) return;
    const want = it.enabled === false;
    const r = await api("POST", "/api/sources/toggle", { key: it.key, enabled: want });
    if (!r.ok) return failToast(r, "Turning sources on and off");
    it.enabled = want; mark(); drawBody();
  });
  $$(".type", body).forEach(b => b.onclick = () => { U.addType = b.dataset.t; drawBody(); $("#aVal")?.focus(); });
  const goBtn = $("#aGo");
  if (goBtn) {
    const add = async () => {
      const v = $("#aVal").value.trim(); if (!v) return $("#aVal").focus();
      goBtn.disabled = true; if (t.discover) goBtn.textContent = "Looking for a feed…";
      const r = await api("POST", "/api/sources/add", { type: t.k, value: v });
      if (!r.ok) { goBtn.disabled = false; goBtn.textContent = `Add ${t.name.toLowerCase()}`; return toast(`<b>Not added.</b> ${esc(r.error)}`); }
      await reload("src"); mark(); drawBody(); drawRail();
      toast(`<b>Added.</b> ${esc(r.data?.label || v)} · first fetch on the next run.`);
    };
    goBtn.onclick = add; $("#aVal").onkeydown = e => { if (e.key === "Enter") add(); };
  }
}

/* ---------- 4. interests ---------- */
function stepInterests(head) {
  const body = $("#sbody"), P = profiles();
  if (!P.length) { body.innerHTML = head("What should it <em>look for?</em>", "Interests come from your project profiles.") + `<div class="empty-box">Add a project in step 2 first.</div>`; return; }
  U.ipj = Math.min(U.ipj, P.length - 1);
  const p = P[U.ipj];
  body.innerHTML = head("What should it <em>look for?</em>", "Gemma found these while reading your projects. Remove anything off-target and add what it missed; every item is scored against this.") + `
    <div class="row mt0 mb14"><div class="seg" id="ipj">${P.map((x, i) => `<button data-i="${i}" class="${i === U.ipj ? "on" : ""}">${esc(x.name)}</button>`).join("")}</div><span class="muted small">${esc(p.purpose)}</span></div>
    ${ISECS.map(([k, t, d]) => `<section class="isec"><h4 class="sec-h">${esc(t)} <span class="by">· ${p.generated_by === "heuristic" ? "from the manifests" : "found by Gemma"}</span></h4><p class="sub2">${esc(d)}</p>
      <div class="chips">${p[k].map((v, i) => `<span class="chip ${k === "key_dependencies" ? "mono" : ""}">${esc(v)}<button class="x" data-k="${k}" data-i="${i}" title="Remove">✕</button></span>`).join("")}
      <label class="chip add">+ <input type="text" data-add="${k}" placeholder="${k === "key_dependencies" ? "package name" : "add a topic"}" maxlength="80"></label></div></section>`).join("")}
    <p class="muted small">The <a href="#/projects">Projects</a> page has the same editor, and profile.yaml is plain YAML if you'd rather edit it there.</p>`;
  $$("#ipj button").forEach(b => b.onclick = () => { U.ipj = +b.dataset.i; drawBody(); });
  const save = async () => {
    const r = await api("PUT", `/api/projects/${encodeURIComponent(p.name)}`, { purpose: p.purpose, ...Object.fromEntries(LISTS.map(k => [k, p[k]])) });
    if (!r.ok) { failToast(r, "Saving interests"); await reload("pj"); drawBody(); return false; }
    mark(); return true;
  };
  $$(".x", body).forEach(b => b.onclick = async () => {
    const k = b.dataset.k, i = +b.dataset.i, v = p[k][i];
    p[k].splice(i, 1); drawBody();
    if (await save()) toast(`Removed <b>${esc(v)}</b>.`, async () => { p[k].splice(i, 0, v); drawBody(); await save(); drawBody(); });
  });
  $$("[data-add]", body).forEach(inp => inp.onkeydown = async e => {
    if (e.key !== "Enter") return;
    const v = inp.value.trim(), k = inp.dataset.add; if (!v || p[k].includes(v)) return;
    p[k].push(v); drawBody(); await save(); $(`[data-add="${k}"]`)?.focus();
  });
}

/* ---------- 5. schedule ---------- */
function stepSchedule(head) {
  const body = $("#sbody"), sc = D.sc;
  const [h, m] = U.time.split(":").map(Number), [lo, hi] = runMins();
  const ready = (() => { const t = (h * 60 + m + hi + 2) % 1440; return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; })();
  const how = sc.via === "launchd" ? `Installed as a macOS launchd job for your user only. No admin password. Low priority, so it won't slow anything down.` : B().platform.startsWith("linux") ? "Installed as a systemd user timer (or shown as a cron line if systemd isn't there)." : "On this system, `night-scout schedule` prints the steps to add it yourself.";
  body.innerHTML = head("When should it <em>go out?</em>", "The scout runs once a night while you sleep and leaves a digest for the morning. It only proposes; nothing it suggests runs on its own.") + `
    <div class="seg mb14" id="nightly"><button data-v="1" class="${U.nightly ? "on" : ""}">Every night</button><button data-v="0" class="${U.nightly ? "" : "on"}">I'll run it myself</button></div>
    ${U.nightly ? `<div class="timepick"><input type="text" id="time" value="${esc(U.time)}" inputmode="numeric" maxlength="5" aria-label="Run time, 24-hour" title="24-hour time, like 04:30"><div class="quick">${["01:00", "02:30", "04:30", "06:00"].map(t => `<button data-t="${t}" class="${t === U.time ? "on" : ""}">${t}</button>`).join("")}</div></div>
      <ul class="facts2">
        <li><span>⏱</span><span>A run usually takes ${lo}–${hi} min with Gemma 4 12B, so the digest is ready by about ${esc(ready)}.</span></li>
        ${sc.via === "launchd" ? `<li><span>💤</span><span>If the Mac is asleep at ${esc(U.time)}, macOS runs it as soon as it wakes.</span></li>` : ""}
        <li><span>🦙</span><span>Ollama has to be running then (for embeddings, even with a cloud judge): keep the Ollama app open, or set it to open at login.</span></li>
        <li><span>👤</span><span>${esc(how)}</span></li>
      </ul>
      ${sc.plist ? `<button class="btn ghost mt16" id="plistBtn">${U.plist ? "Hide" : "Show"} the launchd job</button>${U.plist ? `<pre class="plist">${esc(sc.plist_path)}\n\n${esc(sc.plist.replace(/<key>Hour<\/key>\s*<integer>\d+<\/integer>/, `<key>Hour</key>\n\t\t<integer>${h}</integer>`).replace(/<key>Minute<\/key>\s*<integer>\d+<\/integer>/, `<key>Minute</key>\n\t\t<integer>${m}</integer>`))}</pre>` : ""}` : ""}`
    : `<div class="pcard"><b>No schedule.</b><p class="muted small m-4">Run it whenever you like with <code>night-scout run</code>${sc.installed ? ". Saving this removes the nightly job." : "."} Turn the nightly run on again here any time.</p></div>`}`;
  $$("#nightly button").forEach(b => b.onclick = () => { U.nightly = b.dataset.v === "1"; U.saved.schedule = false; mark(); drawBody(); });
  $("#time") && ($("#time").onchange = e => {
    const mm = e.target.value.trim().match(/^([01]?\d|2[0-3]):?([0-5]\d)$/);
    if (!mm) { e.target.value = U.time; return toast("Use 24-hour time, like 04:30 or 23:15."); }
    U.time = `${mm[1].padStart(2, "0")}:${mm[2]}`; U.saved.schedule = false; mark(); drawBody();
  });
  $$(".quick button", body).forEach(b => b.onclick = () => { U.time = b.dataset.t; U.saved.schedule = false; mark(); drawBody(); });
  $("#plistBtn") && ($("#plistBtn").onclick = () => { U.plist = !U.plist; drawBody(); });
}

/* ---------- 6. finish ---------- */
function stepFinish(head) {
  const body = $("#sbody"), [lo, hi] = runMins();
  const changed = STEPS.slice(0, 5).filter(([k]) => U.dirty[k] || U.saved[k]);
  body.innerHTML = head(U.rerun ? "Review your <em>changes.</em>" : "Ready for <em>tonight.</em>", U.rerun ? (changed.length ? `You changed ${changed.length} step${changed.length > 1 ? "s" : ""}, already saved. Everything else stays as it was.` : "Nothing changed. Your scout keeps its current setup.") : "Here's what you set up. Edit any step, then send the scout out.") + `
    <div class="sumlist">${STEPS.slice(0, 5).map(([k, t], i) => `<div class="sumrow"><span class="ok">✓</span><b>${esc(t)}</b><span class="v">${esc(summary(k))}${U.rerun && (U.dirty[k] || U.saved[k]) ? ' <span class="badge ok">saved just now</span>' : ""}</span><a href="#/setup" data-i="${i}">Edit</a></div>`).join("")}</div>
    <h4 class="sec-h">${U.rerun ? "Now" : "First run"}</h4>
    <div class="golist">
      <button class="choice ${U.go === "now" ? "on" : ""}" data-g="now"><span class="tile">🔭</span><b>Run the scout now</b><small>${U.rerun ? "See a digest with your setup" : "See your first digest"} in about ${lo}–${hi} minutes. Watch it work, or close the tab.</small><span class="pros">${U.rerun ? "try the changes now" : "recommended for a first look"}</span></button>
      <button class="choice ${U.go === "wait" ? "on" : ""}" data-g="wait"><span class="tile">🌙</span><b>${U.nightly ? `Wait for ${esc(U.time)}` : "Not now"}</b><small>${U.nightly ? (U.rerun ? "Tonight's run uses your setup." : "Your first digest is on Morning when you wake up.") : "Run it later with night-scout run."}</small><span class="pros">${U.nightly ? "nothing to wait for" : "manual runs"}</span></button>
    </div>
    <p class="again">↺ Come back any time: <b>Settings → Setup</b>, or <code>night-scout setup</code> in a terminal. It opens with your settings filled in.</p>`;
  $$(".sumrow a", body).forEach(a => a.onclick = e => { e.preventDefault(); goStep(+a.dataset.i); });
  $$(".golist .choice", body).forEach(b => b.onclick = () => { U.go = b.dataset.g; drawBody(); drawBar(); drawRail(); });
}
async function finish() {
  sessionStorage.removeItem("nsSetupSkipped");
  if (U.go === "now") {
    const r = await api("POST", "/api/run", {});
    if (!r.ok) return failToast(r, "Starting a run");
  }
  await NS.loadBoot();
  const now = U.go === "now";
  NS.setupReset();
  if (now) { toast("<b>The scout is out.</b> Your digest lands on Morning when it's done."); await NS.kickRun(); }
  else { toast(U.nightly ? `<b>All set.</b> The scout goes out at ${esc(B().schedule.time)}.` : "<b>All set.</b>"); go("#/morning"); }
}
})();
