/* Night Scout: the Digest (#/digest), the page that opens first. Approved mockup v2 (Oct 22):
   left, a compact story list with a match ring; middle, one big card with the stories in full,
   5 per page; right, the chat drawer (static/chat.js, when present: NS.openChat/closeChat).
   Strict CSP: no inline styles or handlers; meter widths come from data-* via fx(). */
(() => {
"use strict";
const { $, $$, esc, toast, go, chrome, api, failToast, fx, loading, errorView, badge } = NS;
const B = () => NS.B;
const pct = x => Math.round((x || 0) * 100);
const PER = 5, PER_REST = 10;
const WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen", "Twenty"];
const words = n => WORDS[n] || String(n);
const isWeb = u => typeof u === "string" && /^https?:\/\//i.test(u);
const ext = u => isWeb(u) ? `href="${esc(u)}" target="_blank" rel="noopener noreferrer"` : "";
const dayDate = d => new Date(d + "T12:00:00");
const dayLong = d => dayDate(d).toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
const dayShort = d => dayDate(d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
const todayIso = () => { const t = new Date(); return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`; };
const fmtShort = iso => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }); };
const ring = (v, sz = 34) => { const p = pct(v); return `<span class="ring ${p >= 90 ? "hot" : ""}" title="${p}% match"><svg viewBox="0 0 36 36" width="${sz}" height="${sz}" aria-hidden="true"><circle class="rb" cx="18" cy="18" r="15.9"/><circle class="rf" cx="18" cy="18" r="15.9" stroke-dasharray="${p} 100" transform="rotate(-90 18 18)"/></svg><b>${p}</b></span>`; };
const KIND = { issue: "Issue", change: "Change", improvement: "Improvement", relevant: "Relevant" };
const kindPill = k => KIND[k] ? `<span class="kind k-${k}">${KIND[k]}</span>` : "";
const title = i => i.headline || i.title;

/* ---------- state ---------- */
let D = null;                 // GET /api/digest
const V = { filter: "all", view: "digest", page: 1, cur: null };
let held = false;            // a jump holds its story until you scroll (see watchFeed)
const hasChat = () => typeof NS.openChat === "function";
// A reload can finish after you've gone to another page: only redraw if you're still on the Digest.
const onDigest = () => (location.hash.split("/")[1] || "digest") === "digest";

async function load(day) {
  const r = await api("GET", "/api/digest" + (day ? `?day=${encodeURIComponent(day)}` : ""));
  if (r.ok) D = r.data;
  return r;
}
const byId = id => D && [...D.stories, ...D.rest, ...D.saved].find(i => i.id === +id);
function feedItems() {
  if (V.view === "rest") return D.rest;
  if (V.filter === "saved") return D.saved;
  return V.filter === "all" ? D.stories : D.stories.filter(i => i.topic === V.filter);
}
const perPage = () => V.view === "rest" ? PER_REST : PER;
const pages = () => Math.max(1, Math.ceil(feedItems().length / perPage()));

/* ---------- page ---------- */
NS.pages.digest = async (a) => {
  chrome("digest");
  if (!D) {
    loading("Opening your Digest…");
    const r = await load();
    if (!r.ok) return errorView(r);
    if ((location.hash.split("/")[1] || "digest") !== "digest") return;
  }
  if (a === "topics") { render(); return topicsSheet(); }
  if (a === "more") { V.view = "rest"; V.page = 1; V.cur = null; render(); return; }
  if (a === "saved") { V.view = "digest"; V.filter = "saved"; V.page = 1; render(); return; }
  if (a && byId(a)) return showStory(+a);
  if (V.view === "rest" && !a) V.view = "digest";
  render();
};
NS.digestReset = () => { D = null; V.cur = null; V.page = 1; V.view = "digest"; V.filter = "all"; };

function emptyState() {
  const running = NS.runActive && NS.runActive();
  const noTopics = !D.digest_topics.length;
  const left = running
    ? `<p class="eyebrow">Tonight · ${esc(dayLong(todayIso()))}</p><h1>The scout<br>is <em>out.</em></h1><p class="sub">Your Digest will be here when it's done.</p>`
    : `<p class="eyebrow">Digest</p><h1>No Digest<br><em>yet.</em></h1><p class="sub">${noTopics ? "Tell the scout what to look for, then run it once." : "Run the scout once, or let it go out tonight."}</p>`;
  const right = running
    ? `<h2 class="serif">Out scouting.</h2><p class="summary">Follow the run live on the <a href="#/actions">Actions</a> page. Close this tab if you like; it doesn't need you.</p>`
    : noTopics
      ? `<h2 class="serif">What should it look for?</h2><p class="summary">Add a few topics you follow, in your own words. Projects are optional.</p>
         <div class="row"><a class="btn primary" href="#/digest/topics">Add topics ›</a><a class="btn ghost" href="#/setup">Run setup</a></div>`
      : `<h2 class="serif">One run away.</h2><p class="summary">Run one pass now (about 10–15 minutes with Gemma):</p>${NS.runRow ? NS.runRow(true) : `<div class="cmdline">$ night-scout run</div>`}
         <p class="summary">Or let it run every night at ${esc(B().schedule.time)}. Your topics: ${D.digest_topics.map(esc).join(" · ")}. <a href="#/digest/topics">Edit</a></p>`;
  $("#app").innerHTML = `<div class="frame morning-state digest-empty"><div class="col-left"><section class="hero">${left}</section></div>
    <div class="stage"><div class="big enter"><div class="big-scroll"><div class="state-card">${right}</div></div></div></div></div>`;
  NS.wireTilt($(".stage"));
  NS.wireRun?.($("#app"));
}

function render() {
  chrome("digest");
  if (!D.day) return emptyState();
  const items = D.stories, rest = D.rest, saved = D.saved, st = D.stats || { new: 0, judged: 0 };
  const topics = D.digest_topics;
  if (V.filter === "saved" && !saved.length) V.filter = "all";
  if (V.filter !== "all" && V.filter !== "saved" && !topics.includes(V.filter)) V.filter = "all";
  const counts = Object.fromEntries(topics.map(t => [t, items.filter(i => i.topic === t).length]));
  const listOf = V.filter === "saved" ? saved : V.filter === "all" ? items : items.filter(i => i.topic === V.filter);
  const tOf = i => i.topic || "Other";
  const groups = [...topics, "Other"].filter(t => listOf.some(i => tOf(i) === t));
  const latest = D.days[0] === D.day;
  const openChat = !!$(".frame.digest.chat-open");
  $("#app").innerHTML = `<div class="frame digest ${openChat ? "chat-open" : ""}">
    <div class="col-left">
      <section class="hero">
        <p class="eyebrow">${D.day === todayIso() ? "Tonight's digest" : "Digest"} · ${esc(dayLong(D.day))}</p>
        <h1><em>${words(items.length)}</em> stor${items.length === 1 ? "y" : "ies"}<br>for your topics.</h1>
        <p class="sub">Gemma read ${st.judged} of the ${st.new} new items against your ${topics.length} topic${topics.length === 1 ? "" : "s"}${D.topics.length ? "" : D.projects.length ? " (your project names; add your own topics to sort by them)" : ""}.${D.projects.length ? ` <a href="#/actions">Actions</a> has what to do about your projects.` : ""}</p>
        <div class="listenrow" id="listenRow" hidden></div>
        ${NS.runRow ? NS.runRow() : ""}
        ${D.days.length > 1 ? `<div class="days">${[...D.days].slice(0, 7).reverse().map(d => `<button data-day="${esc(d)}" class="${d === D.day ? "on" : ""}">${esc(dayShort(d))}</button>`).join("")}</div>` : ""}
      </section>
      <div class="tfilter" id="tfilter">
        <button data-f="all" class="${V.view === "digest" && V.filter === "all" ? "on" : ""}">All<b>${items.length}</b></button>
        ${topics.map(t => `<button data-f="${esc(t)}" class="${V.view === "digest" && V.filter === t ? "on" : ""}">${esc(t)}<b>${counts[t] || 0}</b></button>`).join("")}
        ${saved.length ? `<button data-f="saved" class="${V.view === "digest" && V.filter === "saved" ? "on" : ""}">🔖 Saved<b>${saved.length}</b></button>` : ""}
        <button class="edit" id="editTopics">✎ Topics</button>
      </div>
      <div class="list" id="list">
        ${groups.map(t => `<p class="list-h topic-h">${esc(t)} <span>${listOf.filter(i => tOf(i) === t).length}</span></p>` + listOf.filter(i => tOf(i) === t).map(i => `
          <article class="mini dmini" data-id="${i.id}">
            <div>
              <div class="meta">${badge(i.source)}<span>${esc(i.source_name)}</span>${i.video_id ? `<span class="vtag">▶ video</span>` : ""}${i.action ? `<span class="inact">IN ACTIONS</span>` : ""}${i.saved ? `<span class="svtag" title="Saved">🔖</span>` : ""}</div>
              <h3>${esc(title(i))}</h3>
            </div>
            ${ring(i.judged && i.relevance != null ? i.relevance : i.match)}
          </article>`).join("")).join("") || `<div class="empty-box">${V.filter === "all" ? `Nothing cleared the ${pct(D.bar)}% bar ${latest ? "tonight" : "that night"}. The rest is below.` : `Nothing in ${esc(V.filter === "saved" ? "Saved" : V.filter)}.`}</div>`}
        <button class="more-t ${V.view === "rest" ? "on" : ""}" id="moreT"><span>Everything else from ${latest ? "tonight" : "that night"} · ${rest.length}<small>under the bar or not read by Gemma · ranked by similarity</small></span><span class="chev">›</span></button>
      </div>
    </div>
    <div class="stage" id="stage"><div class="big feedcard enter"><div class="big-scroll" id="feed"></div></div></div>
    <aside class="chat" id="chat"></aside>
  </div>`;
  $$(".dmini").forEach(m => m.onclick = () => go(`#/digest/${m.dataset.id}`));
  $$("#tfilter [data-f]").forEach(b => b.onclick = () => { V.filter = b.dataset.f; V.view = "digest"; V.page = 1; V.cur = null; if (location.hash !== "#/digest") history.replaceState(null, "", "#/digest"); render(); });
  $$(".days [data-day]").forEach(b => b.onclick = async () => {
    if (b.dataset.day === D.day) return;
    const r = await load(b.dataset.day); if (!r.ok) return failToast(r);
    if (!onDigest()) return;
    V.page = 1; V.view = "digest"; V.filter = "all"; V.cur = null;
    if (location.hash !== "#/digest") history.replaceState(null, "", "#/digest");
    render();
  });
  $("#editTopics").onclick = () => go("#/digest/topics");
  $("#moreT").onclick = () => go("#/digest/more");
  drawFeed();
  NS.listen?.status(D.day); // ▶ Listen (voice add-on only): hero row, story buttons, NOW READING
  NS.wireRun?.($("#app")); // ▶ Run now / Scout is out
  if (openChat && hasChat()) NS.openChat(NS.chatCtx?.() || { kind: "digest", day: D.day });
}

function drawFeed(scrollTo) {
  const feed = $("#feed"); if (!feed) return;
  const all = feedItems(), n = pages();
  V.page = Math.min(Math.max(1, V.page), n);
  const per = perPage(), start = (V.page - 1) * per, shown = all.slice(start, start + per);
  const pager = n > 1 ? `<div class="pager">${V.page > 1 ? `<button data-pg="${V.page - 1}" title="Previous page">‹</button>` : `<button disabled>‹</button>`}
    ${Array.from({ length: n }, (_, k) => `<button data-pg="${k + 1}" class="${k + 1 === V.page ? "on" : ""}">${k + 1}</button>`).join("")}
    ${V.page < n ? `<button data-pg="${V.page + 1}" title="Next page">›</button>` : `<button disabled>›</button>`}</div>` : "";
  const label = V.view === "rest" ? `Everything else · ${all.length} item${all.length === 1 ? "" : "s"}, ranked by similarity` : V.filter === "saved" ? `Saved · ${all.length}` : `${V.filter === "all" ? "All topics" : esc(V.filter)} · ${all.length} stor${all.length === 1 ? "y" : "ies"}`;
  const latest = D.days[0] === D.day;
  feed.innerHTML = `<div class="feedhead"><span class="fh-l">${V.view === "rest" ? `<button class="btn ghost sm" id="backDigest">‹ Digest</button>` : `<a class="back" href="#/digest">‹ Digest</a>`}<b>${label}</b><span class="muted">${all.length ? `${start + 1}–${Math.min(start + per, all.length)}` : ""}</span></span>${pager}
      ${hasChat() ? `<button class="btn ghost sm" id="askAll">${NS.scoutImg(16)} Ask about ${V.view === "rest" ? "these" : latest ? "tonight" : "this Digest"}</button>` : ""}</div>
    ${shown.map(i => story(i)).join(`<hr class="sep">`) || `<div class="state-card"><h2 class="serif">Nothing here.</h2><p class="summary">${V.view === "rest" ? "Every item made the Digest." : `Open <b>Everything else</b> on the left for the rest of ${latest ? "tonight" : "that night"}.`}</p></div>`}
    ${pager ? `<div class="feedfoot">${pager}</div>` : ""}`;
  $$("[data-pg]", feed).forEach(b => b.onclick = () => { V.page = +b.dataset.pg; V.cur = null; history.replaceState(null, "", V.view === "rest" ? "#/digest/more" : "#/digest"); drawFeed(); feed.scrollTop = 0; markList(); });
  $("#backDigest") && ($("#backDigest").onclick = () => { V.view = "digest"; V.page = 1; go("#/digest"); });
  $("#askAll") && ($("#askAll").onclick = () => NS.openChat({ kind: "digest", day: D.day }));
  $$("[data-a]", feed).forEach(b => b.onclick = () => act(b.dataset.a, +b.dataset.id, b));
  $$(".yt-play", feed).forEach(b => b.onclick = () => {
    const w = b.closest(".yt"), v = b.dataset.v;
    if (!/^[\w-]{11}$/.test(v)) return;
    w.innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${v}?autoplay=1&rel=0" title="YouTube video" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>`;
  });
  const target = scrollTo ?? V.cur;
  if (target) {
    const el = $(`.story[data-id="${target}"]`, feed);
    if (el) {
      const top = Math.max(0, Math.min(el.offsetTop - ($(".feedhead", feed)?.offsetHeight || 0) - 8, feed.scrollHeight - feed.clientHeight));
      held = true;
      feed.scrollTop = top; el.classList.add("hl"); setTimeout(() => el.classList.remove("hl"), 1400);
    }
  }
  $("#moreT")?.classList.toggle("on", V.view === "rest");
  watchFeed(); markList(); NS.listen?.storyButtons();
}

function story(i) {
  const top = i.judged && i.topic && i.relevance >= D.bar;
  const score = i.judged && i.topic ? `${ring(i.relevance, 30)}<span class="muted">judged by Gemma${top ? "" : ` · under the ${pct(D.bar)}% bar`}</span>`
    : i.judged ? `<span class="muted">Gemma: fits none of your topics</span>`
    : `${ring(i.match, 30)}<span class="muted">similarity only · not read by Gemma</span>`;
  const vid = i.video_id && /^[\w-]{11}$/.test(i.video_id) ? i.video_id : null;
  return `<article class="story" data-id="${i.id}">
    <div class="big-head">${badge(i.source, 1)}<span><strong class="hi">${esc(i.source_name)}</strong>${i.author && i.author !== i.source_name ? " · " + esc(i.author) : ""}${i.published ? " · " + esc(fmtShort(i.published)) : ""}</span>
      ${i.also_seen?.length ? `<span class="buzz" title="Also seen on ${esc(i.also_seen.join(", "))}">also on ${i.also_seen.length} more</span>` : ""}
      <span class="spacer"></span>
      <div class="votes">
        ${NS.listen?.available() && D.stories.some(x => x.id === i.id) ? `<button class="vote listen" type="button" data-listen="${i.id}" title="Listen to this story">▶ Listen</button>` : ""}
        <button class="vote ${i.saved ? "on" : ""}" data-a="save" data-id="${i.id}" title="${i.saved ? "Saved: click to unsave" : "Save for later"}">🔖</button>
        <button class="vote" data-a="less" data-id="${i.id}" title="Not for me: hide it and rank items like it lower">👎</button>
        <button class="vote" data-a="mute" data-id="${i.id}" title="Mute ${esc(i.source_name)}">🔇</button>
        ${hasChat() ? `<button class="vote ask" data-a="ask" data-id="${i.id}" title="Ask Gemma about this story">${NS.scoutImg(16)} Ask</button>` : ""}
      </div>
      ${isWeb(i.url) ? `<a class="open" ${ext(i.url)}>Open ↗</a>` : ""}
    </div>
    <h2>${esc(title(i))}</h2>
    ${i.headline ? `<p class="orig">Original title: ${esc(i.title)}</p>` : ""}
    <div class="derive">${i.topic ? `<span class="topicpill">${esc(i.topic)}</span>` : ""}${score}</div>
    ${i.action ? `<div class="alsoact">${kindPill(i.action.kind)}<span>Also in <b>Actions</b> for <b>${esc(i.action.project)}</b>${i.action.next_step ? `: ${esc(i.action.next_step)}` : ""}</span><a class="btn" href="#/morning/${i.id}">Open in Actions ›</a></div>` : ""}
    ${vid ? `<div class="yt"><button class="yt-play" data-v="${vid}" title="Play here"><img src="https://i.ytimg.com/vi/${vid}/hqdefault.jpg" alt="" loading="lazy"><span class="yt-btn">▶</span><span class="yt-cap">Play here · youtube-nocookie.com</span></button></div>` : ""}
    ${i.summary ? `<h4 class="sec-h">Summary · Gemma</h4><p class="summary">${esc(i.summary)}</p>` : `<p class="notjudged">Not read by Gemma: it ranked below the ${(D.stats || {}).judged || 0} items Gemma had time for. Shown so nothing from the night is hidden.</p>`}
    ${i.outline?.length ? `<details class="outline" open><summary>Key points</summary><ul>${i.outline.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
    <details class="fromsrc" ${i.summary ? "" : "open"}><summary>From the source</summary><p class="excerpt">${esc(i.text || "(title and link only)")}</p></details>
  </article>`;
}

async function act(a, id, btn) {
  const i = byId(id); if (!i) return;
  const keep = () => { const f = $("#feed"); return f ? f.scrollTop : 0; };
  if (a === "ask") return NS.openChat({ kind: "item", id, title: title(i) });
  btn.disabled = true;
  if (a === "save") {
    const r = await api("POST", `/api/item/${id}/save`, { saved: !i.saved });
    if (!r.ok) { btn.disabled = false; return failToast(r, "Saving"); }
    const sc = keep(); await load(D.day); if (onDigest()) { render(); const f = $("#feed"); if (f) f.scrollTop = sc; }
    return toast(r.data.saved ? "<b>Saved.</b> It stays under 🔖 Saved after the Digest rolls over." : "Removed from Saved.");
  }
  if (a === "less") {
    const r = await api("POST", `/api/item/${id}/vote`, { value: "down" });
    if (!r.ok) { btn.disabled = false; return failToast(r, "Hiding"); }
    await load(D.day); V.cur = null; if (onDigest()) render();
    return toast(`<b>Hidden.</b> Items like it rank lower from tonight.`, async () => {
      await api("POST", `/api/item/${id}/vote`, { value: "clear" }); await load(D.day); if (onDigest()) render();
    });
  }
  if (a === "mute") {
    const r = await api("POST", "/api/source/mute", { name: i.source_name, muted: true });
    if (!r.ok) { btn.disabled = false; return failToast(r, "Muting"); }
    await load(D.day); V.cur = null; if (onDigest()) render();
    return toast(`<b>${esc(i.source_name)} muted.</b> Its items leave the Digest.`, async () => {
      await api("POST", "/api/source/mute", { name: i.source_name, muted: false }); await load(D.day); if (onDigest()) render();
    });
  }
}

/* the list follows the feed: the story under the feed's top edge is highlighted on the left.
   A jump (list click, link, j/k) holds its story, even one too near the end to reach the top;
   only your own scrolling (wheel, touch, drag, Page/Space/Home/End) moves the highlight. */
function watchFeed() {
  const feed = $("#feed"); if (!feed) return;
  feed.onwheel = feed.ontouchstart = feed.onpointerdown = () => { held = false; };
  feed.onscroll = () => {
    if (held) return;
    const line = feed.getBoundingClientRect().top + ($(".feedhead", feed)?.offsetHeight || 0) + 24;
    let cur = null;
    for (const s of $$(".story", feed)) { if (s.getBoundingClientRect().top <= line) cur = s; else break; }
    if (cur && +cur.dataset.id !== +V.cur) { V.cur = +cur.dataset.id; markList(); }
  };
}
function markList() { $$(".dmini").forEach(m => m.classList.toggle("sel", +m.dataset.id === +V.cur)); }

function showStory(id) {
  const i = byId(id); if (!i) return render();
  const inStories = D.stories.some(x => x.id === id);
  if (V.filter === "saved" && D.saved.some(x => x.id === id)) V.view = "digest";
  else if (inStories) { V.view = "digest"; if (V.filter !== "all" && i.topic !== V.filter) V.filter = "all"; }
  else if (D.rest.some(x => x.id === id)) V.view = "rest";
  else { V.view = "digest"; V.filter = "saved"; }
  if (!$(".frame.digest")) render();
  else $$("#tfilter [data-f]").forEach(b => b.classList.toggle("on", V.view === "digest" && b.dataset.f === V.filter));
  const k = feedItems().findIndex(x => x.id === id);
  V.page = Math.floor(Math.max(0, k) / perPage()) + 1;
  V.cur = id;
  drawFeed(id);
  document.body.classList.add("card-open");
}

/* ---------- topics sheet ---------- */
async function topicsSheet() {
  const big = $("#stage .big"); if (!big) return;
  $(".sheet-wrap", big)?.remove();
  let topics = [...D.topics];
  const w = document.createElement("div"); w.className = "sheet-wrap";
  const close = () => { w.remove(); big.classList.remove("locked"); history.replaceState(null, "", "#/digest"); };
  const saveNow = async next => {
    const r = await api("PUT", "/api/topics", { topics: next });
    if (!r.ok) { failToast(r, "Saving topics"); return false; }
    topics = r.data.topics; D.topics = topics; return true;
  };
  const draw = () => {
    w.innerHTML = `<div class="sheet"><p class="eyebrow">Your topics</p><h3>What should the Digest look for?</h3>
      <p class="muted small">Type a topic in your own words. Gemma files every item it reads under one of these. ${D.projects.length ? "Your projects' interests still drive Actions; these drive the Digest." : "No projects needed."}</p>
      <div class="topicbox">${topics.length ? topics.map(t => `<span class="chip">${esc(t)}<button class="x" data-rm="${esc(t)}" title="Remove ${esc(t)}">✕</button></span>`).join("") : `<span class="empty">No topics yet.</span>`}</div>
      <div class="addrow"><input type="text" id="tIn" maxlength="60" placeholder="Add a topic, e.g. Rust, robotics, AI policy" autocomplete="off"><button class="btn" id="tAdd">Add</button></div>
      <p class="muted small mt12">New topics fill from the next run. Removing one hides its stories once you close this.</p>
      <div class="row"><button class="btn primary" id="tDone">Done</button></div></div>`;
    $("#tAdd", w).onclick = async () => {
      const v = $("#tIn", w).value.trim(); if (!v) return $("#tIn", w).focus();
      if (topics.some(t => t.toLowerCase() === v.toLowerCase())) { $("#tIn", w).value = ""; return toast(`<b>${esc(v)}</b> is already a topic.`); }
      if (await saveNow([...topics, v])) { draw(); $("#tIn", w).focus(); toast(`<b>${esc(v)}</b> added. It fills from the next run.`); }
    };
    $("#tIn", w).onkeydown = e => { if (e.key === "Enter") $("#tAdd", w).click(); };
    $$("[data-rm]", w).forEach(b => b.onclick = async () => { if (await saveNow(topics.filter(t => t !== b.dataset.rm))) draw(); });
    $("#tDone", w).onclick = async () => { close(); await load(D.day); if (onDigest()) render(); };
  };
  draw(); big.appendChild(w); big.classList.add("locked");
  w.onclick = e => { if (e.target === w) $("#tDone", w).click(); };
  $("#tIn", w)?.focus();
}

/* keys: ↑ ↓ / j k move through the feed */
document.addEventListener("keydown", e => {
  if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable || $(".sheet-wrap")) return;
  if ((location.hash || "#/digest").split("/")[1] !== "digest" && location.hash) return;
  if (!D || !D.day || $("#chat:focus-within")) return;
  if (["PageDown", "PageUp", " ", "Home", "End"].includes(e.key)) held = false;
  const ids = feedItems().map(i => i.id), k = ids.indexOf(+V.cur);
  if (["ArrowDown", "j"].includes(e.key) && k < ids.length - 1) { e.preventDefault(); go(`#/digest/${ids[k + 1]}`); }
  if (["ArrowUp", "k"].includes(e.key) && k > 0) { e.preventDefault(); go(`#/digest/${ids[k - 1]}`); }
});
})();
