/* Night Scout web app: the approved mockup, ported onto the local JSON API.
   No inline styles or handlers anywhere (strict CSP): one-off styles are classes in app.css,
   widths and delays are set from data-* attributes after the HTML is inserted. */
window.NS = { pages: {} };
(() => {
"use strict";
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = x => Math.round((x || 0) * 100);
const ICON = { youtube: "▶", rss: "◉", hackernews: "Y", reddit: "r/", bluesky: "bs", mastodon: "M", github: "GH", x: "X" };
const badge = (src, lg) => `<span class="src src-${esc(src)}${lg ? " lg" : ""}" title="${esc(src)}">${ICON[src] || "•"}</span>`;
/* an href only for http(s) URLs (the server already sends null for anything else) */
const isWeb = u => typeof u === "string" && /^https?:\/\//i.test(u);
const ext = u => isWeb(u) ? `href="${esc(u)}" target="_blank" rel="noopener noreferrer"` : "";
/* the scout: the Ask icon (top bar, chat drawer, Ask buttons) */
const scoutImg = (size = 18, cls = "scout-ic") => `<img class="${cls}" src="/static/scout.svg" width="${size}" height="${size}" alt="">`;

/* widths and animation delays from data attributes (CSP: no style="" in HTML strings) */
function fx(root) {
  $$("[data-w]", root).forEach(el => { el.style.width = Math.max(0, Math.min(100, +el.dataset.w || 0)) + "%"; });
  $$("[data-delay]", root).forEach(el => { el.style.animationDelay = (+el.dataset.delay || 0) + "ms"; });
}

/* ---- API ---- */
async function api(method, url, body) {
  const opt = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (method !== "GET") opt.headers["X-CSRF-Token"] = CSRF;
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opt); } catch (e) {
    return { ok: false, status: 0, error: "Night Scout isn't answering. Is night-scout serve still running?" };
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.ok) return { ok: true, status: res.status, data };
  const err = data && (data.error || data.detail);
  return { ok: false, status: res.status, notYet: res.status === 501, data,
    error: res.status === 501 ? "Not wired yet" : typeof err === "string" ? err : `HTTP ${res.status}` };
}
function failToast(r, what) {
  if (r.notYet) return toast(`<b>Not wired yet.</b> <span class="muted">${esc(what || "This")} arrives with the next update.</span>`);
  if (r.status === 401) return toast(`<b>Signed out.</b> Open the link printed by <code>night-scout serve</code>.`);
  toast(`<b>Couldn't do that.</b> ${esc(r.error)}`);
}

/* ---- state ---- */
let B = null;   // /api/bootstrap
let M = null;   // /api/morning
let RUN = null; // /api/run
const S = { tab: {}, srcTab: "feeds", addType: "website", profView: "list", howHidden: false,
  edited: {}, prof: null, rebuilding: false, ideaGone: {}, ideaStarted: {}, research: {}, tryStart: {} };
let cur = null; // selected card id on Morning

async function loadBoot() { const r = await api("GET", "/api/bootstrap"); if (r.ok) B = r.data; return r; }
async function loadMorning(day) {
  const r = await api("GET", "/api/morning" + (day ? `?day=${encodeURIComponent(day)}` : ""));
  if (r.ok) M = r.data; return r;
}
async function loadRun() { const r = await api("GET", "/api/run"); if (r.ok) RUN = r.data; return r; }

const visibleCards = () => (M?.cards || []).filter(c => c.vote !== 0);
const card = id => (M?.cards || []).find(c => c.id === +id);
const ideaOf = id => (M?.ideas || []).find(x => String(x.id) === String(id));
const fmtDay = d => new Date(d + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" });
const fmtShort = iso => iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
const fmtAt = iso => iso ? new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
const hhmm = iso => iso ? new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
const runMinutes = run => run && run.finished_at ? ((new Date(run.finished_at) - new Date(run.started_at)) / 60000).toFixed(1) : null;
const judge = () => B?.judge || { model: B?.model?.name || "Gemma", local: true, provider: "Ollama" };
const judgedLine = () => judge().local
  ? `Judged by ${esc(judge().model)} on ${here()}; nothing left the machine.`
  : `Judged by ${esc(judge().model)} via ${esc(judge().provider)}; item text and your project profiles were sent there.`;
const here = () => (B && B.platform === "darwin" ? "this Mac" : "this machine");
const today = () => { const d = new Date(), z = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`; };
const dayLabel = () => fmtDay(M?.day || today());

/* tiny markdown: escape first, then headings, lists, fenced code, paragraphs, inline code/bold/em, http(s) links */
function md(src) {
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = []; let list = false, code = null, para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } if (list) { out.push("</ul>"); list = false; } };
  for (const line of String(src || "").split("\n")) {
    if (code !== null) { if (/^```/.test(line)) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = null; } else code.push(line); continue; }
    if (/^```/.test(line)) { flush(); code = []; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { flush(); const n = Math.min(3, Math.max(2, h[1].length)); out.push(`<h${n}>${inline(h[2])}</h${n}>`); continue; }
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (li) { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
    if (!line.trim()) { flush(); continue; }
    if (list) { out.push("</ul>"); list = false; }
    para.push(line.trim());
  }
  flush(); if (code) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
  return out.join("");
}

/* ---- toast ---- */
let toastT;
function toast(html, undo) {
  const t = $("#toast");
  t.innerHTML = `<span>${html}</span>` + (undo ? `<button id="undo">Undo</button>` : "");
  t.classList.add("show");
  if (undo) $("#undo").onclick = () => { t.classList.remove("show"); undo(); };
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 5200);
}

/* ---- chrome ---- */
const STAGES = [
  ["fetch", "Fetch", () => `${RUN?.sources_total ?? B?.sources_count ?? 0} sources`, "new", "new"],
  ["dedupe", "Dedupe", () => "URL + embeddings", "near_duplicates", "merged"],
  ["prefilter", "Pre-filter", () => B?.model.embed || "embeddings", "candidates", "kept"],
  ["judge", "Judge", () => judge().model, "scored", "scored"],
  ["actions", "Actions", () => "Try-it · notes · research", "with_actions", "proposed"],
  ["ideas", "Ideas", () => "new project ideas", "ideas", "ideas"],
  ["digest", "Digest", () => `top ${B?.thresholds?.digest_size ?? 7}`, "digest_items", "picked"],
];
const stageName = p => (STAGES.find(s => s[0] === p?.stage)?.[1] || "working").toLowerCase();
function chrome(page) {
  $$("[data-page]").forEach(a => a.classList.toggle("on", a.dataset.page === page));
  if (!B) return;
  const j = B.judge || { model: B.model.name, local: true, provider: "Ollama" };
  $("#modelName").textContent = j.model;
  $("#modelWhere").textContent = j.local ? "local" : j.provider;
  $("#modelChip").classList.toggle("cloud", !j.local);
  $("#modelChip").title = j.local ? "All judging happens on this machine" : `Judging runs on ${j.provider}: item text and project profiles are sent there`;
  const rows = B.statuses, bad = rows.filter(r => !r.ok), run = B.run;
  const model = B.pipeline.find(p => p.key === "pipeline:model");
  const sched = B.schedule.installed === false
    ? `<span>Not scheduled · night-scout schedule --install</span>`
    : `<span>Next run ${esc(B.schedule.time)}${B.schedule.via ? " · " + esc(B.schedule.via) : ""}</span>`;
  if (RUN?.running) {
    $("#runChip").innerHTML = `<span class="warn">● scout is out · ${esc(stageName(RUN.progress))}</span>`;
    $("#status").innerHTML = `<span class="warn">Run in progress · started ${esc(hhmm(RUN.run.started_at))} · ${esc(stageName(RUN.progress))}</span>` + sched;
  } else if (run) {
    const min = runMinutes(run);
    $("#runChip").innerHTML = `Last run ${esc(fmtAt(run.started_at))}${min ? ` · ${min} min` : ""} · ` +
      (bad.length ? `<span class="warn">${rows.length - bad.length}/${rows.length} sources</span>` : `<span class="ok">✓ ${rows.length} sources</span>`);
    $("#status").innerHTML = `<span>Last run ${esc(fmtAt(run.started_at))} · ${esc(run.status)}</span>` +
      (bad.length ? bad.map(b => `<span class="warn">${esc(b.label)}: ${esc(b.message)}</span>`).join("") : rows.length ? `<span class="ok">All ${rows.length} sources OK</span>` : "") +
      (model ? `<span class="${model.ok ? "" : "warn"}">Gemma ${esc(model.message)}</span>` : "") + sched;
  } else {
    $("#runChip").textContent = "No runs yet";
    $("#status").innerHTML = `<span>No runs yet. Try <code>night-scout run</code>.</span>` + sched;
  }
  document.body.classList.toggle("night", page === "morning" && !!RUN?.running);
}

/* ---- router ---- */
function route() {
  let [, page = "digest", a, b] = (location.hash || "#/digest").split("/");
  if (page === "actions") page = "morning"; // Actions is the old Morning view, renamed
  document.body.dataset.page = page;
  document.body.classList.remove("card-open");
  if (!B) return;
  const arg = a ? decodeURIComponent(a) : a;
  if (NS.pages[page]) return NS.pages[page](arg, b);
  if (page === "sources") return renderSources();
  if (page === "projects" || page === "profile") return renderProfile(arg);
  if (page === "morning" || !NS.pages.digest) return renderMorning(arg, b);
  NS.pages.digest();
}
window.addEventListener("hashchange", route);
const go = h => { if (location.hash === h) route(); else location.hash = h; };
const reset = () => { cur = null; $("#app").innerHTML = ""; };
function loading(text) { $("#app").innerHTML = `<div class="frame"><div class="col-left"><p class="loading">${esc(text)}</p></div></div>`; }
function errorView(r) {
  $("#app").innerHTML = `<div class="frame"><div class="col-left"><section class="hero"><p class="eyebrow">Night Scout</p>
    <h1>Something's <em>off.</em></h1><p class="sub">${esc(r.error || "The local server didn't answer.")}</p></section></div></div>`;
}

