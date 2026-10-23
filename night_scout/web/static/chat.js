/* Night Scout: the Ask Gemma chat drawer.
   NS.openChat(ctx) / NS.closeChat(). ctx = {kind: "digest", day: "YYYY-MM-DD"}, {kind: "item", id, title} or {kind: "app"}
   (Night Scout itself, the reader's setup, every night, anything else; the top-bar Ask opens it).
   Renders into the page's #chat aside when there is one (the Digest page: toggles `chat-open` on
   .frame.digest), else into a floating aside of its own. `chat-full` on body below 760 px.
   Strict CSP: no inline styles or handlers; everything from the model is escaped first. */
(() => {
"use strict";
const { $, $$, esc, api, failToast } = NS;
const PHONE = 760;
const C = { open: false, ctx: null, key: "", meta: null, thread: null, data: null, timer: null, tick: null,
  pending: null, openCards: new Set(), float: false, err: null, fails: 0, att: [], dec: new Map(), gen: 0 };

const keyOf = ctx => ctx.kind === "app" ? "app" : ctx.kind === "item" ? `item:${ctx.id}` : `digest:${ctx.day || ""}`;
const remember = (key, id) => { try { sessionStorage.setItem("nsChat:" + key, String(id)); } catch (e) { /* private mode */ } };
const recall = key => { try { return +sessionStorage.getItem("nsChat:" + key) || null; } catch (e) { return null; } };
const forget = key => { try { sessionStorage.removeItem("nsChat:" + key); } catch (e) { /* private mode */ } };
const page = () => (location.hash || "#/morning").split("/")[1] || "morning";
const here = () => (C.meta?.platform || NS.B?.platform) === "darwin" ? "this Mac" : "this machine";

/* ---- tiny safe markdown: links (http/https, or in-app #/page), bold, code, bullets, paragraphs; the rest is escaped ---- */
function inline(text) {
  return String(text).split(/(`[^`\n]+`)/g).map(part => {
    if (/^`[^`\n]+`$/.test(part)) return `<code>${esc(part.slice(1, -1))}</code>`;
    return esc(part)
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)
      .replace(/\[([^\]\n]+)\]\((#\/[a-z]+(?:\/[a-z0-9-]+)?)\)/g, (m, label, h) => `<a class="inapp" href="${h}">${label} ›</a>`)
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
  document.body.classList.toggle("chat-on", C.open);
  $(".frame.digest")?.classList.toggle("chat-open", C.open && !C.float);
}

/* ---- open / close ---- */
async function openChat(ctx) {
  if (!ctx || !["digest", "item", "app"].includes(ctx.kind)) return;
  const clean = ctx.kind === "app" ? { kind: "app" } : ctx.kind === "item" ? { kind: "item", id: +ctx.id, title: ctx.title || "" } : { kind: "digest", day: ctx.day || NS.M?.day || "" };
  if (clean.kind === "item" && !(clean.id > 0)) return;
  const key = keyOf(clean), same = C.open && key === C.key && document.getElementById("chat")?.firstChild;
  C.open = true;
  if (same) { host(); layout(); render(); return $("#chatIn")?.focus(); }
  stop();
  Object.assign(C, { ctx: clean, key, meta: null, thread: recall(key), data: null, pending: null, err: null, fails: 0 });
  C.openCards.clear(); C.dec.clear();
  host(); layout(); render(); fab();
  const q = clean.kind === "app" ? "kind=app" : clean.kind === "item" ? `kind=item&id=${clean.id}` : `kind=digest${clean.day ? `&day=${encodeURIComponent(clean.day)}` : ""}`;
  const [m] = await Promise.all([api("GET", `/api/chat/meta?${q}`), C.thread ? load() : null]);
  if (C.key !== key) return;
  if (m.ok) C.meta = m.data; else C.err = m.error;
  if (C.meta?.context?.day && clean.kind === "digest" && !clean.day) {
    // "the latest Digest" turned out to be a real day: use that day's conversation
    C.ctx.day = C.meta.context.day; C.key = keyOf(C.ctx);
    const dayKey = C.key;
    if (!C.thread) { C.thread = recall(dayKey); if (C.thread) await load(); }
    if (C.key !== dayKey) return;
  }
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
function newChat() {
  forget(C.key); stop(); C.gen++;
  Object.assign(C, { thread: null, data: null, pending: null, err: null, fails: 0, att: [] });
  C.openCards.clear(); C.dec.clear();
  render();
}

/* ---- talking to the server ---- */
async function load() {
  if (!C.thread) return;
  const key = C.key, thread = C.thread, r = await api("GET", `/api/chat/${thread}`);
  if (key !== C.key || thread !== C.thread) return;
  if (r.ok) { C.data = r.data; C.pending = null; C.fails = 0; return; }
  if (r.status === 404) { forget(key); C.thread = null; C.data = null; return; }
  // signed out, or the server keeps failing: stop polling and say why
  if (r.status === 401 || ++C.fails >= 5) C.data = { ...(C.data || {}), status: "error", error: r.error, progress: null };
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
  const att = C.att, q = String(question || "").trim() || (att.length ? "What do you make of this?" : "");
  if (!q || C.data?.status === "running" || C.pending) return;
  C.pending = { q, at: Date.now(), att }; C.att = [];
  renderMsgs(); renderBox();
  const key = C.key, gen = C.gen;
  const body = { question: q, context: { kind: C.ctx.kind, day: C.ctx.day, id: C.ctx.id } };
  if (att.length) body.attachments = att.map(({ name, kind, data }) => ({ name, kind, data }));
  if (C.thread) body.thread = C.thread;
  let r = await api("POST", "/api/chat", body);
  if (!r.ok && r.status === 404 && body.thread) { delete body.thread; forget(key); r = await api("POST", "/api/chat", body); }
  if (r.ok && gen === C.gen) remember(key, r.data.thread);  // even if the drawer moved on meanwhile (not after New chat)
  if (key !== C.key || gen !== C.gen) return;
  if (!r.ok) { C.pending = null; C.att = att; renderMsgs(); renderBox(); return failToast(r, "Asking Gemma"); }
  C.thread = r.data.thread;
  C.pending.turn = r.data.turn;
  C.data = { ...(C.data || {}), status: "running", progress: { stage: "thinking" } };
  renderMsgs(); renderBox(); poll();
}

/* ---- drawing ---- */
const SUGGEST = {
  digest: "What's the main thread running through tonight's stories?",
  item: "What actually changed here, and is it worth my time?",
  video: "Give me the key claims in this video.",
  app: ["How do I follow a YouTube channel?", "Why was my last Digest short?", "Has anything about local LLMs come up this week?", "Add a topic for me: Robotics"],
};
function isVideo() { return C.ctx?.kind === "item" && /^Video\b/.test(C.meta?.context?.label || ""); }
function ctxLabel() {
  if (C.meta?.context?.label) return C.meta.context.label;
  return C.ctx.kind === "app" ? "Night Scout" : C.ctx.kind === "item" ? `Story · ${C.ctx.title || "#" + C.ctx.id}` : `Digest · ${C.ctx.day || "latest"}`;
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
  // Keep what's been typed: the drawer is rebuilt below (e.g. when /api/chat/meta answers).
  const draft = document.getElementById("chatIn")?.value;
  el.innerHTML = `<div class="chat-in" role="complementary" aria-label="Ask Gemma">
    <div class="chat-h"><b class="ch-title">${NS.scoutImg(20)} Ask Gemma</b><span class="spacer"></span><button class="newchat" id="chatNew" title="Start a new conversation" ${hasTalk() ? "" : "hidden"}>＋ New chat</button><button class="x" id="chatX" title="Close" aria-label="Close">✕</button></div>
    <div class="ctx"><span class="muted">About:</span> <span class="ctxchip" title="${esc(ctxLabel())}">${esc(ctxLabel())}</span>${item ? `<button class="lnk" id="ctxAll">whole Digest</button>` : ""}${C.ctx.kind !== "app" ? `<button class="lnk" id="ctxApp" title="Night Scout, your setup, every night, or anything else">anything else</button>` : ""}</div>
    <div class="toolrow" id="chatTools">${toolChips()}</div>
    <div class="msgs" id="chatMsgs"></div>
    <div class="chat-box" id="chatBox"></div>
    <p class="chat-foot muted" id="chatFoot">${footer()}</p>
  </div>`;
  $("#chatX").onclick = closeChat;
  $("#chatNew").onclick = () => { newChat(); $("#chatIn")?.focus(); };
  const all = $("#ctxAll"), ap = $("#ctxApp");
  if (all) all.onclick = () => openChat({ kind: "digest", day: NS.M?.day || "" });
  if (ap) ap.onclick = () => openChat({ kind: "app" });
  const drawer = $(".chat-in", el), files = e => [...(e.dataTransfer?.types || [])].includes("Files");
  drawer.ondragover = e => { if (!files(e)) return; e.preventDefault(); drawer.classList.add("drop"); };
  drawer.ondragleave = e => { if (!drawer.contains(e.relatedTarget)) drawer.classList.remove("drop"); };
  drawer.ondrop = e => { if (!files(e)) return; e.preventDefault(); drawer.classList.remove("drop"); addFiles([...e.dataTransfer.files]); };
  renderMsgs(); renderBox(draft);
}
function renderBox(draft) {
  const box = $("#chatBox"); if (!box) return;
  const busy = C.data?.status === "running" || !!C.pending;
  const prev = draft ?? ($("#chatIn")?.value || "");
  const what = C.ctx.kind === "app" ? "Ask anything" : `Ask about ${C.ctx.kind === "item" ? (isVideo() ? "this video" : "this story") : "tonight's stories"}`;
  box.innerHTML = `${C.att.length ? `<div class="attrow">${C.att.map((a, i) => attChip(a, i, true)).join("")}</div>` : ""}
    <button class="attbtn" id="chatAtt" title="Attach images, text files or PDFs (or paste a screenshot)" aria-label="Attach files" ${busy ? "disabled" : ""}>📎</button>
    <input type="file" id="chatFile" multiple hidden accept="image/*,.pdf,.txt,.md,.json,.csv,.tsv,.yaml,.yml,.toml,.ini,.log,.xml,.html,.css,.js,.ts,.py,.sh,.sql">
    <input type="text" id="chatIn" maxlength="2000" autocomplete="off" placeholder="${what}…" ${busy ? "disabled" : ""}>
    <button class="btn primary" id="chatGo" ${busy ? "disabled" : ""}>Ask</button>`;
  const input = $("#chatIn"); input.value = busy ? "" : prev;
  $("#chatAtt").onclick = () => $("#chatFile").click();
  $("#chatFile").onchange = e => addFiles([...e.target.files]);
  input.onpaste = e => { const f = [...(e.clipboardData?.files || [])]; if (f.length) { e.preventDefault(); addFiles(f); } };
  $$("[data-rmatt]", box).forEach(b => b.onclick = () => { C.att.splice(+b.dataset.rmatt, 1); renderBox(); });
  $$("[data-big]", box).forEach(b => b.onclick = () => lightbox(C.att[+b.dataset.big]));
  $("#chatGo").onclick = () => { const q = input.value.trim(); if (!q && !C.att.length) return input.focus(); ask(q); };
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
  if (C.ctx.kind === "app") return `<div class="hello">${NS.scoutImg(56, "scout-hero")}<p>Ask about Night Scout, your setup and runs, anything it has fetched on any night, or anything else.</p>
    <p class="muted small">Gemma looks things up in the Guide, your setup and past nights before it answers. It can suggest sources and topics; you confirm before anything is added. Paste, attach or drop images, text files and PDFs.</p></div>
    <div class="sugg">${SUGGEST.app.map(q => `<button data-sg>${esc(q)}</button>`).join("")}</div>`;
  const on = new Set((C.meta?.tools || []).filter(t => t.enabled).map(t => t.name));
  const what = C.ctx.kind === "item" ? (isVideo() ? "this video" : "this story") : "tonight's stories";
  const sugg = C.ctx.kind === "item" ? (isVideo() ? SUGGEST.video : SUGGEST.item) : SUGGEST.digest;
  const verbs = [["search_tonight", "search last night's items"], ["read_page", "read pages"], ["youtube_transcript", "pull YouTube transcripts"],
    ["hn_search", "search Hacker News"], ["web_search", "search the web"]].filter(([n]) => on.has(n)).map(([, v]) => v);
  const can = verbs.length ? `Gemma can ${verbs.length > 1 ? verbs.slice(0, -1).join(", ") + " and " + verbs.at(-1) : verbs[0]}.` : "";
  return `<div class="hello"><p>Ask anything about ${what}. ${can}</p>
    <p class="muted small">It reads pages only when it needs to, and tells you when something couldn't be checked.</p></div>
    <div class="sugg"><button data-sg>${esc(sugg)}</button></div>`;
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
  let html = C.err ? `<div class="a err"><p>${esc(C.err)}</p></div>` : "", cards = "";  // a turn's proposal cards go under its answer
  for (const m of msgs) {
    if (m.role === "user") html += cards + userMsg(m.content, m.attachments, m.id);
    else if (m.role === "tool" && proposal(m)) { cards += proposeCard(m); continue; }
    else if (m.role === "tool") html += toolCard(m);
    else if (m.role === "assistant") html += `<div class="a">${md(m.content)}<p class="asec">answered in ${secs(m.seconds)}${m.model ? ` · ${esc(m.model)}` : ""}</p></div>` + cards;
    else if (m.role === "error") html += `<div class="a err"><p>${esc(m.content)}</p></div>` + cards;
    else continue;
    cards = "";
  }
  html += cards;
  const seen = msgs.some(m => m.role === "user" && C.pending && m.turn === C.pending.turn);
  if (C.pending && !seen) html += userMsg(C.pending.q, C.pending.att, "p");
  const lastTurn = msgs.length ? msgs[msgs.length - 1].turn : 0;
  const shown = msgs.some(m => m.role === "error" && m.turn === lastTurn);
  if (C.data?.status === "error" && C.data.error && !shown) html += `<div class="a err"><p>${esc(C.data.error)}</p></div>`;
  if (C.pending || C.data?.status === "running") html += `<div class="thinking" id="chatWait" aria-live="polite"><span class="label"></span> <span class="secs"></span></div>`;
  box.innerHTML = msgs.length || C.pending || C.data?.status === "running" ? html : html + hello();
  const nc = $("#chatNew"); if (nc) nc.hidden = !hasTalk();
  $$("details.tcall", box).forEach(d => d.addEventListener("toggle", () => { if (d.open) C.openCards.add(+d.dataset.mid); else C.openCards.delete(+d.dataset.mid); }));
  $$("[data-sg]", box).forEach(b => b.onclick = () => ask(b.textContent));
  $$(".uatt [data-big]", box).forEach(b => b.onclick = () => {
    const [mid, i] = b.dataset.big.split(":");
    lightbox(mid === "p" ? C.pending?.att?.[+i] : msgs.find(x => String(x.id) === mid)?.attachments?.[+i]);
  });
  $$(".propose [data-do]", box).forEach(b => b.onclick = () => decide(+b.dataset.mid, b.dataset.do));
  elapsed();
  if (nearBottom || C.pending) box.scrollTop = box.scrollHeight;
}

const hasTalk = () => !!(C.thread || C.pending || C.data?.messages?.length);
function userMsg(text, att, mid) {
  const list = Array.isArray(att) ? att : [];
  return `${list.length ? `<div class="uatt">${list.map((a, i) => attChip(a, `${mid}:${i}`, false)).join("")}</div>` : ""}<div class="u">${esc(text)}</div>`;
}

/* ---- attachments: paste, 📎 or drop. Images are shrunk here to ≤1568 px JPEG; text ≤200 KB; PDF ≤10 MB (its text is read by the server) ---- */
const IMG_MAX = 1568, TEXT_MAX = 200000, PDF_MAX = 10e6, MAX_ATT = 4;
const readAs = (f, how) => new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = bad; r[how](f); });
async function shrink(file) {
  const img = new Image(); img.src = await readAs(file, "readAsDataURL"); await img.decode();  // a data: URL (the CSP's img-src has no blob:)
  const k = Math.min(1, IMG_MAX / Math.max(img.naturalWidth, img.naturalHeight));
  const c = document.createElement("canvas"); c.width = Math.max(1, Math.round(img.naturalWidth * k)); c.height = Math.max(1, Math.round(img.naturalHeight * k));
  const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);  // JPEG has no transparency
  g.drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.86);
}
const dataSize = d => Math.round((d.length - d.indexOf(",") - 1) * 3 / 4);
async function addFiles(files) {
  for (const f of files) {
    if (C.att.length >= MAX_ATT) { NS.toast(`Up to ${MAX_ATT} attachments per question.`); break; }
    const name = f.name || (f.type.startsWith("image/") ? "screenshot.png" : "file.txt");
    try {
      if (f.type === "application/pdf" || /\.pdf$/i.test(name)) {
        if (f.size > PDF_MAX) { NS.toast(`<b>${esc(name)}</b> is over 10 MB.`); continue; }
        const data = (await readAs(f, "readAsDataURL")).replace(/^data:[^,]*,/, "data:application/pdf;base64,");
        C.att.push({ name, kind: "pdf", size: f.size, data });
      } else if (f.type.startsWith("image/")) {
        if (f.size > 40e6) { NS.toast(`<b>${esc(name)}</b> is too big to attach.`); continue; }
        const data = await shrink(f);
        C.att.push({ name, kind: "image", size: dataSize(data), data });
      } else {
        if (f.size > TEXT_MAX) { NS.toast(`<b>${esc(name)}</b> is over 200 KB. Attach a smaller text file.`); continue; }
        const text = await readAs(f, "readAsText");
        if (/[\u0000-\u0008]/.test(text.slice(0, 4000))) { NS.toast(`<b>${esc(name)}</b> isn't a text file. Attach images, text files or PDFs.`); continue; }
        C.att.push({ name, kind: "text", size: f.size, data: text });
      }
    } catch (e) { NS.toast(`Couldn't read <b>${esc(name)}</b>.`); }
  }
  renderBox();
}
/* a chip: local (before sending, has .data) or from the server (has .url / .text_url) */
const sameOrigin = u => typeof u === "string" && /^\/api\//.test(u);
function attChip(a, i, removable) {
  const size = a.size ? ` · ${a.size > 1e6 ? (a.size / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(a.size / 1e3)) + " KB"}` : "";
  const what = a.kind === "image" ? "image" : a.kind === "pdf" ? `PDF${a.pages ? ` · ${a.pages} page${a.pages === 1 ? "" : "s"}` : ""}` : "text";
  const src = a.data && a.kind === "image" ? a.data : sameOrigin(a.url) ? a.url : "";
  const thumb = a.kind === "image" && src ? `<img src="${esc(src)}" alt="">` : `<span class="ftype">${esc((String(a.name || "").split(".").pop() || "txt").slice(0, 4).toUpperCase())}</span>`;
  return `<span class="att ${esc(a.kind)}"><button class="attview" data-big="${i}" title="Open ${esc(a.name)}">${thumb}<span class="attname">${esc(a.name)}<small>${what}${size}</small></span></button>${removable ? `<button class="attx" data-rmatt="${i}" title="Remove" aria-label="Remove ${esc(a.name)}">✕</button>` : ""}</span>`;
}
function lightbox(a) {
  if (!a) return;
  $(".lightbox")?.remove();
  const w = document.createElement("div"); w.className = "lightbox"; w.setAttribute("role", "dialog"); w.setAttribute("aria-label", a.name || "Attachment");
  const img = a.data && a.kind === "image" ? a.data : a.kind === "image" && sameOrigin(a.url) ? a.url : "";
  const open = a.kind === "pdf" && sameOrigin(a.url) ? ` <a class="btn ghost sm" href="${esc(a.url)}" target="_blank" rel="noopener">Open the PDF ↗</a>` : "";
  const body = img ? `<img src="${esc(img)}" alt="${esc(a.name)}">`
    : a.data && a.kind === "text" ? `<pre>${esc(a.data)}</pre>`
    : sameOrigin(a.text_url) ? `<pre class="muted">Loading…</pre>`
    : `<p class="muted">PDF · Night Scout reads its text on this machine when you send it, and gives that text to Gemma.</p>`;
  w.innerHTML = `<div class="lb-in">${body}<p><span>${esc(a.name)}${open}</span><button class="btn ghost sm" id="lbX">Close</button></p></div>`;
  w.onclick = e => { if (e.target === w || e.target.id === "lbX") w.remove(); };
  document.body.appendChild(w);
  $("#lbX", w).focus();
  if (!img && !a.data && sameOrigin(a.text_url)) {
    fetch(a.text_url, { credentials: "same-origin" }).then(r => r.ok ? r.text() : Promise.reject(r.status))
      .then(t => { const pre = $("pre", w); if (pre) { pre.textContent = t; pre.classList.remove("muted"); } })
      .catch(() => { const pre = $("pre", w); if (pre) pre.textContent = "Couldn't load the text of this file."; });
  }
}

/* ---- proposals: Gemma suggests a source or topic; only the reader's click adds it (the server reads the proposal from its own record) ---- */
const TYPE_NAME = { website: "Website", rss: "Feed", youtube: "YouTube channel", newsletter: "Newsletter", podcast: "Podcast", reddit: "Subreddit", hackernews: "Hacker News search",
  bluesky: "Bluesky", mastodon: "Mastodon hashtag", github: "GitHub", arxiv: "arXiv category" };
function proposal(m) {
  if (!/^propose_/.test(m.tool?.name || "")) return null;
  try { const p = JSON.parse(m.tool.result || "{}").proposal; return p && (p.kind === "source" || p.kind === "topic") ? p : null; } catch (e) { return null; }
}
function proposeCard(m) {
  const p = proposal(m), src = p.kind === "source", d = m.decision, local = C.dec.get(m.id) || {};
  const done = d && (d.state === "added" || d.state === "skipped");
  const label = src ? (p.label || p.value) : p.topic;
  const note = done ? (d.state === "added" ? `<p class="pr-ok">✓ ${esc(d.note || "Added.")}</p>` : `<p class="muted small">${esc(d.note || "Not added.")}</p>`)
    : local.error || d?.state === "error" ? `<p class="pr-err">${esc(local.error || d.note || "Couldn't add it.")}</p>` : "";
  const btns = done ? "" : `<div class="row"><button class="btn primary sm" data-do="add" data-mid="${m.id}" ${local.busy ? "disabled" : ""}>${local.busy === "add" ? "Adding…" : src ? "Add source" : "Add topic"}</button>
    <button class="btn ghost sm" data-do="skip" data-mid="${m.id}" ${local.busy ? "disabled" : ""}>Not now</button></div>`;
  return `<div class="propose ${done ? esc(d.state) : ""}"><p class="pr-h">${src ? `➕ Add source · ${esc(TYPE_NAME[p.type] || p.type || "")}` : "🏷 Add topic"}</p>
    <p class="pr-v">${esc(label)}</p>${src && p.label && p.value && p.value !== p.label ? `<p class="pr-val">${esc(p.value)}</p>` : ""}${p.why ? `<p class="pr-why">${esc(p.why)}</p>` : ""}${note}${btns}</div>`;
}
async function decide(mid, action) {
  const thread = C.thread, m = (C.data?.messages || []).find(x => x.id === mid);
  if (!thread || !m || C.dec.get(mid)?.busy) return;
  C.dec.set(mid, { busy: action }); renderMsgs();
  const r = await api("POST", `/api/chat/${thread}/proposal/${mid}`, { action });
  if (thread !== C.thread) return;
  if (r.ok && r.data?.decision) { C.dec.delete(mid); m.decision = r.data.decision; if (r.data.decision.state === "added") NS.loadBoot?.(); }
  else if (r.status === 409) { C.dec.delete(mid); await load(); }  // decided already (another tab): show what the server has
  else C.dec.set(mid, { error: r.error || "Couldn't do that." });
  renderMsgs();
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

/* Track A's Digest page re-renders its #chat aside; keep the drawer in it while open.
   The general chat outlives the page (it floats on the next one); a Digest or story chat closes with it. */
const keepsOpen = () => C.ctx?.kind === "app" && innerWidth > PHONE;
const watch = new MutationObserver(() => {
  const el = document.getElementById("chat");
  if (C.open && !C.float && !el) { if (keepsOpen()) { host(); layout(); render(); } else { C.open = false; stop(); layout(); } }  // the page went away
  else if (C.open && el && !el.classList.contains("chat-float") && $$("#chat.chat-float").length === 0 && !el.firstChild) { layout(); render(); }
  else if (C.open && C.float && $(".frame.digest #chat")) { removeFloat(); C.float = false; layout(); render(); }
  fab();
});
window.addEventListener("DOMContentLoaded", () => {
  const app = document.getElementById("app");
  if (app) watch.observe(app, { childList: true, subtree: true });
  const nav = document.getElementById("askNav");
  if (nav) nav.onclick = () => C.open && C.ctx?.kind === "app" ? closeChat() : openChat({ kind: "app" });
});
window.addEventListener("hashchange", () => { if (C.open && C.float && page() !== "morning" && !keepsOpen()) closeChat(); setTimeout(fab, 0); });
window.addEventListener("resize", () => { if (C.open) layout(); });
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const lb = $(".lightbox");
  if (lb) { lb.remove(); return $("#chatIn")?.focus({ preventScroll: true }); }  // Esc closes only the lightbox
  if (C.open && !$("#tour") && !$(".sheet-wrap")) closeChat();
});

Object.assign(NS, { openChat, closeChat, chatMarkdown: md, chatCtx: () => (C.open ? { ...C.ctx } : null) });
})();
