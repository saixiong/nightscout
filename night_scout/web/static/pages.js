/* Night Scout: Guide + tour, Settings → AI Providers. Uses helpers from app.js (window.NS). */
(() => {
"use strict";
const { $, $$, esc, md, toast, go, wireTilt, chrome, api, failToast, fx, loading, errorView } = NS;
const B = () => NS.B;

/* ================= GUIDE + TOUR ================= */
const slug = t => t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
let GUIDE = null;
NS.pages.guide = sec => {
  chrome("guide");
  if (GUIDE === null) {
    loading("Loading the guide…");
    api("GET", "/api/guide").then(r => { if (!r.ok) return errorView(r); GUIDE = r.data.markdown; if ((location.hash.split("/")[1] || "") === "guide") NS.pages.guide(sec); });
    return;
  }
  const heads = [...GUIDE.matchAll(/^# (.+)$/gm)].map(m => m[1]);
  $("#app").innerHTML = `<div class="frame">
    <div class="col-left">
      <section class="hero"><p class="eyebrow">Guide</p><h1>How Night Scout<br><em>works.</em></h1>
        <p class="sub">Everything in one page, from the first night to the command line.</p></section>
      <button class="btn primary wide mb14" id="tourGo">▶ Take the 60-second tour</button>
      <div class="panel toc">${heads.map((h, i) => `<a href="#/guide/${slug(h)}" class="${(sec || slug(heads[0] || "")) === slug(h) ? "on" : ""}"><span>${String(i + 1).padStart(2, "0")}</span>${esc(h)}</a>`).join("")}</div>
    </div>
    <div class="stage"><div class="big enter"><div class="big-scroll"><a class="back" href="#/guide">‹ Guide</a><div class="brief guide" id="guideBody">${md(GUIDE)}</div></div></div></div>
  </div>`;
  wireTilt($(".stage"));
  $$("#guideBody h2").forEach(h => (h.id = "g-" + slug(h.textContent)));
  if (sec) { document.body.classList.add("card-open"); const t = document.getElementById("g-" + sec); if (t) setTimeout(() => t.scrollIntoView({ block: "start", behavior: "smooth" }), 350); }
  $("#tourGo").onclick = startTour;
};

/* Tour steps follow real data: the first pick, the first pick with a Try-it, the first idea. */
function steps() {
  const M = NS.M, cards = NS.visibleCards();
  const first = cards[0], withTry = cards.find(c => c.actions.some(a => a.kind === "try_it")) || first;
  const idea = M?.ideas?.[0];
  const st = M?.run?.stats || {};
  const proj = B()?.projects?.[0];
  const at = c => c ? `#/morning/${c.id}` : "#/morning";
  return [
    { at: at(first), sel: ".funnel", t: "What happened overnight", x: `The scout found ${st.new ?? 0} new items, had Gemma judge the ${st.scored ?? 0} most promising, and kept ${cards.length}. Everything else was dropped on purpose.` },
    { at: at(first), sel: "#list", t: "Your picks, ranked", x: "Each pick is tied to one of your projects and labelled Issue, Change, Improvement or Relevant." },
    { at: at(first), sel: ".derive", t: "Why it's here", x: "The project it matters to, what it matched in that project's profile, and a suggested next step." },
    { at: at(first), sel: ".why-box", t: "Scored against every project", x: "Gemma scores each item for every project and explains why. The best fit decides where it lands." },
    { at: at(withTry), sel: ".tabs", t: "Act on it", x: "Try it in a throwaway sandbox, have Gemma research it, or save a note into the project. You always confirm first." },
    { at: at(withTry), sel: ".votes", t: "Teach the scout", x: "👍 / 👎 nudge tonight's pre-filter toward or away from this source and its topics. ✕ hides it for good." },
    ...(idea ? [{ at: `#/morning/idea-${idea.id}`, sel: ".list-h", t: "New project ideas", x: "Side projects Gemma thinks you'd enjoy, inspired by what it read last night and what you already build." }] : []),
    { at: proj ? `#/projects/${encodeURIComponent(proj)}` : "#/projects", sel: ".how", t: "Projects are the yardstick", x: "Every item is judged against what's written here. Sharpen the interests and the scout gets sharper." },
    { at: "#/sources", sel: "#addSrc", t: "Choose what it reads", x: "Websites, YouTube channels, newsletters, Reddit, Hacker News, Bluesky, GitHub and more. Official feeds only." },
    { at: "#/settings/providers", sel: ".roles", t: "Your models, your machine", x: "Everything runs on Ollama locally by default. Cloud providers are optional, and their keys live in your system keychain." },
  ];
}
let STEPS = [];
let tourI = -1;
function startTour() { NS.S.howHidden = false; STEPS = steps(); tourI = 0; showStep(); }
function endTour() { tourI = -1; $("#tour")?.remove(); }
function showStep(tries = 0) {
  const st = STEPS[tourI]; if (!st) return endTour();
  let ov = $("#tour");
  if (!ov) { ov = document.createElement("div"); ov.id = "tour"; ov.innerHTML = `<div class="hole"></div><div class="tip"></div>`; document.body.appendChild(ov); }
  ov.classList.add("moving");
  const place = () => {
    const el = $(st.sel);
    if (!el) { if (tries < 6) return setTimeout(() => showStep(tries + 1), 300); tourI++; return showStep(); }
    el.scrollIntoView({ block: "center" });
    setTimeout(() => {
      const r = el.getBoundingClientRect(), pad = 8, hole = $(".hole", ov), tip = $(".tip", ov);
      Object.assign(hole.style, { left: r.left - pad + "px", top: r.top - pad + "px", width: r.width + pad * 2 + "px", height: Math.min(r.height, innerHeight - 140) + pad * 2 + "px" });
      tip.innerHTML = `<div class="tip-n">${tourI + 1} / ${STEPS.length}</div><h4>${esc(st.t)}</h4><p>${esc(st.x)}</p>
        <div class="row mt12"><button class="btn ghost" id="tSkip">Skip</button><span class="spacer grow"></span>
        ${tourI ? `<button class="btn ghost" id="tBack">Back</button>` : ""}<button class="btn primary" id="tNext">${tourI === STEPS.length - 1 ? "Done" : "Next"}</button></div>`;
      const tw = 340, below = r.bottom + 16 + 190 < innerHeight;
      let left = Math.min(Math.max(16, r.left), innerWidth - tw - 16), top = below ? r.bottom + 16 : Math.max(16, r.top - 16 - tip.offsetHeight);
      if (!below && r.top < 220) { left = Math.min(r.right + 18, innerWidth - tw - 16); top = Math.max(16, r.top); }
      Object.assign(tip.style, { left: left + "px", top: top + "px", width: tw + "px" });
      ov.classList.remove("moving");
      $("#tNext").onclick = () => { tourI++; tourI >= STEPS.length ? (endTour(), toast("<b>That's the tour.</b> The Guide has the details.")) : showStep(); };
      $("#tSkip").onclick = endTour;
      const bk = $("#tBack"); if (bk) bk.onclick = () => { tourI--; showStep(); };
    }, 120);
  };
  if (tries === 0 && location.hash !== st.at) { NS.reset(); location.hash = st.at; setTimeout(place, 950); } else place();
}
addEventListener("resize", () => tourI >= 0 && showStep());
addEventListener("keydown", e => { if (tourI >= 0 && e.key === "Escape") endTour(); });
NS.startTour = startTour;

/* ================= SETTINGS → PROVIDERS ================= */
/* Cloud presets: all speak the OpenAI-compatible API; keys go to the OS keychain. */
const CLOUD = [
  { id: "openai", name: "OpenAI", tile: "◎", env: "OPENAI_API_KEY", url: "https://platform.openai.com/api-keys", ph: "a model id, e.g. from platform.openai.com/docs/models" },
  { id: "openrouter", name: "OpenRouter", tile: "⇄", env: "OPENROUTER_API_KEY", url: "https://openrouter.ai/keys", ph: "e.g. google/gemma-3-27b-it" },
  { id: "anthropic", name: "Anthropic", tile: "A", soon: true },
  { id: "google", name: "Google Gemini", tile: "G", soon: true },
];
const ROLES = [
  ["judge", "Judge items & write briefs", "Scores every item for every project, proposes actions, writes research briefs."],
  ["fallback", "Fallback judge", "Used if the main model fails or times out."],
  ["embed", "Embeddings", "Finds near-duplicates and pre-filters items. Local only."],
];
const NAV = [["providers", "🔌", "AI Providers", "Models and API keys"], ["schedule", "🌙", "Schedule", "When the scout runs"], ["sandbox", "🧪", "Try-it sandbox", "Safety limits"], ["privacy", "🔒", "Data & privacy", "What's stored where"], ["about", "ⓘ", "About", "Version and licence"]];
const keychain = () => ({ darwin: "macOS Keychain", win32: "Windows Credential Manager" }[B().platform] || "system keychain");

/* What the page shows until GET /api/providers answers: Ollama with the models from config.yaml. */
function staticProviders() {
  const m = B().model;
  return {
    static: true,
    providers: [
      { id: "ollama", kind: "ollama", name: "Ollama", base_url: m.host, status: "unknown",
        models: [{ name: m.name, role: "Judge · Research" }, { name: m.fallback, role: "Fallback" }, { name: m.embed, role: "Embeddings" }] },
      { id: "lmstudio", kind: "openai_compatible", name: "LM Studio", base_url: "http://127.0.0.1:1234/v1", status: "unknown", models: [] },
    ],
    roles: { judge: `ollama/${m.name}`, fallback: `ollama/${m.fallback}`, embed: `ollama/${m.embed}` },
  };
}
let PV = null;
const ST = { olOpen: false, keyOpen: null };

NS.pages.settings = (sec = "providers") => {
  chrome("settings");
  if (!PV) {
    loading("Loading settings…");
    api("GET", "/api/providers").then(r => {
      PV = r.ok ? r.data : staticProviders();
      if ((location.hash.split("/")[1] || "") === "settings") NS.pages.settings(sec);
    });
    return;
  }
  $("#app").innerHTML = `<div class="frame">
    <div class="col-left">
      <section class="hero"><p class="eyebrow">Settings</p><h1>Your models,<br><em>your</em> machine.</h1>
        <p class="sub">Night Scout runs on local models by default. Nothing here is required for it to work.</p></section>
      <div class="snav">${NAV.map(([k, ic, l, d]) => `<a href="#/settings/${k}" class="${k === sec ? "on" : ""}"><span class="ic">${ic}</span><span><b>${l}</b><small>${d}</small></span></a>`).join("")}</div>
    </div>
    <div class="stage"><div class="big enter"><div class="big-scroll" id="setPane"></div></div></div>
  </div>`;
  wireTilt($(".stage"));
  if (location.hash.split("/").length > 2) document.body.classList.add("card-open");
  if (sec !== "providers") return renderOther(sec);
  renderProviders();
};

function renderOther(sec) {
  const b = B(), sb = b.sandbox, title = NAV.find(n => n[0] === sec)?.[2] || sec;
  const rows = {
    schedule: [["Runs every night at", b.schedule.time], ["Installed", b.schedule.installed === null ? "unknown on this platform" : b.schedule.installed ? `yes (${b.schedule.via})` : "no"],
      ["Install", "night-scout schedule --install"], ["Remove", "night-scout schedule --uninstall"]],
    sandbox: [["Sandbox here", sb.error ? `unavailable: ${sb.error}` : sb.mode === "container" ? `container (${sb.engine})` : sb.is_mac ? "venv + macOS sandbox" : "venv"],
      ["Prefer", sb.prefer], ["Time limit", `${sb.timeout_s} seconds`], ["Container image", sb.container_image]],
    privacy: [["Data folder", b.data_dir], ["Stored there", "scout.db (items, scores, votes), digests/, profile.yaml"], ["Notes", `written only under ${b.notes_subdir}/ inside a project, when you click Save`],
      ["Network", "official feeds and public APIs; judging runs on Ollama on this machine"]],
    about: [["Version", b.version], ["Licence", "MIT"], ["Model", b.model.name]],
  }[sec] || [];
  $("#setPane").innerHTML = `<a class="back" href="#/settings">‹ Settings</a><h2 class="serif mt0">${esc(title)}</h2>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <p class="muted small mt22">These live in <code>config.yaml</code> today${sec === "sandbox" ? " (sandbox: prefer, timeout_s, container_image)" : sec === "schedule" ? " (schedule: time)" : ""}.</p>`;
}

const modelsOf = p => (p.models || []).map(m => (typeof m === "string" ? { name: m } : m));
function roleOptions(role, val) {
  const local = PV.providers.filter(p => p.local !== false);
  const cloud = PV.providers.filter(p => !local.includes(p));
  const opt = (p, m) => { const v = `${p.id}/${m.name}`; return `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(p.id === "ollama" ? m.name : v)}</option>`; };
  const groups = local.filter(p => modelsOf(p).length).map(p => `<optgroup label="${esc(p.id === "ollama" ? `On ${B().platform === "darwin" ? "this Mac" : "this machine"} · Ollama` : p.name)}">${modelsOf(p).map(m => opt(p, m)).join("")}</optgroup>`).join("");
  const cl = role === "embed" ? "" : cloud.filter(p => modelsOf(p).length).map(p => `<optgroup label="Cloud · leaves this machine">${modelsOf(p).map(m => opt(p, m)).join("")}</optgroup>`).join("");
  const locked = role === "embed" ? [] : CLOUD.filter(c => !c.soon && !PV.providers.some(p => p.id === c.id && p.key?.configured)).map(c => c.name);
  const known = PV.providers.some(p => modelsOf(p).some(m => `${p.id}/${m.name}` === val));
  return (known ? "" : `<option value="${esc(val)}" selected>${esc(val)}</option>`) + groups + cl +
    (locked.length ? `<optgroup label="Add a key to use">${locked.map(n => `<option disabled>${esc(n)}</option>`).join("")}</optgroup>` : "");
}
const isCloud = v => { const id = String(v || "").split("/")[0]; const p = PV?.providers?.find(x => x.id === id); return p ? p.local === false : CLOUD.some(c => c.id === id); };
const statusBadge = p => p.status === "connected" ? `<span class="badge ok">Connected</span>` : p.status === "offline" ? `<span class="badge off">Not running</span>` : `<span class="badge off">Not checked</span>`;
const mask = k => k?.masked || "••••••••";

function renderProviders() {
  const pane = $("#setPane"); if (!pane) return;
  const prov = id => PV.providers.find(p => p.id === id);
  const ol = prov("ollama") || staticProviders().providers[0];
  const lm = prov("lmstudio");
  const custom = PV.providers.filter(p => !["ollama", "lmstudio"].includes(p.id) && !CLOUD.some(c => c.id === p.id));
  const olModels = modelsOf(ol);
  pane.innerHTML = `<a class="back" href="#/settings">‹ Settings</a>
    <div class="big-head"><div><h2 class="serif m0">AI Providers</h2>
      <p class="muted m-4">Local models by default. Cloud providers are optional; their keys go in your system keychain and are never shown again.</p></div>
      <span class="spacer"></span><button class="btn" id="addCustom">+ Add custom provider</button></div>

    <section class="pcard roles"><h4 class="sec-h">Model roles</h4>
      ${ROLES.map(([k, l, d]) => { const v = PV.roles?.[k] || ""; return `<div class="role"><div><b>${esc(l)}</b><small>${esc(d)}</small></div>
        <select data-role="${k}">${roleOptions(k, v)}</select>
        <span class="where ${isCloud(v) ? "cloud" : ""}">${isCloud(v) ? "↗ Leaves this machine" : `● On ${B().platform === "darwin" ? "this Mac" : "this machine"}`}</span></div>`; }).join("")}
    </section>

    <h4 class="sec-h mt22">On this machine</h4>
    <section class="pcard prov">
      <div class="prow"><span class="tile">🦙</span><div class="pmain"><div class="pname">Ollama <span class="badge local">Local</span>${statusBadge(ol)}</div>
        <div class="muted small mono">${esc(ol.base_url)}${ol.version ? ` · ollama ${esc(ol.version)}` : ""} · ${olModels.length} model${olModels.length === 1 ? "" : "s"}</div></div>
        <button class="btn ghost" data-test="ollama">Test</button><button class="btn" id="olModels">${ST.olOpen ? "Hide models" : "Models ▾"}</button></div>
      ${ST.olOpen ? `<div class="mlist">${olModels.map(m => `<div class="mrow2"><span class="mono">${esc(m.name)}</span>${m.params ? `<span class="pillx">${esc(m.params)}</span>` : ""}${m.quant ? `<span class="pillx">${esc(m.quant)}</span>` : ""}${m.size ? `<span class="muted small">${esc(m.size)}</span>` : ""}<span class="spacer grow"></span><span class="muted small">${esc(m.role || "")}</span></div>`).join("")}
        <div class="addform mt10"><input type="text" id="pullName" placeholder="Model from ollama.com/library"><button class="btn" id="pull">Pull</button></div>
        <div id="pullProg"></div></div>` : ""}
      <div class="testline" id="t-ollama"></div>
    </section>
    <section class="pcard prov">
      <div class="prow"><span class="tile">LM</span><div class="pmain"><div class="pname">LM Studio <span class="badge local">Local</span>${lm ? statusBadge(lm) : `<span class="badge off">Not added</span>`}</div>
        <div class="muted small mono">${esc(lm?.base_url || "http://127.0.0.1:1234/v1")} · OpenAI-compatible${modelsOf(lm || {}).length ? ` · ${modelsOf(lm).map(m => esc(m.name)).join(", ")}` : ""}</div></div>
        <button class="btn ghost" data-test="lmstudio">Test</button></div>
      <div class="addform mt10"><input type="text" id="lmModel" placeholder="Model loaded in LM Studio, e.g. google/gemma-3-12b"><button class="btn" id="lmAdd">Offer for roles</button></div>
      <div class="testline" id="t-lmstudio"></div>
    </section>
    ${custom.map(c => `<section class="pcard prov"><div class="prow"><span class="tile">⚙</span><div class="pmain"><div class="pname">${esc(c.name)} ${c.local === false ? `<span class="badge off">Leaves this machine</span>` : `<span class="badge local">Local</span>`}${statusBadge(c)}</div>
      <div class="muted small mono">${esc(c.base_url)}</div></div><button class="btn ghost" data-test="${esc(c.id)}">Test</button></div><div class="testline" id="t-${esc(c.id)}"></div></section>`).join("")}

    <h4 class="sec-h mt22">Cloud · optional</h4>
    ${CLOUD.map(p => { const sp = prov(p.id), k = sp?.key?.configured ? sp.key : null, open = ST.keyOpen === p.id;
      if (p.soon) return `<section class="pcard prov soon"><div class="prow"><span class="tile">${esc(p.tile)}</span><div class="pmain">
        <div class="pname">${esc(p.name)} <span class="badge off">After the contest</span></div>
        <div class="muted small">A native ${esc(p.name)} client is next on the list. Until then, OpenRouter reaches many of the same models.</div></div></div></section>`;
      return `<section class="pcard prov"><div class="prow"><span class="tile">${esc(p.tile)}</span><div class="pmain">
        <div class="pname">${esc(p.name)} ${k ? `<span class="badge ok">Configured</span>` : `<span class="badge off">Not configured</span>`}</div>
        ${k ? `<div class="small"><span class="keypill mono">${esc(mask(k))}</span> <span class="muted">${k.source === "env" ? `from <code>${esc(p.env)}</code>` : `🔒 in ${esc(keychain())}`}</span></div>` : `<div class="muted small">Sends item text and project profiles to ${esc(p.name)} when you pick it for a role</div>`}</div>
        ${k ? `<button class="btn ghost" data-test="${p.id}">Test</button><button class="btn ghost danger" data-rm="${p.id}">Remove</button><button class="btn" data-key="${p.id}">Update</button>` : `<button class="btn" data-key="${p.id}">Add key</button>`}</div>
        ${open ? `<div class="keyform"><div class="keyin"><input type="password" id="keyVal" placeholder="Enter ${esc(p.name)} API key…" autocomplete="off"><button class="eye" id="eye" title="Show">👁</button></div>
          <input type="text" id="keyModel" placeholder="Model to offer for roles: ${esc(p.ph || "")}" autocomplete="off">
          <div class="small muted"><a ${NS.ext(p.url)}>Get a key →</a> · Or set env var: <code>${esc(p.env)}</code></div>
          <div class="row"><button class="btn primary" id="keySave">Save to ${esc(keychain())}</button><button class="btn ghost" id="keyCancel">Cancel</button></div></div>` : ""}
        <div class="testline" id="t-${p.id}"></div></section>`; }).join("")}

    <section class="pcard lockbox"><b>🔒 How keys are stored</b>
      <ul><li>In your operating system's keychain: macOS Keychain, Windows Credential Manager or the Linux Secret Service.</li>
      <li>Never in <code>config.yaml</code>, the database, the logs, or the Try-it sandbox (its environment is wiped clean).</li>
      <li>Shown only as the first and last four characters. An environment variable overrides a stored key.</li>
      <li>Cloud models are never used unless you pick one for a role above.</li></ul></section>`;
  $$("select[data-role]", pane).forEach(sel => sel.onchange = async () => {
    const role = sel.dataset.role, value = sel.value, prev = PV.roles?.[role];
    const r = await api("PUT", "/api/roles", { [role]: value });
    if (!r.ok) { sel.value = prev; return failToast(r, "Choosing models from here"); }
    PV.roles = r.data?.roles || { ...PV.roles, [role]: value }; renderProviders();
    if (isCloud(value)) toast(`<b>Heads up:</b> ${esc(sel.closest(".role").querySelector("b").textContent)} now sends item text and project profiles to ${esc(value.split("/")[0])}.`);
  });
  $("#olModels").onclick = async () => {
    ST.olOpen = !ST.olOpen;
    if (ST.olOpen) { const r = await api("GET", "/api/ollama/models"); if (r.ok && Array.isArray(r.data?.models)) (prov("ollama") || {}).models = r.data.models; }
    renderProviders();
  };
  const pull = $("#pull");
  if (pull) pull.onclick = async () => {
    const n = $("#pullName").value.trim(); if (!n) return $("#pullName").focus();
    pull.disabled = true;
    const r = await api("POST", "/api/ollama/pull", { name: n });
    if (!r.ok) { pull.disabled = false; return failToast(r, "Pulling models from here"); }
    const draw = (pctv, label) => { const pp = $("#pullProg"); if (!pp) return;
      pp.innerHTML = `<div class="pullbar"><span class="mono small">${esc(n)}</span><span class="meter"><i data-w="${Math.round(pctv || 0)}"></i></span><span class="mono small">${esc(label)}</span></div>`; fx(pp); };
    draw(0, "starting");
    const poll = async () => {
      const s = await api("GET", `/api/ollama/pull?name=${encodeURIComponent(n)}`);
      const d = s.data || {};
      if (d.state === "error") { pull.disabled = false; draw(0, "failed"); return toast(`<b>Pull failed.</b> ${esc(d.error || "")}`); }
      if (d.state === "done") { draw(100, "done"); pull.disabled = false; toast(`<b>Pulled ${esc(n)}.</b> It now shows up in the model pickers.`); PV = null; return setTimeout(() => NS.pages.settings("providers"), 800); }
      draw(d.percent, d.percent != null ? `${Math.round(d.percent)}%` : (d.status || "…"));
      setTimeout(poll, 1000);
    };
    setTimeout(poll, 800);
  };
  $$("[data-test]", pane).forEach(b => b.onclick = () => runTest(b.dataset.test));
  $("#lmAdd").onclick = async () => {
    const m = $("#lmModel").value.trim(); if (!m) return $("#lmModel").focus();
    const r = await api("PUT", "/api/providers/lmstudio/models", { model: m });
    if (!r.ok) return failToast(r, "Adding models from here");
    toast(`<b>${esc(m)}</b> is now in the model pickers.`); PV = null; NS.pages.settings("providers");
  };
  $$("[data-key]", pane).forEach(b => b.onclick = () => { ST.keyOpen = b.dataset.key; renderProviders(); $("#keyVal")?.focus(); });
  $$("[data-rm]", pane).forEach(b => b.onclick = async () => {
    const r = await api("DELETE", `/api/keys/${encodeURIComponent(b.dataset.rm)}`);
    if (!r.ok) return failToast(r, "Removing keys from here");
    PV = null; toast(`Key removed from the ${esc(keychain())}. Roles that used it went back to Gemma.`); NS.pages.settings("providers");
  });
  const ks = $("#keySave");
  if (ks) {
    const input = $("#keyVal");
    $("#eye").onclick = () => { input.type = input.type === "password" ? "text" : "password"; };
    $("#keyCancel").onclick = () => { ST.keyOpen = null; renderProviders(); };
    ks.onclick = async () => {
      const v = input.value.trim(); if (v.length < 12) return toast("That doesn't look like an API key.");
      const id = ST.keyOpen;
      const model = ($("#keyModel")?.value || "").trim();
      const r = await api("PUT", `/api/keys/${encodeURIComponent(id)}`, model ? { key: v, model } : { key: v });
      input.value = "";
      if (!r.ok) return failToast(r, "Saving keys from here");
      ST.keyOpen = null; PV = null; NS.pages.settings("providers"); setTimeout(() => runTest(id), 600);
    };
    input.onkeydown = e => { if (e.key === "Enter") ks.click(); };
  }
  $("#addCustom").onclick = customSheet;
}
async function runTest(id) {
  const box = document.getElementById("t-" + id); if (!box) return;
  box.innerHTML = `<div class="checks"><span class="now">◌ Health</span><span>○ Auth</span><span>○ Chat</span></div>`;
  const r = await api("POST", `/api/providers/${encodeURIComponent(id)}/test`, {});
  if (!r.ok) { box.innerHTML = ""; return failToast(r, "Testing providers from here"); }
  const steps = r.data?.steps || [];
  const failed = steps.find(s => !s.ok);
  box.innerHTML = `<div class="checks">${steps.map(s => `<span class="${s.ok ? "good" : "bad"}">${s.ok ? "✓" : "✕"} ${esc(s.name)}</span>`).join("")}</div>` +
    (failed ? `<p class="small neg testmsg">Test failed at ${esc(failed.name)}: ${esc(failed.detail || "")}</p>`
      : `<p class="small testmsg good">Connection test passed.${steps.at(-1)?.detail ? " " + esc(steps.at(-1).detail) : ""}</p>`);
}
function customSheet() {
  const big = $(".big"); const w = document.createElement("div");
  w.className = "sheet-wrap";
  w.innerHTML = `<div class="sheet"><p class="eyebrow">Add custom provider</p><h3>Any OpenAI-compatible server</h3>
    <p class="muted small">llama.cpp server, vLLM, Jan, LocalAI, or a hosted endpoint.</p>
    <div class="fgrid"><label>Name<input type="text" id="cName" placeholder="llama.cpp on the Windows box"></label>
      <label>Base URL<input type="text" id="cUrl" placeholder="http://127.0.0.1:8080/v1"></label>
      <label>API key <span class="muted">(optional, stored in the ${esc(keychain())})</span><input type="password" id="cKey" placeholder="sk-…" autocomplete="off"></label>
      <label>Model<input type="text" id="cModel" placeholder="gemma-4-12b-it"></label></div>
    <div class="row"><button class="btn primary" id="cSave">Test and save</button><button class="btn ghost" id="cCancel">Cancel</button></div></div>`;
  big.appendChild(w); big.classList.add("locked");
  const close = () => { w.remove(); big.classList.remove("locked"); };
  $("#cCancel", w).onclick = close;
  w.onclick = e => { if (e.target === w) close(); };
  $("#cSave", w).onclick = async () => {
    const body = { name: $("#cName", w).value.trim(), base_url: $("#cUrl", w).value.trim(), api_key: $("#cKey", w).value, model: $("#cModel", w).value.trim() };
    if (!body.name || !/^https?:\/\//i.test(body.base_url)) return toast("Give it a name and an http(s) base URL.");
    const r = await api("POST", "/api/providers", body);
    $("#cKey", w).value = "";
    if (!r.ok) return failToast(r, "Adding providers from here");
    close(); PV = null; NS.pages.settings("providers");
    if (r.data?.id) setTimeout(() => runTest(r.data.id), 600);
  };
}
})();