/* ================= MORNING ================= */
function morningState() {
  if (RUN?.running) return "running";
  if (!B.projects.length && (B.topics || []).length) return "noprojects";
  if (!M.day) return "empty";
  if (!visibleCards().length && !(M.ideas || []).length) return "quiet";
  return "normal";
}
function renderMorning(id, tab) {
  chrome("morning");
  const app = $("#app");
  if (!M) {
    loading("Loading your morning…");
    loadMorning().then(r => { if (!r.ok) return errorView(r); if (["morning", "actions"].includes(location.hash.split("/")[1])) { reset(); route(); } });
    return;
  }
  const st8 = morningState();
  if (st8 !== "normal") return renderMorningState(app, st8);
  const explicitId = !!id;
  const cards = visibleCards();
  const isIdea = /^idea-\d+$/.test(id || "") && ideaOf(id.slice(5));
  if (!isIdea && (!id || !card(id) || card(id).vote === 0)) id = cards[0]?.id ?? (M.ideas?.[0] ? `idea-${M.ideas[0].id}` : undefined);
  const nowIdea = /^idea-\d+$/.test(String(id || "")) && ideaOf(String(id).slice(5));
  if (tab && !nowIdea) S.tab[id] = tab;
  const fresh = !$(".frame.morning", app);
  if (fresh) {
    const st = M.run?.stats || {};
    const days = [...(M.days || [])].slice(0, 7).reverse();
    const min = runMinutes(M.run);
    app.innerHTML = `<div class="frame morning">
      <div class="col-left">
        <section class="hero">
          <p class="eyebrow">Good morning · ${esc(dayLabel())}</p>
          <h1><em>${words(cards.length)}</em> thing${cards.length === 1 ? "" : "s"}<br>worth doing.</h1>
          <p class="sub">Every item was checked against your ${M.projects.length} project${M.projects.length === 1 ? "" : "s"} for issues, changes and improvements. ${judgedLine()}</p>
          <div class="funnel">
            <div class="st"><b>${st.new ?? 0}</b><small>new</small></div><span class="arrow">→</span>
            <div class="st"><b>${st.scored ?? 0}</b><small>judged</small></div><span class="arrow">→</span>
            <div class="st last"><b>${cards.length}</b><small>picked</small></div>
          </div>
          <p class="funnel-cap">${M.statuses.length} sources · ${st.near_duplicates ?? 0} near-dupes merged${min ? ` · ${min} min` : ""}</p>
          ${days.length ? `<div class="days">${days.map(d => `<button data-day="${esc(d)}" class="${d === M.day ? "on" : ""}">${esc(fmtShort(d + "T12:00:00Z"))}</button>`).join("")}</div>` : ""}
        </section>
        <div class="list" id="list"></div>
      </div>
      <div class="stage" id="stage"></div>
    </div>`;
    wireTilt($("#stage"));
    $$(".days button", app).forEach(b => b.onclick = async () => {
      if (b.dataset.day === M.day) return;
      const r = await loadMorning(b.dataset.day); if (!r.ok) return failToast(r);
      S.tab = {}; reset(); go("#/morning");
    });
    $("#list").classList.add("intro"); setTimeout(() => $("#list")?.classList.remove("intro"), 1200);
  }
  renderList(id);
  if (nowIdea) { if (fresh || String(cur) !== String(id)) showIdea(id); if (explicitId) document.body.classList.add("card-open"); return; }
  if (id === undefined) return;
  if (fresh || String(cur) !== String(id)) showBig(id, fresh ? 0 : dir(cur, id));
  else if (tab) showBig(id, 0, true);
  if (explicitId) document.body.classList.add("card-open");
}
const words = n => ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"][n] || String(n);
const dir = (a, b) => { const ids = (M.cards || []).map(c => c.id); return ids.indexOf(+b) >= ids.indexOf(+a) ? 1 : -1; };

const tryOf = c => c.actions.find(a => a.kind === "try_it" && a.status !== "refused") || c.actions.find(a => a.kind === "try_it");
function glyphs(c) {
  const t = tryOf(c);
  const out = [];
  if (t) out.push(`<span class="glyph ${t.status === "passed" ? "passed" : ""}">▶ Try${t.status === "passed" ? " ✓" : t.status === "running" ? " …" : ""}</span>`);
  c.actions.filter(a => a.kind === "note_for_project").slice(0, 1).forEach(a => out.push(`<span class="glyph ${a.status === "done" ? "done" : ""}">📝${a.status === "done" ? " ✓" : ""}</span>`));
  if (c.actions.some(a => a.kind === "research_more") || c.research) out.push(`<span class="glyph">🔎</span>`);
  if (c.topics.some(t2 => String(t2).toLowerCase() === "security")) out.push(`<span class="glyph alert">security</span>`);
  return out.join("");
}
function renderList(sel) {
  const list = $("#list"); if (!list) return;
  const shown = (M.cards || []).filter(c => c.vote !== 0);
  const ideas = M.ideas || [];
  list.innerHTML = shown.map((c, k) => `
    <article class="mini ${String(c.id) === String(sel) ? "sel" : ""} ${c.vote === -1 ? "down" : ""}" data-id="${c.id}" data-delay="${k * 45}">
      <div class="rank">${k + 1}</div>
      <div>
        <div class="meta">${badge(c.source)}<span>${esc(c.source_name)}</span>${c.also_seen.length ? `<span class="buzz">+${c.also_seen.length}</span>` : ""}</div>
        <h3>${esc(c.title)}</h3>
        <div class="fitline">for <strong>${esc(c.best || "your projects")}</strong><span class="meter"><i data-w="${pct(c.best_score)}"></i></span><span class="pct">${pct(c.best_score)}%</span>
          <span class="glyphs">${glyphs(c)}</span></div>
      </div>
      ${kindPill(c)}
    </article>`).join("") + (ideas.length ? `
    <p class="list-h">💡 New project ideas <span>side projects inspired by last night's items</span></p>` +
    ideas.map(x => `<article class="mini idea ${String(sel) === "idea-" + x.id ? "sel" : ""} ${S.ideaGone[x.id] ? "down" : ""}" data-id="idea-${x.id}">
      <div class="rank">💡</div><div><div class="meta"><span>Idea · inspired by ${x.inspired_by.length} item${x.inspired_by.length === 1 ? "" : "s"}</span></div>
      <h3>${esc(x.title)}</h3><div class="fitline"><span class="idea-pitch">${esc(x.pitch)}</span></div></div></article>`).join("") : "");
  fx(list);
  list.querySelectorAll(".mini").forEach(m => m.onclick = () => go(`#/morning/${m.dataset.id}`));
}

function tabsFor(c) {
  const t = [];
  const tr = tryOf(c); if (tr) t.push(["try", "▶ Try it", tr]);
  t.push(["research", "🔎 Research", null]);
  c.actions.filter(a => a.kind === "note_for_project").forEach(a => t.push([`note-${a.id}`, `📝 Note for ${a.project}`, a]));
  t.push(["source", "From the source", null]);
  return t;
}
const KIND = { issue: "Issue", change: "Change", improvement: "Improvement", relevant: "Relevant" };
const kindPill = c => c.impact && KIND[c.impact] ? `<span class="kind k-${c.impact}">${KIND[c.impact]}</span>` : "";

