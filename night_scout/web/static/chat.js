/* Night Scout: the ✦ Ask Gemma chat drawer (Track B).
   NS.openChat(ctx) / NS.closeChat(). ctx = {kind: "digest", day: "YYYY-MM-DD"} or {kind: "item", id, title}.
   Renders into the page's #chat aside when there is one (the Digest page: toggles `chat-open` on
   .frame.digest), else into a floating aside of its own. `chat-full` on body below 760 px.
   Strict CSP: no inline styles or handlers; everything from the model is escaped first. */
(() => {
"use strict";
const { $, $$, esc, api, failToast } = NS;
const PHONE = 760;
const C = { open: false, ctx: null, key: "", meta: null, thread: null, data: null, timer: null, tick: null,
  pending: null, openCards: new Set(), float: false, err: null };

const keyOf = ctx => ctx.kind === "item" ? `item:${ctx.id}` : `digest:${ctx.day || ""}`;
const remember = (key, id) => { try { sessionStorage.setItem("nsChat:" + key, String(id)); } catch (e) { /* private mode */ } };
const recall = key => { try { return +sessionStorage.getItem("nsChat:" + key) || null; } catch (e) { return null; } };
const forget = key => { try { sessionStorage.removeItem("nsChat:" + key); } catch (e) { /* private mode */ } };
const page = () => (location.hash || "#/morning").split("/")[1] || "morning";
const here = () => (C.meta?.platform || NS.B?.platform) === "darwin" ? "this Mac" : "this machine";

/* ---- tiny safe markdown: links (http/https only), bold, code, bullets, paragraphs; the rest is escaped ---- */
function inline(text) {
  return String(text).split(/(`[^`\n]+`)/g).map(part => {
    if (/^`[^`\n]+`$/.test(part)) return `<code>${esc(part.slice(1, -1))}</code>`;
    return esc(part)
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  }).join("");
}
function md(src) {
  const out = []; let list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } };
  for (const raw of String(src || "").replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    const li = raw.match(/^(\s*)(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (li) { flushPara(); (list = list || []).push(`<li${li[1].length >= 2 ? ' class="sub"' : ""}>${inline(li[2])}</li>`); continue; }
    if (!line) { flushPara(); flushList(); continue; }
    flushList();
    if (/^#{1,6}\s/.test(line)) { flushPara(); out.push(`<p><b>${inline(line.replace(/^#+\s*/, ""))}</b></p>`); continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { flushPara(); continue; }
    para.push(line);
  }
  flushPara(); flushList();
  return out.join("");
}

/* ---- where the drawer lives ---- */
function host() {
  const el = document.getElementById("chat");
  if (el && !el.classList.contains("chat-float")) { removeFloat(); C.float = false; return el; }
  if (el) return el;
  const aside = document.createElement("aside");
  aside.id = "chat"; aside.className = "chat chat-float";
  document.body.appendChild(aside);
  C.float = true;
  return aside;
}
function removeFloat() { $$("#chat.chat-float").forEach(e => e.remove()); }
function layout() {
  document.body.classList.toggle("chat-full", C.open && innerWidth <= PHONE);
  $(".frame.digest")?.classList.toggle("chat-open", C.open && !C.float);
}

/* ---- open / close ---- */
async function openChat(ctx) {
  if (!ctx || !["digest", "item"].includes(ctx.kind)) return;
  const clean = ctx.kind === "item" ? { kind: "item", id: +ctx.id, title: ctx.title || "" } : { kind: "digest", day: ctx.day || NS.M?.day || "" };
  if (clean.kind === "item" && !(clean.id > 0)) return;
  const key = keyOf(clean), same = C.open && key === C.key && document.getElementById("chat")?.firstChild;
  C.open = true;
  if (same) { host(); layout(); render(); return $("#chatIn")?.focus(); }
  stop();
  Object.assign(C, { ctx: clean, key, meta: null, thread: recall(key), data: null, pending: null, err: null });
  C.openCards.clear();
  host(); layout(); render(); fab();
  const q = clean.kind === "item" ? `kind=item&id=${clean.id}` : `kind=digest${clean.day ? `&day=${encodeURIComponent(clean.day)}` : ""}`;
  const [m] = await Promise.all([api("GET", `/api/chat/meta?${q}`), C.thread ? load() : null]);
  if (C.key !== key) return;
  if (m.ok) C.meta = m.data; else C.err = m.error;
  if (C.meta?.context?.day && clean.kind === "digest") C.ctx.day = C.meta.context.day;
  render();
  if (C.data?.status === "running") poll();
  $("#chatIn")?.focus();
}
function closeChat() {
  C.open = false; stop();
  const el = document.getElementById("chat");
  if (el) { if (el.classList.contains("chat-float")) el.remove(); else el.innerHTML = ""; }
  layout(); fab();
}
function stop() { clearTimeout(C.timer); clearInterval(C.tick); C.timer = C.tick = null; }

/* ---- talking to the server ---- */
async function load() {
  if (!C.thread) return;
  const key = C.key, r = await api("GET", `/api/chat/${C.thread}`);
  if (key !== C.key) return;
  if (r.ok) { C.data = r.data; C.pending = null; }
  else if (r.status === 404) { forget(key); C.thread = null; C.data = null; }
}
function poll() {
  clearTimeout(C.timer);
  if (!C.tick) C.tick = setInterval(elapsed, 1000);
  C.timer = setTimeout(async () => {
    if (!C.open) return stop();
    await load();
    renderMsgs();
    if (C.data?.status === "running") poll(); else { stop(); renderBox(); }
  }, 1200);
}
async function ask(question) {
  const q = String(question || "").trim();
  if (!q || C.data?.status === "running" || C.pending) return;
  C.pending = { q, at: Date.now() };
  renderMsgs(); renderBox();
  const key = C.key;
  const body = { question: q, context: { kind: C.ctx.kind, day: C.ctx.day, id: C.ctx.id } };
  if (C.thread) body.thread = C.thread;
  let r = await api("POST", "/api/chat", body);
  if (!r.ok && r.status === 404 && body.thread) { delete body.thread; forget(key); r = await api("POST", "/api/chat", body); }
  if (key !== C.key) return;
  if (!r.ok) { C.pending = null; renderMsgs(); renderBox(); return failToast(r, "Asking Gemma"); }
  C.thread = r.data.thread; remember(key, C.thread);
  C.pending.turn = r.data.turn;
  C.data = { ...(C.data || {}), status: "running", progress: { stage: "thinking" } };
  renderMsgs(); renderBox(); poll();
}

/* ---- drawing ---- */
const SUGGEST = {
  digest: "What's the main thread running through tonight's stories?",
  item: "What actually changed here, and is it worth my time?",
  video: "Give me the key claims in this video.",
};
function isVideo() { return C.ctx?.kind === "item" && /^Video\b/.test(C.meta?.context?.label || ""); }
function ctxLabel() {
  if (C.meta?.context?.label) return C.meta.context.label;
  return C.ctx.kind === "item" ? `Story · ${C.ctx.title || "#" + C.ctx.id}` : `Digest · ${C.ctx.day || "latest"}`;
}
function toolChips() {
  const tools = C.meta?.tools || [];
  return tools.map(t => t.enabled
    ? `<span class="tool" title="Gemma can use this">${esc(t.icon)} ${esc(t.label)}</span>`
    : `<a class="tool off" href="#/settings/chat" title="${esc(t.why || "Off")}">${esc(t.icon)} ${esc(t.label)}</a>`).join("");
}
function footer() {
  const m = C.meta?.model;
  if (!m) return "Gemma · tools run from this machine";
  return m.local
    ? `${esc(m.model)} on ${here()} · tools run from this machine · pages are fetched only when Gemma asks`
    : `${esc(m.model)} via ${esc(m.provider)} · your question, the context and tool results are sent there · pages are fetched from this machine`;
}
function render() {
  const el = document.getElementById("chat"); if (!el || !C.open) return;
  const item = C.ctx.kind === "item";
  el.innerHTML = `<div class="chat-in" role="complementary" aria-label="Ask Gemma">
    <div class="chat-h"><b>✦ Ask Gemma</b><span class="spacer"></span><button class="x" id="chatX" title="Close" aria-label="Close">✕</button></div>
    <div class="ctx"><span class="muted">About:</span> <span class="ctxchip" title="${esc(ctxLabel())}">${esc(ctxLabel())}</span>${item ? `<button class="lnk" id="ctxAll">whole Digest</button>` : ""}</div>
    <div class="toolrow" id="chatTools">${toolChips()}</div>
    <div class="msgs" id="chatMsgs"></div>
    <div class="chat-box" id="chatBox"></div>
    <p class="chat-foot muted" id="chatFoot">${footer()}</p>
  </div>`;
  $("#chatX").onclick = closeChat;
  const all = $("#ctxAll");
  if (all) all.onclick = () => openChat({ kind: "digest", day: NS.M?.day || "" });
  renderMsgs(); renderBox();
}
function renderBox() {
  const box = $("#chatBox"); if (!box) return;
  const busy = C.data?.status === "running" || !!C.pending;
  const prev = $("#chatIn")?.value || "";
  box.innerHTML = `<input type="text" id="chatIn" maxlength="2000" autocomplete="off" placeholder="Ask about ${C.ctx.kind === "item" ? (isVideo() ? "this video" : "this story") : "tonight's stories"}…" ${busy ? "disabled" : ""}>
    <button class="btn primary" id="chatGo" ${busy ? "disabled" : ""}>Ask</button>`;
  const input = $("#chatIn"); input.value = busy ? "" : prev;
  $("#chatGo").onclick = () => { const q = input.value.trim(); if (!q) return input.focus(); ask(q); };
  input.onkeydown = e => { if (e.key === "Enter" && !e.isComposing) $("#chatGo").click(); };
  if (!busy && C.open) input.focus({ preventScroll: true });
}
const secs = v => (typeof v === "number" ? v.toFixed(1) : "?") + "s";
/* a tool result as indented "key: value" lines, so page text reads as text */
function pretty(v, ind = "") {
  if (Array.isArray(v)) return v.map(x => x && typeof x === "object" ? `${ind}-\n${pretty(x, ind + "  ")}` : `${ind}- ${x}`).join("\n");
  if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => x && typeof x === "object" ? `${ind}${k}:\n${pretty(x, ind + "  ")}` : `${ind}${k}: ${x}`).join("\n");
  return ind + String(v);
}
function toolCard(m) {
  const t = m.tool || {};
  const arg = String(Object.values(t.args || {})[0] ?? "");
  let res = t.result || "";
  try { res = pretty(JSON.parse(res)); } catch (e) { /* clipped JSON: show as is */ }
  const open = C.openCards.has(m.id) ? " open" : "";
  return `<details class="tcall${t.ok ? "" : " bad"}" data-mid="${m.id}"${open}><summary><span class="ti">${esc(t.icon || "⚙")}</span><b>${esc(t.label || t.name)}</b>
    <span class="targ" title="${esc(arg)}">${esc(arg)}</span><span class="tsec">${t.ok ? "✓" : "✕"} ${secs(m.seconds)}</span></summary><pre>${esc(res)}</pre></details>`;
}
function hello() {
  const on = new Set((C.meta?.tools || []).filter(t => t.enabled).map(t => t.name));
  const what = C.ctx.kind === "item" ? (isVideo() ? "this video" : "this story") : "tonight's stories";
  const sugg = C.ctx.kind === "item" ? (isVideo() ? SUGGEST.video : SUGGEST.item) : SUGGEST.digest;
  const verbs = [["search_tonight", "search last night's items"], ["read_page", "read pages"], ["youtube_transcript", "pull YouTube transcripts"],
    ["hn_search", "search Hacker News"], ["web_search", "search the web"]].filter(([n]) => on.has(n)).map(([, v]) => v);
  const can = verbs.length ? `Gemma can ${verbs.length > 1 ? verbs.slice(0, -1).join(", ") + " and " + verbs.at(-1) : verbs[0]}.` : "";
  return `<div class="hello"><p>Ask anything about ${what}. ${can}</p>
    <p class="muted small">It reads pages only when it needs to, and tells you when something couldn't be checked.</p></div>
    <div class="sugg"><button id="chatSugg">${esc(sugg)}</button></div>`;
}
const STAGE = { thinking: "Gemma is thinking", reading: "Gemma is reading the results", writing: "Gemma is writing" };
function progressText() {
  const p = C.data?.progress || { stage: "thinking" };
  if (p.stage === "tool") {
    const t = (C.meta?.tools || []).find(x => x.name === p.tool);
    const arg = String(Object.values(p.args || {})[0] ?? "");
    return `Gemma is calling ${t ? t.label : p.tool}${arg ? ` · ${arg.slice(0, 80)}` : ""}…`;
  }
  return (STAGE[p.stage] || STAGE.thinking) + "…";
}
function elapsed() {
  const el = $("#chatWait"); if (!el) return;
  const msgs = C.data?.messages || [], last = [...msgs].reverse().find(m => m.role === "user");
  const from = C.pending?.at || (last ? Date.parse(last.created_at) : Date.now());
  $(".label", el).textContent = progressText();
  $(".secs", el).textContent = `${Math.max(0, Math.round((Date.now() - from) / 1000))}s`;
}
function renderMsgs() {
  const box = $("#chatMsgs"); if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const msgs = C.data?.messages || [];
  if (C.err) { box.innerHTML = `<div class="a err"><p>${esc(C.err)}</p></div>`; return; }
  let html = "";
  for (const m of msgs) {
    if (m.role === "user") html += `<div class="u">${esc(m.content)}</div>`;
    else if (m.role === "tool") html += toolCard(m);
    else if (m.role === "assistant") html += `<div class="a">${md(m.content)}<p class="asec">answered in ${secs(m.seconds)}${m.model ? ` · ${esc(m.model)}` : ""}</p></div>`;
    else if (m.role === "error") html += `<div class="a err"><p>${esc(m.content)}</p></div>`;
  }
  const seen = msgs.some(m => m.role === "user" && C.pending && m.turn === C.pending.turn);
  if (C.pending && !seen) html += `<div class="u">${esc(C.pending.q)}</div>`;
  if (C.data?.status === "error" && C.data.error && !msgs.some(m => m.role === "error")) html += `<div class="a err"><p>${esc(C.data.error)}</p></div>`;
  if (C.pending || C.data?.status === "running") html += `<div class="thinking" id="chatWait" aria-live="polite"><span class="label"></span> <span class="secs"></span></div>`;
  box.innerHTML = html || hello();
  $$("details.tcall", box).forEach(d => d.addEventListener("toggle", () => { if (d.open) C.openCards.add(+d.dataset.mid); else C.openCards.delete(+d.dataset.mid); }));
  const s = $("#chatSugg");
  if (s) s.onclick = () => ask(s.textContent);
  elapsed();
  if (nearBottom || C.pending) box.scrollTop = box.scrollHeight;
}

/* ---- the fallback ✦ Ask button: only on the Morning page of builds without the Digest page ---- */
function fabWanted() {
  return !C.open && page() === "morning" && !NS.pages.digest && !$(".frame.digest") && !!NS.B && !$("#tour");
}
function fab() {
  let b = $("#nsAskFab");
  if (!fabWanted()) { b?.remove(); return; }
  if (b) return;
  b = document.createElement("button");
  b.id = "nsAskFab"; b.className = "ask-fab"; b.textContent = "✦ Ask";
  b.title = "Ask Gemma about this pick or last night's items";
  b.onclick = () => {
    const id = +((location.hash || "").split("/")[2] || 0);
    const c = id ? NS.card(id) : null;
    openChat(c ? { kind: "item", id: c.id, title: c.title } : { kind: "digest", day: NS.M?.day || "" });
  };
  document.body.appendChild(b);
}

/* Track A's Digest page re-renders its #chat aside; keep the drawer in it while open. */
const watch = new MutationObserver(() => {
  const el = document.getElementById("chat");
  if (C.open && !C.float && !el) { C.open = false; stop(); layout(); }  // the page went away
  else if (C.open && el && !el.classList.contains("chat-float") && $$("#chat.chat-float").length === 0 && !el.firstChild) { layout(); render(); }
  else if (C.open && C.float && $(".frame.digest #chat")) { removeFloat(); C.float = false; layout(); render(); }
  fab();
});
window.addEventListener("DOMContentLoaded", () => {
  const app = document.getElementById("app");
  if (app) watch.observe(app, { childList: true, subtree: true });
});
window.addEventListener("hashchange", () => { if (C.open && C.float && page() !== "morning") closeChat(); setTimeout(fab, 0); });
window.addEventListener("resize", () => { if (C.open) layout(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && C.open && !$("#tour") && !$(".sheet-wrap")) closeChat(); });

Object.assign(NS, { openChat, closeChat, chatMarkdown: md });
})();