function showIdea(id) {
  const x = ideaOf(String(id).slice(5)), stage = $("#stage"); if (!x || !stage) return;
  const old = $(".big:not(.leave)", stage);
  cur = id;
  const el = document.createElement("div");
  el.className = "big enter";
  const started = S.ideaStarted[x.id] || (x.status === "started" ? { path: null } : null);
  const items = M.idea_items || {};
  el.innerHTML = `<div class="big-scroll">
    <div class="big-head"><a class="back" href="#/actions">‹ Actions</a><span class="kind k-idea">New project idea</span><span class="muted">Gemma · from last night's ${M.run?.stats?.scored ?? 0} judged items</span><span class="spacer"></span>
      ${started ? "" : `<button class="btn ghost" id="iNo">Not interested</button>`}</div>
    <h2 class="serif idea-title">${esc(x.title)}</h2>
    <p class="summary idea-pitch-lg">${esc(x.pitch)}</p>
    <div class="why-grid mt22">
      <div class="why-box"><h4>Why you</h4><p class="projwhy fs14">${esc(x.why_you)}</p></div>
      <div><h4 class="sec-h">First step</h4><pre class="cmds">${esc(x.first_step)}</pre></div>
    </div>
    <h4 class="sec-h">Inspired by</h4>
    <div class="insp">${x.inspired_by.map(iid => { const it = items[iid]; if (!it) return "";
      const inner = `${badge(it.source)}<span>${esc(it.title)}</span><span class="muted small">${esc(it.source_name)}</span>`;
      return card(iid) ? `<a class="insp-row" href="#/morning/${iid}">${inner}</a>` : isWeb(it.url) ? `<a class="insp-row" ${ext(it.url)}>${inner}</a>` : `<div class="insp-row">${inner}</div>`; }).join("")}</div>
    <div class="row mt22">
      ${started ? `<div class="saved">✓ ${started.path ? `Created <code>${esc(started.path)}</code> with a README of this idea and added it to Projects.` : "Started as a project and added to Projects."} Tonight's run judges against it too.</div>`
        : `<button class="btn primary" id="iGo">Start as a project…</button>
           <span class="muted small">Creates a folder with a README of this idea and adds it to your Projects.</span>`}
    </div></div>`;
  if (old) { old.classList.add("leave"); setTimeout(() => old.remove(), 460); }
  stage.appendChild(el);
  document.body.classList.add("card-open");
  const b = $("#iGo", el);
  if (b) b.onclick = async () => {
    b.disabled = true;
    const r = await api("POST", `/api/ideas/${x.id}/start`, {});
    b.disabled = false;
    if (!r.ok) return failToast(r, "Starting a project from an idea");
    S.ideaStarted[x.id] = { path: r.data?.path || null }; x.status = "started";
    showIdea(id);
    toast(`<b>Project created.</b> ${r.data?.path ? `<code>${esc(r.data.path)}</code> is` : "It's"} on your Projects page.`);
  };
  const no = $("#iNo", el);
  if (no) no.onclick = async () => {
    const r = await api("POST", `/api/ideas/${x.id}/dismiss`, {});
    if (!r.ok) return failToast(r, "Dismissing ideas");
    S.ideaGone[x.id] = true; renderList(id); toast("Got it. Fewer ideas like this.");
  };
}

function showBig(id, direction, keepPos) {
  const c = card(id), stage = $("#stage"); if (!c || !stage) return;
  const old = $(".big:not(.leave)", stage);
  const scrollTop = keepPos && old ? $(".big-scroll", old).scrollTop : 0;
  cur = id;
  const tabs = tabsFor(c);
  let tab = S.tab[id] || (c.research ? "research" : tabs[0][0]);
  if (!tabs.some(t => t[0] === tab)) tab = tabs[0][0];
  S.tab[id] = tab;
  const others = c.per_project.filter(p => p.project !== c.best).sort((a, b) => b.relevance - a.relevance);
  const best = c.per_project.find(p => p.project === c.best);
  const v = c.vote;
  const el = document.createElement("div");
  el.className = "big" + (keepPos ? "" : " enter");
  el.innerHTML = `<div class="big-scroll">
    <div class="big-head"><a class="back" href="#/actions">‹ Actions</a>${badge(c.source, 1)}<span><strong class="hi">${esc(c.source_name)}</strong>${c.author && c.author !== c.source_name ? " · " + esc(c.author) : ""}${c.published ? " · " + esc(fmtShort(c.published)) : ""}</span>
      ${c.also_seen.length ? `<span class="buzz" title="Also seen on ${esc(c.also_seen.join(", "))}">also on ${c.also_seen.length} more</span>` : ""}
      <span class="spacer"></span>
      <div class="votes">
        <button class="vote ${v === 1 ? "on" : ""}" data-v="1" title="More like this">👍</button>
        <button class="vote ${v === -1 ? "on" : ""}" data-v="-1" title="Less like this">👎</button>
        <button class="vote" data-v="0" title="Hide this item">✕</button>
      </div>
      ${isWeb(c.url) ? `<a class="open" ${ext(c.url)}>Open ↗</a>` : ""}
    </div>
    <h2>${esc(c.title)}</h2>
    ${c.impact ? `<div class="derive">${kindPill(c)}<span>for <strong>${esc(c.best)}</strong></span>
      ${c.matched?.length ? `<span class="muted">matched in its project profile:</span>${c.matched.map(m => `<a class="match" href="#/projects/${encodeURIComponent(c.best || "")}">${esc(m)}</a>`).join("")}` : ""}
      ${c.next_step ? `<span class="next">→ ${esc(c.next_step)}</span>` : ""}</div>` : ""}
    <div class="why-grid">
      <div class="why-box">
        <h4>Why it matters</h4>
        <div class="projrow best"><strong>${esc(c.best || "—")}</strong><span class="meter"><i data-w="${pct(best?.relevance ?? c.best_score)}"></i></span><span class="pct">${pct(best?.relevance ?? c.best_score)}%</span></div>
        <p class="projwhy">${esc(best?.reason)}</p>
        ${others.map(p => `<div class="projrow"><span class="muted">${esc(p.project)}</span><span class="meter dim"><i data-w="${pct(p.relevance)}"></i></span><span class="pct dim">${pct(p.relevance)}%</span></div>
          ${p.reason ? `<p class="projwhy dim">${esc(p.reason)}</p>` : ""}`).join("")}
      </div>
      <div>
        <h4 class="sec-h">Summary · Gemma</h4>
        <p class="summary">${esc(c.summary)}</p>
        <div class="topics">${c.topics.map(t => `<span class="tag">#${esc(t)}</span>`).join("")}</div>
      </div>
    </div>
    <div class="tabs">${tabs.map(([k, label, a], i) => {
      let st = "";
      if (k === "try" && a.status !== "proposed") st = `<span class="st st-${esc(a.status)}">${esc(a.status)}</span>`;
      if (k.startsWith("note-") && a.status === "done") st = `<span class="st st-done">saved</span>`;
      if (k === "research") st = researchRunning(c) ? `<span class="st st-running">writing</span>` : c.research ? `<span class="st st-done">brief</span>` : "";
      return `<button class="tab ${k === tab ? "on" : ""}" data-tab="${k}" title="Key ${i + 1}">${esc(label)}${st}</button>`;
    }).join("")}</div>
    <div class="pane" id="pane"></div>
  </div>`;
  fx(el);
  if (old && !keepPos) {
    old.classList.add("leave");
    old.style.setProperty("--d", direction);
    setTimeout(() => old.remove(), 460);
  } else if (old) old.remove();
  stage.appendChild(el);
  if (keepPos) $(".big-scroll", el).scrollTop = scrollTop;
  $$(".vote", el).forEach(b => b.onclick = () => vote(c, +b.dataset.v));
  $$(".tab", el).forEach(b => b.onclick = () => { S.tab[id] = b.dataset.tab; showBig(id, 0, true); });
  renderPane(c, tab, el);
}

const VOTE_NAME = { 1: "up", "-1": "down", 0: "ignore" };
async function vote(c, v) {
  const prev = c.vote;
  const value = v !== 0 && prev === v ? "clear" : VOTE_NAME[v];
  const r = await api("POST", `/api/item/${c.id}/vote`, { value });
  if (!r.ok) return failToast(r);
  c.vote = r.data.vote;
  if (value === "clear") { renderList(c.id); showBig(c.id, 0, true); return; }
  const undo = async () => {
    const back = await api("POST", `/api/item/${c.id}/vote`, { value: prev == null ? "clear" : VOTE_NAME[prev] });
    if (!back.ok) return failToast(back);
    c.vote = back.data.vote; reset(); go(`#/morning/${c.id}`);
  };
  if (v === 0) {
    const m = $(`.mini[data-id="${c.id}"]`); if (m) m.classList.add("gone");
    const next = visibleCards()[0];
    toast(`Hidden. It won't come back.`, undo);
    setTimeout(() => { reset(); go(next ? `#/morning/${next.id}` : "#/morning"); }, 380);
    return;
  }
  renderList(c.id); showBig(c.id, 0, true);
  const tops = r.data.topics.slice(0, 2).map(([t, w]) => `#${esc(t)} ${fmt(w)}`).join(" · ");
  toast(`${v > 0 ? "<b>More like this.</b>" : "<b>Less like this.</b>"} ${esc(r.data.source.name)} ${fmt(r.data.source.weight)}${tops ? " · " + tops : ""}. Tonight's pre-filter uses it.`, undo);
}
const fmt = x => (x >= 0 ? "+" : "") + (x || 0).toFixed(2);

function renderPane(c, tab, root) {
  const pane = $("#pane", root);
  if (tab === "try") return paneTry(c, pane, root);
  if (tab === "research") return paneResearch(c, pane, root);
  if (tab.startsWith("note-")) return paneNote(c, pane, +tab.slice(5));
  const txt = c.transcript || c.text || "";
  pane.innerHTML = `<h4 class="sec-h">${c.transcript ? "Transcript excerpt" : "From the source"}</h4>
    ${txt ? `<div class="source-text">${esc(txt.slice(0, 1400))}${txt.length > 1400 ? "…" : ""}</div>`
      : `<p class="muted">This ${c.source === "hackernews" ? "Hacker News post is a link" : "item came"} without text of its own. Open the original, or use the Research tab to have Gemma read the full page and brief you.</p>`}
    ${isWeb(c.url) ? `<div class="row"><a class="btn ghost" ${ext(c.url)}>Open the original ↗</a></div>` : ""}`;
}

/* ---- Try it ---- */
const sandboxLabel = () => B.sandbox.error ? `unavailable (${B.sandbox.error})` : B.sandbox.mode === "container" ? `container (${B.sandbox.engine || "docker"})` : B.sandbox.is_mac ? "venv + macOS sandbox" : "venv";
function paneTry(c, pane, root) {
  const a = tryOf(c), d = a.detail || {}, p = d.plan, r = a.result;
  if (!p) { pane.innerHTML = `<p class="summary mb10">${esc(d.why)}</p><p class="muted">Gemma didn't write a runnable plan for this one.</p>`; return; }
  const st = a.status;
  const cmds = [...(p.setup || []), ...(p.smoke_test || [])].map(x => `<span class="p">$ </span>${esc(x)}`).join("\n");
  pane.innerHTML = `
    <p class="summary mb10">${esc(d.why)}</p>
    <h4 class="sec-h">The plan · ${esc(p.runtime)}</h4>
    <pre class="cmds">${cmds}</pre>
    <p class="muted small">Passes if the output contains <code>${esc(p.expected_output)}</code>. Sandbox on ${here()}: ${esc(r?.mode && r.mode !== "none" ? r.mode : sandboxLabel())}.</p>
    ${st === "refused" ? `<div class="danger"><strong>Refused by the safety check.</strong><ul>${(d.refused || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : `
    <div class="row">
      <button class="btn primary" id="tryGo" ${st === "running" ? "disabled" : ""}>${st === "proposed" ? "Run in sandbox…" : st === "running" ? "Running…" : "Run again in sandbox…"}</button>
      <span class="muted small">You'll see the exact commands and confirm first.</span>
    </div>`}
    <div id="termSlot"></div>`;
  const b = $("#tryGo", pane); if (b) b.onclick = () => openConfirm(c, a, root);
  if (st === "running") { $("#termSlot", pane).innerHTML = terminal(a, false); tickClock(a); pollAction(a.id); }
  else if ((st === "passed" || st === "failed") && r) $("#termSlot", pane).innerHTML = terminal(a, true);
}
function logHTML(text) {
  return String(text || "").split("\n").map(l => l.startsWith("[night-scout]") ? `<span class="nl">${esc(l)}</span>` : esc(l)).join("\n");
}
function terminal(a, done) {
  const r = a.result || {};
  const secs = typeof r.seconds === "number" ? r.seconds : null;
  const verdict = !done ? "" : a.status === "passed"
    ? `<div class="verdict pass"><span class="big-check">✅</span>Passed<span class="muted">· ${esc(r.reason)}${secs !== null ? ` · ${secs} s` : ""} · sandbox deleted</span></div>`
    : `<div class="verdict fail"><span class="big-check">❌</span>Failed<span class="muted">· ${esc(r.reason)}${secs !== null ? ` · ${secs} s` : ""}${r.mode && r.mode !== "none" ? " · sandbox deleted" : ""}</span></div>`;
  const body = done ? (r.log || "(no output)") : (r.log || "[night-scout] running in the sandbox… the full log appears here when it finishes");
  return `<div class="term">
    <div class="term-bar"><span class="lights"><i></i><i></i><i></i></span>sandbox · ${esc(r.mode && r.mode !== "none" ? r.mode : sandboxLabel())}<span class="spacer"></span><span id="tclock" data-aid="${a.id}">${secs !== null && done ? secs.toFixed(1) : "0.0"} s</span></div>
    <pre class="log" id="tlog" data-aid="${a.id}">${logHTML(body)}</pre>
    ${verdict}
  </div>`;
}
let clockT;
function tickClock(a) {
  clearInterval(clockT);
  const t0 = S.tryStart[a.id] || Date.parse(a.updated_at) || Date.now();
  const upd = () => {
    const el = $(`#tclock[data-aid="${a.id}"]`);
    if (!el || a.status !== "running") return clearInterval(clockT);
    el.textContent = Math.max(0, (Date.now() - t0) / 1000).toFixed(1) + " s";
  };
  upd(); clockT = setInterval(upd, 100);
}
const polls = {};
function pollAction(aid) {
  if (polls[aid]) return; polls[aid] = true;
  const tick = async () => {
    const r = await api("GET", `/api/action/${aid}`);
    if (!r.ok) { delete polls[aid]; return; }
    const a = r.data, c = card(a.item_id);
    let obj = a;
    if (c) { const i = c.actions.findIndex(x => x.id === aid); if (i >= 0) { Object.assign(c.actions[i], a); obj = c.actions[i]; } }
    if (a.status === "running") {
      const log = $(`#tlog[data-aid="${aid}"]`);
      if (log && a.result?.log) { log.innerHTML = logHTML(a.result.log); log.scrollTop = log.scrollHeight; }
      setTimeout(tick, 1000); return;
    }
    delete polls[aid]; delete S.tryStart[aid];
    if (c && String(cur) === String(c.id)) {
      showBig(c.id, 0, true); renderList(c.id);
      const lg = $("#tlog"); if (lg) lg.scrollTop = 1e6;
      $(".verdict")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } else if (c) { renderList(cur); toast(`<b>Try-it ${esc(obj.status)}</b> for “${esc(c.title.slice(0, 60))}”.`); }
  };
  tick();
}
async function openConfirm(c, a, root) {
  const r = await api("GET", `/api/action/${a.id}/plan`);
  if (!r.ok) return failToast(r);
  const P = r.data, p = P.plan, sb = P.sandbox;
  const where = sb.error ? null : sb.mode === "container"
    ? "A throwaway container with no access to your files, capped at 2 GB RAM, removed afterwards."
    : `A throwaway virtualenv in a temp folder with a clean environment: no tokens, no keys.${sb.is_mac ? " macOS fences it off from your home folder and other drives." : ""} Deleted afterwards.`;
  const blocked = !P.verdict.ok ? `<div class="danger"><strong>The safety check refuses this plan:</strong><ul>${P.verdict.problems.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>`
    : sb.error ? `<div class="danger"><strong>No sandbox available.</strong> ${esc(sb.error)}</div>` : "";
  const wrap = document.createElement("div");
  wrap.className = "sheet-wrap";
  wrap.innerHTML = `<div class="sheet">
    <p class="eyebrow">Run in sandbox</p>
    <h3>${esc(P.title)}</h3>
    <p class="muted small m0">From “${esc(P.item_title)}”, for <strong class="hi">${esc(P.project)}</strong>.</p>
    <h4 class="sec-h mt16">These exact commands will run</h4>
    <pre class="cmds">${[...p.setup, ...p.smoke_test].map(x => `<span class="p">$ </span>${esc(x)}`).join("\n")}</pre>
    <ul class="facts">
      <li><b>Where</b>${esc(where || "Nowhere yet: no sandbox is available on this machine.")}</li>
      <li><b>Time limit</b>${sb.timeout_s} seconds, then it's killed.</li>
      <li><b>Passes if</b>Every step exits cleanly and the output contains <code>${esc(p.expected_output)}</code>.</li>
      <li><b>Never</b>sudo, global installs, or anything inside your projects.</li>
    </ul>
    ${blocked || `<div class="supply"><strong>⚠ Supply-chain warning.</strong> These commands download and run packages from the internet, picked by a language model from a post it read. Packages can be malicious or typo-squatted. The sandbox limits the damage; it doesn't make a package trustworthy.</div>
    <label class="check"><input type="checkbox" id="ok"> I've read the commands and want to run them in the sandbox.</label>`}
    <div class="row">${blocked ? "" : `<button class="btn primary" id="run" disabled>Run in sandbox</button>`}<button class="btn ghost" id="cancel">${blocked ? "Close" : "Cancel"}</button></div>
  </div>`;
  root.appendChild(wrap);
  root.classList.add("locked"); root.style.setProperty("--tx", "0deg"); root.style.setProperty("--ty", "0deg");
  const close = () => { wrap.remove(); root.classList.remove("locked"); };
  const ok = $("#ok", wrap), run = $("#run", wrap);
  if (ok) ok.onchange = e => (run.disabled = !e.target.checked);
  $("#cancel", wrap).onclick = close;
  wrap.onclick = e => { if (e.target === wrap) close(); };
  if (run) run.onclick = async () => {
    run.disabled = true;
    const res = await api("POST", `/api/action/${a.id}/run`, { confirmed: true });
    close();
    if (!res.ok) return failToast(res);
    Object.assign(a, res.data, { status: "running", result: res.data.result || { log: "", reason: "starting…" } });
    S.tryStart[a.id] = Date.now();
    S.tab[c.id] = "try";
    showBig(c.id, 0, true); renderList(c.id);
    $("#tlog")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
}

/* ---- Research ---- */
const researchRunning = c => (S.research[c.id] || c.research_job?.state) === "running";
function paneResearch(c, pane) {
  const proj = c.best || "your projects";
  const what = c.source === "youtube" ? "transcript" : "article";
  if (researchRunning(c)) {
    const t0 = (S.research["t" + c.id] ||= Date.now());
    const steps = [`Fetching the full ${what}`, "Cleaning it to plain text", `Gemma writing the brief for ${proj}`];
    const at = [0, 2500, 5000];
    pane.innerHTML = `<div class="writing"><ul class="steps">${steps.map(s => `<li>○ ${esc(s)}</li>`).join("")}</ul>
      <div class="shimmer" data-w="92"></div><div class="shimmer" data-w="78"></div><div class="shimmer" data-w="85"></div><div class="shimmer" data-w="60"></div>
      <p class="muted small m-10">Takes about a minute on Gemma. You can switch cards; it keeps going.</p></div>`;
    fx(pane);
    const upd = () => {
      if (!document.body.contains(pane) || !researchRunning(c)) return;
      const el = Date.now() - t0; let k = 0;
      while (k < steps.length - 1 && el >= at[k + 1]) k++;
      $$(".steps li", pane).forEach((li, i) => { li.className = i < k ? "ok" : i === k ? "on" : ""; li.textContent = (i < k ? "✓ " : i === k ? "● " : "○ ") + steps[i]; });
      setTimeout(upd, 250);
    };
    upd(); pollResearch(c);
    return;
  }
  if (!c.research) {
    const prop = c.actions.find(a => a.kind === "research_more");
    const err = c.research_job?.state === "error" ? `<p class="warnline">Last try failed: ${esc(c.research_job.error)}</p>` : "";
    pane.innerHTML = `${prop ? `<p class="summary mb6"><strong class="hi">Gemma suggests:</strong> ${esc(prop.title)}</p><p class="muted small">${esc(prop.detail?.why || "")}</p>` : ""}
      <div class="writing solid">
        <p>Gemma reads the full ${what} and writes a short brief for <strong>${esc(proj)}</strong>: what's new, what it means for the project, and the caveats.</p>
        <div class="row mt0"><button class="btn primary" id="rGo">🔎 Research more</button><span class="muted small">Runs locally on Gemma · about a minute · costs nothing</span></div>
      </div>${err}`;
    $("#rGo", pane).onclick = () => startResearch(c);
    return;
  }
  pane.innerHTML = `<div class="brief-meta"><span>Research brief · ${esc(c.research_model || B.model.name)} · for ${esc(proj)}</span><span class="spacer"></span></div>
     <div class="brief">${md(c.research)}</div>
     <div class="row"><button class="btn ghost" id="rAgain">Rewrite</button><span class="muted small">Saved with the item. Rewrite asks Gemma again.</span></div>`;
  $("#rAgain", pane).onclick = () => startResearch(c);
}
async function startResearch(c) {
  const r = await api("POST", `/api/item/${c.id}/research`, {});
  if (!r.ok) return failToast(r);
  S.research[c.id] = "running"; S.research["t" + c.id] = Date.now();
  c.research_job = { state: "running" };
  if (String(cur) === String(c.id)) showBig(c.id, 0, true);
}
const rPolls = {};
function pollResearch(c) {
  if (rPolls[c.id]) return; rPolls[c.id] = true;
  const tick = async () => {
    const r = await api("GET", `/api/item/${c.id}/research`);
    if (r.ok && r.data.state === "running") { setTimeout(tick, 2000); return; }
    delete rPolls[c.id]; delete S.research[c.id]; delete S.research["t" + c.id];
    if (!r.ok) { c.research_job = { state: "error", error: r.error }; }
    else {
      c.research_job = { state: r.data.state, error: r.data.error };
      if (r.data.state === "done" && r.data.brief) { c.research = r.data.brief; c.research_model = r.data.model; }
    }
    const ok = r.ok && r.data.state !== "error";
    if (String(cur) === String(c.id)) { showBig(c.id, 0, true); renderList(c.id); }
    else toast(ok ? `<b>Brief ready</b> for “${esc(c.title.slice(0, 60))}”.` : `<b>Research failed</b> for “${esc(c.title.slice(0, 60))}”.`);
    if (!ok && String(cur) === String(c.id)) toast(`<b>Research failed.</b> ${esc(c.research_job.error || "")}`);
  };
  tick();
}

/* ---- Note ---- */
function paneNote(c, pane, aid) {
  const a = c.actions.find(x => x.id === aid); if (!a) return;
  const done = a.status === "done";
  const path = done ? a.result?.path : a.note_path;
  pane.innerHTML = `<p class="summary mb12">${esc(a.detail?.why)}</p>
    <div class="paper"><div class="paper-bar">📝 ${esc(path || `${a.project}/${B.notes_subdir}/`)}</div><div class="paper-body brief">${md(a.detail?.note_markdown)}</div></div>
    ${done ? `<div class="saved">✓ Saved to <code>${esc(path || "the project")}</code></div>`
      : `<div class="row"><button class="btn primary" id="nGo">Save note into ${esc(a.project)}</button>
         <span class="muted small">Writes this one markdown file under <code>${esc(B.notes_subdir)}/</code>. Nothing else in the project is touched.</span></div>`}`;
  const b = $("#nGo", pane);
  if (b) b.onclick = async () => {
    b.disabled = true;
    const r = await api("POST", `/api/action/${a.id}/note`, {});
    if (!r.ok) { b.disabled = false; return failToast(r); }
    a.status = "done"; a.result = { path: r.data.path };
    showBig(c.id, 0, true); renderList(c.id); toast(`Note saved into <b>${esc(a.project)}</b>.`);
  };
}

/* ---- Morning states: scout running, quiet, first run ---- */
let runPoll;
const scoutingFor = () => {
  if (RUN?.projects) return `${RUN.projects} project${RUN.projects === 1 ? "" : "s"}`;
  const n = (B.topics || []).length;
  return n ? `your ${n} topic${n === 1 ? "" : "s"}` : "you";
};
function renderMorningState(app, s) {
  const st = M.run?.stats || {};
  const projects = B.projects;
  const left = {
    running: `<p class="eyebrow">Tonight · ${esc(fmtDay(today()))}</p><h1>The scout<br>is <em>out.</em></h1><p class="sub">Started at ${esc(hhmm(RUN?.run?.started_at))}. Your digest will be here when it's done. Close this tab; it doesn't need you.</p>`,
    quiet: M.ignored && !(M.cards || []).length
      ? `<p class="eyebrow">Good morning · ${esc(dayLabel())}</p><h1>All <em>clear.</em></h1><p class="sub">You've hidden every pick for today.</p>`
      : `<p class="eyebrow">Good morning · ${esc(dayLabel())}</p><h1>A <em>quiet</em><br>morning.</h1><p class="sub">Nothing cleared the bar today. That's the point: no feed to scroll.</p>`,
    noprojects: `<p class="eyebrow">Actions</p><h1>Actions come<br>from <em>your projects.</em></h1><p class="sub">You're reading the news without projects, and that's fine: the <a href="#/digest">Digest</a> has the night's stories for your topics.</p>`,
    empty: `<p class="eyebrow">Welcome</p><h1>No digest<br><em>yet.</em></h1><p class="sub">${projects.length ? "Your projects are profiled. Run the scout once, or schedule it for tonight." : "Profile your projects, then run the scout once or schedule it for tonight."}</p>`,
  }[s];
  let right = "";
  if (s === "running") right = `<h2 class="serif">Scouting ${RUN.sources_total} sources for ${scoutingFor()}</h2>
      <ul class="stages" id="stages"></ul>
      <p class="muted small mt22">${esc(judge().model)} ${judge().local ? `on ${here()} · nothing leaves the machine` : `via ${esc(judge().provider)} · item text goes to ${esc(judge().provider)}`}. This page follows along every few seconds.</p>`;
  if (s === "quiet") right = (M.ignored && !(M.cards || []).length)
    ? `<h2 class="serif">Nothing left on the list.</h2><p class="summary">Hidden picks stay hidden. Tomorrow's run brings new ones.</p>
       <div class="row"><a class="btn" href="#/sources">Check sources</a><a class="btn ghost" href="#/projects">Tune what projects care about</a></div>`
    : `<h2 class="serif">Nothing worth your morning.</h2>
      <p class="summary">Gemma read <strong>${st.scored ?? 0}</strong> of the <strong>${st.new ?? 0}</strong> new items.${M.best_score != null ? ` The best fit was <strong>${pct(M.best_score)}%</strong>;` : ""} the bar is <strong>${pct(M.bar)}%</strong>.</p>
      <div class="row"><a class="btn" href="#/sources">Check sources</a><a class="btn ghost" href="#/projects">Tune what projects care about</a></div>`;
  if (s === "noprojects") right = `<h2 class="serif">Add a project to get actions.</h2>
      <p class="summary">Point Night Scout at a folder you're building in. Each night Gemma checks every item against it and tells you what to do:</p>
      <ul class="why-actions"><li><span class="kind k-issue">Issue</span><span>A security advisory or breaking bug in something you depend on.</span></li>
        <li><span class="kind k-change">Change</span><span>A release or API change that affects your code.</span></li>
        <li><span class="kind k-improvement">Improvement</span><span>A tool or technique worth trying on it, with a sandboxed <b>Try it</b>.</span></li>
        <li><span>💡</span><span>New side-project ideas from the night's best items.</span></li></ul>
      <div class="row"><a class="btn primary" href="#/setup">Add a project ›</a><a class="btn ghost" href="#/digest">Back to the Digest</a></div>
      <p class="muted small mt22">Gemma reads only the README, manifests and file types. Your code never leaves this machine.</p>`;
  if (s === "empty") right = projects.length
    ? `<h2 class="serif">Two commands.</h2>
      <p class="summary">Run one pass now (about 15 minutes on Gemma 4 12B):</p><div class="cmdline">$ night-scout run</div>
      <p class="summary">Or let it run every night at ${esc(B.schedule.time)} while you sleep:</p><div class="cmdline">$ night-scout schedule --install</div>
      <p class="muted small">Profile: ${projects.map(esc).join(" · ")} · <a href="#/projects">review it</a></p>`
    : B.setup?.needed ? `<h2 class="serif">Let's set it up.</h2>
      <p class="summary">Six short steps: pick the model, add your projects, choose what it reads and when it goes out.</p>
      <div class="row"><a class="btn primary" href="#/setup">Set up the scout ›</a></div>
      <p class="muted small mt22">Prefer the terminal? <code>night-scout init ~/code/my-app</code>, then <code>night-scout run</code>.</p>`
    : `<h2 class="serif">Three commands.</h2>
      <p class="summary">Point the scout at your project folders and let Gemma profile them:</p><div class="cmdline">$ night-scout init ~/code/my-app</div>
      <p class="summary">Run one pass now:</p><div class="cmdline">$ night-scout run</div>
      <p class="summary">Or every night at ${esc(B.schedule.time)}:</p><div class="cmdline">$ night-scout schedule --install</div>`;
  app.innerHTML = `<div class="frame morning-state"><div class="col-left"><section class="hero">${left}</section></div>
    <div class="stage"><div class="big enter"><div class="big-scroll"><div class="state-card">${right}</div></div></div></div></div>`;
  wireTilt($(".stage", app));
  cur = null;
  if (s === "running") { drawStages(); watchRun(); }
}
function drawStages() {
  const ul = $("#stages"); if (!ul || !RUN?.progress) return;
  const p = RUN.progress, stats = RUN.run?.stats || {};
  const stages = STAGES.filter(s => s[0] !== "ideas" || B.features.ideas || p.stage === "ideas" || (p.stages_done || []).includes("ideas"));
  let k = stages.findIndex(s => s[0] === p.stage); if (k < 0) k = 0;
  const doneSet = new Set(p.stages_done || []);
  ul.innerHTML = stages.map(([key, name, what, statKey, unit], i) => {
    const done = i < k || doneSet.has(key), now = !done && i === k;
    const total = now ? p.total : null, val = now ? p.done : stats[statKey];
    const w = done ? 100 : now && total ? Math.round((val || 0) / total * 100) : 0;
    const num = now ? (total ? `${val ?? 0}/${total}` : "…") : done && val != null ? `${val} ${unit}` : done ? "✓" : "";
    return `<li class="${done ? "done" : now ? "now" : ""}"><span class="ic">${done ? "✓" : now ? "●" : "○"}</span>
      <span><strong>${esc(name)}</strong> <span class="muted small">${esc(what())}</span></span>
      <span class="meter"><i data-w="${w}"></i></span><span class="num">${esc(num)}</span></li>`;
  }).join("");
  fx(ul);
}
/* poll GET /api/run: every 3 s while a run is going, every 60 s otherwise */
function watchRun() {
  clearTimeout(runPoll);
  runPoll = setTimeout(async () => {
    const was = !!RUN?.running;
    const r = await loadRun();
    if (!r.ok) return watchRun();
    let page = (location.hash || "#/digest").split("/")[1] || "digest";
    if (page === "actions") page = "morning";
    const home = page === "morning" || page === "digest";
    if (was && !RUN.running) {
      await Promise.all([loadBoot(), loadMorning()]);
      NS.digestReset?.();
      if (home) { reset(); route(); }
      else chrome(page);
      const n = visibleCards().length;
      toast(B.projects.length ? `<b>Good morning.</b> ${words(n)} thing${n === 1 ? "" : "s"} worth doing.` : `<b>Good morning.</b> Your Digest is ready.`);
    } else if (!was && RUN.running) {
      if (home) { NS.digestReset?.(); reset(); route(); } else chrome(page);
    } else if (RUN.running) {
      drawStages(); chrome(page);
    }
    watchRun();
  }, RUN?.running ? 3000 : 60000);
}

/* ---- 3D tilt: the big card leans toward the mouse (camera never pulls back) ---- */
function wireTilt(stage) {
  if (!stage) return;
  stage.onmousemove = e => {
    const b = $(".big:not(.leave)", stage); if (!b || b.classList.contains("locked")) return;
    const r = stage.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
    b.style.setProperty("--ty", (x * 3.2).toFixed(2) + "deg"); b.style.setProperty("--tx", (-y * 2.4).toFixed(2) + "deg");
  };
  stage.onmouseleave = () => $$(".big", stage).forEach(b => { b.style.setProperty("--tx", "0deg"); b.style.setProperty("--ty", "0deg"); });
}

/* ================= SOURCES ================= */
const TYPE = {
  youtube: ["YouTube channels", "Transcripts included"], rss: ["RSS / Atom feeds", "Blogs, release notes"],
  hackernews: ["Hacker News", "Lists and keyword searches"], reddit: ["Reddit", "Public JSON, no login"],
  bluesky: ["Bluesky", "Public AppView search"], mastodon: ["Mastodon", "Public hashtag timelines"], github: ["GitHub", "Releases and repo search"],
};
let SRC = null;
function renderSources() {
  chrome("sources"); cur = null;
  if (NS.srcStale) { SRC = null; NS.srcStale = false; }
  if (!SRC) {
    loading("Loading sources…");
    api("GET", "/api/sources").then(r => { if (!r.ok) return errorView(r); SRC = r.data; if ((location.hash.split("/")[1] || "") === "sources") renderSources(); });
    return;
  }
  const rows = SRC.statuses, w = SRC.weights, byKey = Object.fromEntries(rows.map(r => [r.key, r]));
  const model = SRC.pipeline.find(p => p.key === "pipeline:model");
  const learned = [...Object.entries(w.source).map(([k, v]) => [k, v, 1]), ...Object.entries(w.topic).map(([k, v]) => ["#" + k, v, 0])].filter(x => x[1]);
  const wbar = v => `<span class="wbar"><i class="${v >= 0 ? "up" : "down"}" data-w="${Math.abs(v) / 0.8 * 100}"></i></span>`;
  const groups = {};
  SRC.catalog.forEach(r => (groups[r.type] ||= []).push(r));
  $("#app").innerHTML = `<div class="frame">
    <div class="col-left">
      <section class="hero"><p class="eyebrow">Sources</p><h1>What the scout<br><em>reads</em> overnight.</h1>
        <p class="sub">Official feeds and public APIs, no logins. Searching a whole YouTube channel for your key terms uses YouTube's API, or yt-dlp if you haven't added a key.</p></section>
      <div class="panel"><h4 class="sec-h">Last run${SRC.run ? " · " + esc(fmtAt(SRC.run.started_at)) : ""}</h4>
        ${rows.length ? rows.map(r => `<div class="srcrow ${r.ok ? "" : "bad"}"><span class="okdot"></span><span>${esc(r.label)}</span>
          <span class="num">${r.ok ? `${r.fetched} · ${r.new} new` : esc(r.message)}</span></div>`).join("") : `<p class="small muted m0">No runs yet.</p>`}
        ${model ? `<div class="srcrow"><span class="center">✦</span><span>${esc(model.label)}</span><span class="num">${esc(model.message)}</span></div>` : ""}
      </div>
      <div class="panel"><h4 class="sec-h">Learned from your votes</h4>
        ${learned.length ? learned.map(([k, v, isSrc]) => `<div class="weight"><span>${esc(k)}${isSrc && v <= -0.3 ? ' <span class="neg">· muted</span>' : ""}</span>${wbar(v)}<span class="${v < 0 ? "neg" : "pos"}">${fmt(v)}</span></div>`).join("")
          : `<p class="small muted m0">No votes yet. 👍 / 👎 on a Morning card nudges its source and topics here.</p>`}
      </div>
    </div>
    <div class="stage"><div class="big enter"><div class="big-scroll">
      <div class="big-head"><h2 class="serif m0">${SRC.catalog.length} source${SRC.catalog.length === 1 ? "" : "s"}</h2><span class="spacer"></span>
        <div class="seg"><button data-t="feeds" class="${S.srcTab === "feeds" ? "on" : ""}">Feeds</button><button data-t="yaml" class="${S.srcTab === "yaml" ? "on" : ""}">config.yaml</button></div></div>
      <div class="mt20" id="srcPane"></div>
    </div></div></div>
  </div>`;
  fx($("#app"));
  wireTilt($(".stage"));
  $$(".seg button").forEach(b => b.onclick = () => { S.srcTab = b.dataset.t; renderSources(); });
  const pane = $("#srcPane");
  if (S.srcTab === "yaml") {
    pane.innerHTML = `<textarea spellcheck="false" id="yaml">${esc(SRC.yaml)}</textarea>
      <div class="savebar"><button class="btn primary" id="ySave">Save</button><span class="muted small">Checked before saving, so a typo can't break tonight's run.</span></div>`;
    $("#ySave").onclick = async () => {
      const text = $("#yaml").value;
      const r = await api("POST", "/api/sources/yaml", { yaml: text });
      if (!r.ok) return toast(`<b>Not saved.</b> ${esc(r.error)}`);
      SRC = null; loadBoot();
      toast(`<b>Saved.</b> Valid config; tonight's ${esc(B.schedule.time)} run uses it.`);
      renderSources();
    };
    return;
  }
  pane.innerHTML = Object.entries(groups).map(([t, list]) => `<div class="group">
      <div class="group-h">${badge(t, 1)}<h4>${esc(TYPE[t]?.[0] || t)}</h4><span class="muted">${esc(TYPE[t]?.[1] || "")}</span></div>
      ${list.map(r => { const off = r.enabled === false, st = byKey[r.key], sw = w.source[r.label];
        return `<div class="feed ${off ? "off" : ""}"><div><div>${esc(r.label)}${sw ? ` <span class="${sw < 0 ? "neg" : "pos"} fs11">${fmt(sw)}</span>` : ""}</div><div class="what">${esc(r.detail)}</div>${scoutChips(r)}</div>
          <span class="stat">${off ? "off" : !st ? "first fetch tonight" : st.ok ? `${st.new} new last run` : `<span class="neg">${esc(st.message)}</span>`}</span>
          ${SCOUTABLE_ROW.includes(r.type) ? `<button class="btn ghost sm" data-edit="${esc(r.key)}">${S.editKey === r.key ? "Close" : "Edit"}</button>` : ""}
          <button class="toggle ${off ? "" : "on"}" data-k="${esc(r.key)}" title="${off ? "Off" : "On"}"></button></div>
          ${S.editKey === r.key ? `<div class="feededit">${scoutFields(r.type === "rss" ? "website" : r.type, r.look_back_days || LB(), (r.scout_terms || []).join(", "), true)}
            <div class="row"><button class="btn primary sm" id="eSave">Save</button><button class="btn ghost sm" id="eCancel">Cancel</button></div></div>` : ""}`; }).join("")}
    </div>`).join("") + `
    <div class="group" id="addSrc"><h4 class="sec-h">Add a source</h4>
      <div class="types">${ADD.map(t => `<button class="type ${S.addType === t.k ? "on" : ""} ${t.off ? "off" : ""}" data-k="${t.k}">${badge(t.icon)}<b>${esc(t.name)}</b><small>${esc(t.hint)}</small></button>`).join("")}</div>
      ${(() => { const t = ADD.find(x => x.k === S.addType);
        return t.off ? `<div class="supply mt12"><strong>Not available.</strong> ${esc(t.why)}</div>`
          : `<div class="addform"><input type="text" id="aVal" placeholder="${esc(t.ph)}"><button class="btn primary" id="aGo">Add ${esc(t.name.toLowerCase())}</button></div>
             <p class="muted small m-8">${esc(t.note)}</p>
             ${SCOUTABLE_ADD.includes(t.k) ? scoutFields(t.k, S.addLb || LB(), S.addTerms || "") : ""}`; })()}
      ${S.scoutResult ? scoutResult(S.scoutResult) : ""}
    </div>`;
  $$(".toggle", pane).forEach(b => b.onclick = async () => {
    const item = SRC.catalog.find(x => x.key === b.dataset.k); if (!item) return;
    const want = item.enabled === false;
    const r = await api("POST", "/api/sources/toggle", { key: item.key, enabled: want });
    if (!r.ok) return failToast(r, "Turning sources on and off");
    item.enabled = want; renderSources();
    toast(`${want ? "<b>On.</b>" : "<b>Off.</b>"} ${esc(item.label)} ${want ? "is back in" : "sits out"} tonight's run.`);
  });
  $$(".type", pane).forEach(b => b.onclick = () => { S.addType = b.dataset.k; renderSources(); $("#addSrc").scrollIntoView({ block: "nearest" }); $("#aVal")?.focus(); });
  wireScoutFields(pane);
  $$("[data-edit]", pane).forEach(b => b.onclick = () => { S.editKey = S.editKey === b.dataset.edit ? null : b.dataset.edit; renderSources(); });
  const eSave = $("#eSave");
  if (eSave) {
    $("#eCancel").onclick = () => { S.editKey = null; renderSources(); };
    eSave.onclick = async () => {
      const box = eSave.closest(".feededit"), item = SRC.catalog.find(x => x.key === S.editKey);
      const body = { key: S.editKey, look_back_days: +$(".lbval", box).textContent, scout_terms: splitTerms($(".terms-in", box).value) };
      eSave.disabled = true;
      const was = new Set((item.scout_terms || []).map(x => x.toLowerCase()));
      const fresh = body.scout_terms.filter(x => !was.has(x.toLowerCase()));
      if (fresh.length) { eSave.textContent = item.type === "youtube" ? "Searching the channel…" : "Checking the feed…"; }
      const r = await api("POST", "/api/sources/scout", body);
      eSave.disabled = false;
      if (!r.ok) { eSave.textContent = "Save"; return failToast(r, "Changing sources from here"); }
      Object.assign(item, { look_back_days: body.look_back_days, scout_terms: body.scout_terms }); S.editKey = null;
      const kind = item.type === "rss" ? "website" : item.type;
      S.scoutResult = r.data?.scout ? { ...r.data.scout, label: item.label, type: kind, edited: true } : null;
      renderSources();
      if (r.data?.scout_error) toast(`<b>Saved</b>, but the key-term search didn't run: ${esc(r.data.scout_error)}`);
      else if (!S.scoutResult) toast(body.scout_terms.length ? `<b>Saved.</b> New posts from ${esc(item.label)} that mention ${esc(body.scout_terms.join(", "))} go first.` : `<b>Saved.</b> ${esc(item.label)} has no key terms.`);
      if (S.scoutResult) $("#addSrc .scoutres")?.scrollIntoView({ block: "nearest" });
    };
  }
  const srx = $("#srX"); if (srx) srx.onclick = () => { S.scoutResult = null; renderSources(); };
  const goBtn = $("#aGo");
  const add = async () => {
    const t = ADD.find(x => x.k === S.addType), v = $("#aVal").value.trim(); if (!v) return $("#aVal").focus();
    const body = { type: t.k, value: v };
    if (SCOUTABLE_ADD.includes(t.k)) { body.look_back_days = S.addLb || LB(); body.scout_terms = splitTerms($("#aTerms")?.value); }
    goBtn.disabled = true; const label = goBtn.textContent;
    if (t.discover) goBtn.textContent = "Looking for a feed…";
    if (body.scout_terms?.length) goBtn.textContent = t.k === "youtube" ? "Searching the channel…" : "Checking the feed…";
    const r = await api("POST", "/api/sources/add", body);
    goBtn.disabled = false; goBtn.textContent = label;
    if (!r.ok) return failToast(r, "Adding sources from here");
    SRC = null; S.addTerms = ""; S.addLb = LB();
    S.scoutResult = r.data?.scout ? { ...r.data.scout, label: r.data.label || v, type: t.k, existing: r.data.existing } : null;
    if (r.data?.scout_error) toast(`<b>${r.data.existing ? "Updated" : "Added"} ${esc(r.data.label || v)}</b>, but the key-term search didn't run: ${esc(r.data.scout_error)} New posts that mention a term still go first.`);
    else if (!S.scoutResult) toast(r.data?.existing ? `<b>Updated.</b> You already follow ${esc(r.data.label)}; its look back and key terms are saved.`
      : `<b>Added.</b> ${esc(r.data?.label || v)} · reads ${body.look_back_days || LB()} days back at ${esc(B.schedule.time)} tonight.`);
    renderSources();
  };
  if (goBtn) { goBtn.onclick = add; $("#aVal").onkeydown = e => { if (e.key === "Enter") add(); }; }
}
/* Look back + Scout key terms (feed-type sources) */
const LB = () => SRC?.look_back_default || 3, SCOUTABLE_ADD = ["youtube", "website", "newsletter", "podcast", "reddit", "arxiv"], SCOUTABLE_ROW = ["youtube", "rss", "reddit"];
const splitTerms = v => [...new Set(String(v || "").split(",").map(x => x.trim()).filter(Boolean))].slice(0, 10);
function scoutChips(r) {
  const lb = r.look_back_days && r.look_back_days !== LB() ? `<span class="chip">looks back ${r.look_back_days} day${r.look_back_days === 1 ? "" : "s"}</span>` : "";
  const tm = (r.scout_terms || []).map(x => `<span class="chip term">🔎 ${esc(x)}</span>`).join("");
  return lb || tm ? `<div class="scoutchips">${lb}${tm}</div>` : "";
}
function scoutFields(kind, lb, terms, editing) {
  const yt = kind === "youtube", key = SRC?.youtube_api_key;
  const lbNote = yt ? "How many days of videos the first night reads (and how far it catches up after nights the scout didn't run). YouTube's feed lists only the 15 newest videos, so a busy channel may not reach that far."
    : "How many days of posts the first night reads (and how far it catches up after nights the scout didn't run).";
  const tNote = editing ? (yt ? "New uploads that mention a term go to the front of Gemma's queue. Saving new terms also searches the whole channel for them once." : "New posts that mention a term go to the front of Gemma's queue.")
    : yt ? "Once, now: searches the whole channel, up to 10 videos per term, any age. Then every night: new uploads that mention a term go to the front of Gemma's queue."
    : "Once, now: checks every post the feed still lists, any age. Then every night: new posts that mention a term go to the front of Gemma's queue. Feeds only; posts the feed no longer lists can't be searched.";
  const via = yt ? `<p class="small muted m-4 via">${key ? "🔑 Channel search uses your YouTube Data API key."
    : SRC?.ytdlp ? `Channel search uses <b>yt-dlp</b>. <a href="#/settings/chat">Add a YouTube API key</a> to use the official API instead.`
    : `<span class="neg">Channel search needs a <a href="#/settings/chat">YouTube API key</a> or yt-dlp (<code>pip install yt-dlp</code>).</span> New uploads that mention a term still go first.`}</p>` : "";
  return `<div class="scoutopts">
    <div class="sopt"><span class="slabel">Look back</span>
      <span class="stepper"><button type="button" data-lb="-1" ${lb <= 1 ? "disabled" : ""} aria-label="Fewer days">−</button><b class="lbval">${lb}</b><span>day${lb === 1 ? "" : "s"}</span><button type="button" data-lb="1" ${lb >= 14 ? "disabled" : ""} aria-label="More days">+</button></span>
      <small class="muted">${esc(lbNote)}</small></div>
    <div class="sopt"><span class="slabel">Scout key terms <small class="muted">comma delimited · optional</small></span>
      <input type="text" class="terms-in" ${editing ? "" : 'id="aTerms"'} value="${esc(terms)}" placeholder="${yt ? "kling, veo, seedance" : "mcp, ollama"}" autocomplete="off">
      <small class="muted">${esc(tNote)} A term matches a whole word in the title or description, any case.</small>${via}</div></div>`;
}
function wireScoutFields(root) {
  $$(".scoutopts", root).forEach(box => {
    const editing = !!box.closest(".feededit");
    $$("[data-lb]", box).forEach(b => b.onclick = () => {
      const v = Math.min(14, Math.max(1, +$(".lbval", box).textContent + +b.dataset.lb));
      if (editing) { $(".lbval", box).textContent = v; $$("[data-lb]", box).forEach(x => x.disabled = (+x.dataset.lb < 0 ? v <= 1 : v >= 14)); return; }
      S.addLb = v; S.addTerms = $("#aTerms")?.value || ""; const val = $("#aVal")?.value || ""; renderSources(); if ($("#aVal")) $("#aVal").value = val;
    });
  });
  const ti = $("#aTerms"); if (ti) ti.oninput = () => { S.addTerms = ti.value; };
}
function scoutResult(x) {
  const yt = x.type === "youtube", all = x.matches || [];
  const termRow = t => `<div class="sr-term"><b>🔎 ${esc(t.term)}</b><span>${t.matched} match${t.matched === 1 ? "" : "es"}</span>
    <span class="muted small">${yt ? `${t.checked} YouTube result${t.checked === 1 ? "" : "s"} checked; ${t.checked - t.matched} didn't mention it` : `${t.checked} posts in the feed checked`}</span></div>`;
  return `<div class="scoutres"><button class="x" id="srX" title="Dismiss">✕</button>
    <p class="sr-h"><b>✓ ${esc(x.label)} ${x.existing ? "updated" : x.edited ? "saved" : "added"}.</b> ${x.existing ? "You already follow it, so its look back and key terms were updated. " : ""}${yt ? `Searched the whole channel with ${x.via === "api" ? "the YouTube Data API" : "yt-dlp"}.` : "Checked its feed."}</p>
    ${x.terms.map(termRow).join("")}
    ${all.length ? `<div class="sr-list">${all.map(m => `<a class="sr-item" ${NS.ext(m.url)}><span class="mono small muted">${esc(m.date)}</span><span>${esc(m.title)}</span><span class="sr-tags">${m.terms.map(t => `<span class="chip term">${esc(t)}</span>`).join("")}</span></a>`).join("")}</div>` : ""}
    <p class="small m-8">${x.queued ? `<b>${x.queued} ${yt ? "video" : "post"}${x.queued === 1 ? "" : "s"} go first in the next run (${esc(B.schedule.time)})</b>, ahead of the usual queue. If that's more than the run's ${B.thresholds?.max_scored ?? 40}-item limit, the rest go first the night after.`
      : all.length ? "Gemma has already read these, so nothing new is queued." : "Nothing matched yet."} From now on, new ${yt ? "uploads" : "posts"} that mention a term go first${x.edited ? "" : `, and the first night reads the last ${x.look_back_days} day${x.look_back_days === 1 ? "" : "s"}`}.</p></div>`;
}
const ADD = [
  { k: "website", icon: "rss", name: "Website or blog", hint: "finds its RSS feed", ph: "https://example.com", note: "Paste the site's address. Night Scout looks for its RSS or Atom feed; sites without one can't be followed (no scraping).", discover: 1 },
  { k: "youtube", icon: "youtube", name: "YouTube channel", hint: "transcripts included", ph: "@fireship or a channel URL", note: "Gemma judges what's said in each video, not just the title." },
  { k: "newsletter", icon: "rss", name: "Newsletter", hint: "Substack, Ghost, Buttondown", ph: "https://name.substack.com", note: "Any newsletter with a public archive feed.", discover: 1 },
  { k: "podcast", icon: "rss", name: "Podcast", hint: "show notes and titles", ph: "https://example.com/podcast.xml", note: "Paste the podcast's RSS feed." },
  { k: "reddit", icon: "reddit", name: "Subreddit", hint: "public, no login", ph: "LocalLLaMA", note: "Newest posts from a public subreddit." },
  { k: "hackernews", icon: "hackernews", name: "Hacker News", hint: "keyword search", ph: "mcp", note: "Stories matching the keyword." },
  { k: "bluesky", icon: "bluesky", name: "Bluesky", hint: "search or account", ph: "mcp server  or  @simonwillison.net", note: "Public posts via the Bluesky AppView. No login." },
  { k: "mastodon", icon: "mastodon", name: "Mastodon", hint: "hashtag", ph: "#ollama", note: "Public hashtag timeline on mastodon.social (change the server in config.yaml)." },
  { k: "github", icon: "github", name: "GitHub", hint: "releases or search", ph: "modelcontextprotocol/typescript-sdk", note: "owner/repo follows releases; anything else is a repo search." },
  { k: "arxiv", icon: "rss", name: "arXiv", hint: "paper category", ph: "cs.CL", note: "New papers in a category (via arXiv's RSS)." },
  { k: "x", icon: "x", name: "X (Twitter)", hint: "not available", off: 1, why: "X has no free public API and its terms don't allow scraping, so Night Scout can't follow X accounts. Many of the same people also post on Bluesky or Mastodon; add them there." },
];

/* ================= PROJECTS ================= */
const SECTIONS = [
  ["interests", "Interests", "Topics Gemma looks for in every item.", 0],
  ["would_want_to_know", "Would want to know about", "Specific news that should always make the cut.", 0],
  ["key_dependencies", "Key dependencies", "Releases and advisories for these come up as Changes or Issues.", 1],
  ["stack", "Stack", "Languages and frameworks; helps judge if a tool fits.", 0],
];
let PJ = null;
const clone = x => JSON.parse(JSON.stringify(x));
async function loadProjects() {
  const r = await api("GET", "/api/projects");
  if (r.ok) { PJ = r.data; S.prof = clone(PJ.projects); S.edited = {}; S.rebuilding = PJ.job?.state === "running"; }
  return r;
}
function renderProfile(name) {
  chrome("projects"); cur = null;
  if (!PJ || !M) {
    loading("Loading projects…");
    Promise.all([PJ ? { ok: true } : loadProjects(), M ? { ok: true } : loadMorning()]).then(([a, b]) => {
      if (!a.ok) return errorView(a); if (!b.ok) return errorView(b);
      if ((location.hash.split("/")[1] || "") === "projects") renderProfile(name);
    });
    return;
  }
  const P = S.prof, p = P.find(x => x.name === name) || P[0];
  const picksFor = n => (M.cards || []).filter(c => c.best === n && c.vote !== 0);
  $("#app").innerHTML = `<div class="frame">
    <div class="col-left">
      <section class="hero"><p class="eyebrow">Projects</p><h1>What your projects<br><em>care</em> about.</h1>
        <p class="sub">The scout judges every item against these. Gemma drafted them from each project's README and dependency files; you have the last word.</p></section>
      ${P.map((x, i) => { const pk = picksFor(x.name), iss = pk.filter(c => c.impact === "issue").length;
        return `<div class="projcard ${x === p ? "sel" : ""}" data-n="${esc(x.name)}" data-delay="${i * 50}">
        <h3>${esc(x.name)}${S.edited[x.name] ? ` <span class="edited">●</span>` : ""}</h3><p>${esc(x.purpose)}</p><div class="path">${esc(x.path)}</div>
        <div class="lastnight">${pk.length ? `${pk.length} pick${pk.length === 1 ? "" : "s"} last night${iss ? ` · <span class="neg">${iss} issue${iss === 1 ? "" : "s"}</span>` : ""}` : "nothing last night"}</div></div>`; }).join("")}
      <button class="addproj" id="addProj">+ Add a project folder</button>
    </div>
    <div class="stage"><div class="big enter"><div class="big-scroll" id="profPane"></div></div></div>
  </div>`;
  fx($("#app"));
  wireTilt($(".stage"));
  $$(".projcard").forEach(el => el.onclick = () => { go(`#/projects/${encodeURIComponent(el.dataset.n)}`); document.body.classList.add("card-open"); });
  if (name) document.body.classList.add("card-open");
  $("#addProj").onclick = () => toast(`Add the folder under <code>projects:</code> in config.yaml, then <b>Rebuild with Gemma</b>. <span class="muted">(A folder picker comes later.)</span>`);
  const pane = $("#profPane");
  if (S.rebuilding) {
    pane.innerHTML = `<h2 class="serif mt0">Rebuilding profiles…</h2><div class="writing"><ul class="steps">
      ${(P.length ? P : [{ name: "your projects" }]).map(x => `<li class="on">● Reading ${esc(x.name)}: README, manifests, languages</li>`).join("")}</ul>
      <div class="shimmer" data-w="90"></div><div class="shimmer" data-w="70"></div><div class="shimmer" data-w="80"></div></div>`;
    fx(pane); pollRebuild();
    return;
  }
  if (!p) {
    pane.innerHTML = `<h2 class="serif mt0">No projects yet.</h2><p class="summary">Point the scout at a project folder and Gemma drafts its profile:</p>
      <div class="cmdline">$ night-scout init ~/code/my-app</div>
      <div class="row"><button class="btn ghost" id="pRebuild">Rebuild with Gemma…</button></div>`;
    wireRebuild(() => renderProfile(name));
    return;
  }
  const st = M.run?.stats || {}, nsrc = B.sources_count;
  const how = S.howHidden ? `<button class="howlink" id="howShow">ⓘ How this page is used</button>` : `
    <section class="how">
      <div class="how-flow">
        <div class="hf"><b>${nsrc} sources</b><small>${st.new ?? 0} new items last night</small></div><span class="hf-arrow">→</span>
        <div class="hf hot"><b>Checked against each project</b><small>${P.length} project${P.length === 1 ? "" : "s"} · interests, wants, dependencies</small></div><span class="hf-arrow">→</span>
        <div class="hf"><b>Your morning</b><small><span class="kind k-issue">Issue</span> <span class="kind k-change">Change</span> <span class="kind k-improvement">Improvement</span> <span class="kind k-relevant">Relevant</span></small></div>
      </div>
      <p>Every night, each new item from your <a href="#/sources">Sources</a> is checked against every project here. Gemma looks for an <b>issue</b> to check (a vulnerable dependency, a breaking bug), a <b>change</b> to track (a release or API change), an <b>improvement</b> worth adopting, or news that's simply <b>relevant</b>. Gemma also suggests <b>💡 new side projects</b> inspired by what it read and what you already build.</p>
      <p class="muted small m0">The lists below are exactly what Gemma reads when it judges. The sharper they are, the better your mornings.</p>
      <button class="btn ghost mt10" id="howHide">Got it</button>
    </section>`;
  const listSec = ([key, title, hint, mono]) => `<section class="lsec">
      <div class="lsec-h"><h4>${esc(title)}</h4><span class="count">${(p[key] || []).length}</span><span class="muted small">${esc(hint)}</span></div>
      <ul class="lrows ${mono ? "mono" : ""}" data-key="${key}">${(p[key] || []).map((s, i) => `<li><span>${esc(s)}</span><button class="x" data-i="${i}" title="Remove">✕</button></li>`).join("")}
        <li class="addrow"><input type="text" maxlength="200" placeholder="+ Add ${esc(title.toLowerCase().replace(/s$/, ""))}" data-add="${key}"></li></ul></section>`;
  const chipSec = ([key, title, , mono]) => `<div class="${key === "interests" || key === "would_want_to_know" ? "full" : ""}"><h4 class="sec-h">${esc(title)}</h4>
      <div class="chips" data-key="${key}">${(p[key] || []).map((s, i) => `<span class="chip ${mono ? "mono" : ""}">${esc(s)}<button class="x" data-i="${i}" title="Remove">✕</button></span>`).join("")}
      <span class="chip add"><input type="text" maxlength="200" placeholder="+ add" data-add="${key}"></span></div></div>`;
  const pk = picksFor(p.name);
  const edited = !!S.edited[p.name];
  pane.innerHTML = `${how}
    <div class="big-head"><a class="back" href="#/projects">‹ Projects</a><span class="muted small mono">${esc(p.path)}</span><span class="spacer"></span>
      ${edited ? `<span class="edited">● edited</span>` : ""}
      <div class="seg"><button data-v="list" class="${S.profView === "list" ? "on" : ""}">☰ List</button><button data-v="tags" class="${S.profView === "tags" ? "on" : ""}">◇ Tags</button></div></div>
    <h2 class="serif mt6">${esc(p.name)}</h2>
    <p class="summary editable purpose" contenteditable="true" id="purpose">${esc(p.purpose)}</p>
    ${S.profView === "list" ? `<div class="lgrid">${SECTIONS.map(listSec).join("")}</div>` : `<div class="pgrid">${SECTIONS.map(chipSec).join("")}</div>`}
    <section class="lsec mt8"><div class="lsec-h"><h4>Last night for ${esc(p.name)}</h4><span class="count">${pk.length}</span><span class="muted small">Picks this project produced on the Morning page.</span></div>
      ${pk.length ? pk.map(c => `<a class="insp-row" href="#/morning/${c.id}">${kindPill(c)}<span>${esc(c.title)}</span><span class="muted small">${pct(c.best_score)}%</span></a>`).join("") : `<p class="muted small">Nothing cleared the bar for this project last night.</p>`}</section>
    <div class="foot-note"><span>Drafted by ${esc(p.generated_by || "you")}${PJ.generated_at ? " · " + esc(fmtShort(PJ.generated_at)) : ""}</span><span class="spacer"></span>
      ${edited ? `<button class="btn primary" id="pSave">Save project</button>` : ""}
      <button class="btn ghost" id="pRebuild">Rebuild with Gemma…</button></div>`;
  const rerender = () => renderProfile(p.name);
  const markEdited = () => { S.edited[p.name] = true; };
  $$(".seg button", pane).forEach(b => b.onclick = () => { S.profView = b.dataset.v; rerender(); });
  const hh = $("#howHide"); if (hh) hh.onclick = () => { S.howHidden = true; rerender(); };
  const hs = $("#howShow"); if (hs) hs.onclick = () => { S.howHidden = false; rerender(); };
  $$(".x", pane).forEach(b => b.onclick = () => { const k = b.closest("[data-key]").dataset.key; p[k].splice(+b.dataset.i, 1); markEdited(); rerender(); });
  $$("input[data-add]", pane).forEach(inp => inp.onkeydown = e => {
    if (e.key !== "Enter" || !inp.value.trim()) return;
    const k = inp.dataset.add; (p[k] ||= []).push(inp.value.trim()); markEdited(); rerender();
    const box = $(`[data-key="${k}"]`), items = box.querySelectorAll(":scope > li:not(.addrow), :scope > .chip:not(.add)");
    items[items.length - 1]?.classList.add("new"); $(`input[data-add="${k}"]`).focus();
  });
  const save = async () => {
    const b = $("#pSave"); if (b) b.disabled = true;
    const body = { purpose: p.purpose, stack: p.stack || [], key_dependencies: p.key_dependencies || [], interests: p.interests || [], would_want_to_know: p.would_want_to_know || [] };
    const r = await api("PUT", `/api/projects/${encodeURIComponent(p.name)}`, body);
    if (!r.ok) { if (b) b.disabled = false; return toast(`<b>Not saved.</b> ${esc(r.error)}`); }
    const i = PJ.projects.findIndex(x => x.name === p.name);
    if (i >= 0) PJ.projects[i] = r.data.project;
    Object.assign(p, clone(r.data.project));
    delete S.edited[p.name]; rerender();
    toast(`<b>Saved.</b> Tonight's run judges against the new profile.`);
  };
  const wireSave = () => { const b = $("#pSave"); if (b) b.onclick = save; };
  $("#purpose").oninput = () => {
    p.purpose = $("#purpose").textContent;
    if (!S.edited[p.name]) {
      markEdited();
      $(".foot-note .spacer").insertAdjacentHTML("afterend", `<button class="btn primary" id="pSave">Save project</button>`);
      $(".big-head .spacer", pane).insertAdjacentHTML("afterend", `<span class="edited">● edited</span>`);
      wireSave();
    }
  };
  wireSave();
  wireRebuild(rerender);
}
function wireRebuild(rerender) {
  const b = $("#pRebuild"); if (!b) return;
  b.onclick = async () => {
    if (!b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "Overwrites your edits · click again"; b.classList.add("armed"); return; }
    b.disabled = true;
    const r = await api("POST", "/api/projects/rebuild", {});
    if (!r.ok) { b.disabled = false; return failToast(r); }
    S.rebuilding = true; rerender();
  };
}
let rbPoll = false;
function pollRebuild() {
  if (rbPoll) return; rbPoll = true;
  const tick = async () => {
    const r = await api("GET", "/api/projects/rebuild");
    if (r.ok && r.data.state === "running") { setTimeout(tick, 2000); return; }
    rbPoll = false; S.rebuilding = false;
    await loadProjects(); await loadBoot();
    if ((location.hash.split("/")[1] || "") === "projects") { const n = location.hash.split("/")[2]; renderProfile(n ? decodeURIComponent(n) : n); }
    if (!r.ok || r.data.state === "error") toast(`<b>Rebuild failed.</b> ${esc(r.ok ? r.data.error : r.error)}`);
    else toast(`<b>Profiles rebuilt</b> by Gemma from ${PJ.projects.length} project folder${PJ.projects.length === 1 ? "" : "s"}.`);
  };
  tick();
}

/* ================= keys, sky, boot ================= */
document.addEventListener("keydown", e => {
  if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable || $(".sheet-wrap") || $("#tour")) return;
  const page = (location.hash || "#/digest").split("/")[1] || "digest";
  if (!["morning", "actions"].includes(page) || !M || morningState() !== "normal") return;
  const ids = visibleCards().map(c => c.id), i = ids.indexOf(+cur);
  if (["ArrowDown", "j"].includes(e.key) && i < ids.length - 1) { e.preventDefault(); go(`#/morning/${ids[i + 1]}`); }
  if (["ArrowUp", "k"].includes(e.key) && i > 0) { e.preventDefault(); go(`#/morning/${ids[i - 1]}`); }
  if (/^[1-4]$/.test(e.key) && card(cur)) { const t = tabsFor(card(cur))[+e.key - 1]; if (t) { S.tab[cur] = t[0]; showBig(cur, 0, true); } }
});

(function sky() {
  const cv = $("#sky"); if (!cv) return;
  const g = cv.getContext("2d"); let W, H, stars = [], shoot = null;
  const size = () => { W = cv.width = innerWidth * devicePixelRatio; H = cv.height = innerHeight * devicePixelRatio;
    stars = Array.from({ length: 240 }, () => ({ x: Math.random() * W, y: Math.random() * H * 0.9, r: (Math.random() * 1.1 + 0.3) * devicePixelRatio, p: Math.random() * 6.28, s: 0.3 + Math.random() * 1.2, warm: Math.random() < 0.12 })); };
  size(); addEventListener("resize", size);
  const frame = t => {
    g.clearRect(0, 0, W, H);
    for (const s of stars) { const a = 0.35 + 0.45 * Math.sin(s.p + t / 1000 * s.s); g.fillStyle = s.warm ? `rgba(255,222,170,${a})` : `rgba(205,220,255,${a})`; g.beginPath(); g.arc(s.x, s.y, s.r, 0, 6.28); g.fill(); }
    if (!shoot && Math.random() < 0.0016) shoot = { x: Math.random() * W * 0.7 + W * 0.2, y: Math.random() * H * 0.3, l: 0 };
    if (shoot) { shoot.l += 14 * devicePixelRatio; const x = shoot.x - shoot.l, y = shoot.y + shoot.l * 0.45;
      const gr = g.createLinearGradient(x, y, x + 120 * devicePixelRatio, y - 54 * devicePixelRatio); gr.addColorStop(0, "rgba(255,230,190,.8)"); gr.addColorStop(1, "rgba(255,230,190,0)");
      g.strokeStyle = gr; g.lineWidth = 1.4 * devicePixelRatio; g.beginPath(); g.moveTo(x, y); g.lineTo(x + 120 * devicePixelRatio, y - 54 * devicePixelRatio); g.stroke(); if (shoot.l > 600 * devicePixelRatio) shoot = null; }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
})();

async function boot() {
  loading("Waking the scout…");
  const [b] = await Promise.all([loadBoot(), loadRun()]);
  if (!b.ok) return errorView(b);
  // First launch (no projects profiled yet): open Setup instead of an empty Morning, unless
  // this tab already chose "Skip setup for now".
  const page = (location.hash || "#/digest").split("/")[1] || "digest";
  if (B.setup?.needed && ["digest", "morning", "actions"].includes(page) && !RUN?.running && !sessionStorage.getItem("nsSetupSkipped")) {
    location.replace("#/setup"); // replace: Back shouldn't bounce into the empty Morning
  }
  route();
  watchRun();
}

/* Setup's "Run now": wait (up to a minute) for the new run to register, then follow it on
   Morning. Returns whether it showed up. */
async function kickRun() {
  const until = Date.now() + 60000;
  for (;;) {
    await loadRun();
    if (RUN?.running || Date.now() > until) break;
    await new Promise(r => setTimeout(r, 1000));
  }
  M = null; reset(); go("#/morning"); watchRun();
  return !!RUN?.running;
}

Object.assign(NS, { runActive: () => !!RUN?.running, loadBoot, kickRun, $, $$, esc, md, toast, go, badge, wireTilt, chrome, card, kindPill, words, fx, api, failToast, ext, isWeb, scoutImg, reset, loading, errorView,
  S, visibleCards, loadMorning, route });
/* live views of the loaded data (Object.assign would copy today's value, not a getter) */
Object.defineProperties(NS, { B: { get: () => B }, M: { get: () => M } });
window.addEventListener("DOMContentLoaded", boot);
})();
