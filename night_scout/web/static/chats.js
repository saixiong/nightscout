/* Night Scout: the Chats page (#/chats, #/chats/<id>): every saved Ask Gemma conversation, searchable.
   Reached from the drawer's History ▾ → All chats & search (no tab of its own: the phone bar is full).
   The list is on the left, the open conversation in the big card on the right; on a phone the list
   alone, and a tap continues that conversation in the full-screen chat.
   Strict CSP: no inline styles or handlers; everything from the model is escaped (NS.chatMarkdown). */
(() => {
"use strict";
const { $, $$, esc, api, chrome } = NS;
const PHONE = 760;
const KINDS = [["all", "All"], ["app", "Night Scout"], ["digest", "Digest"], ["item", "Stories"], ["files", "With files"]];
const S = { q: "", kind: "all", sel: null, data: null, full: new Map(), armed: null, armT: null, req: 0, qT: null };
const phone = () => innerWidth <= PHONE;
const here = () => location.hash.split("/")[1] === "chats";

NS.pages.chats = async (a) => {
  chrome("chats");
  if (a && +a > 0) S.sel = +a;
  if (!$(".frame.chats")) shell();
  await fetchList();
};

function shell() {
  $("#app").innerHTML = `<div class="frame chats"><div class="col-left">
    <section class="hero"><p class="eyebrow">Ask Gemma</p><h1>Your <em>conversations.</em></h1>
      <p class="sub" id="chSub">Saved on this ${NS.B?.platform === "darwin" ? "Mac" : "machine"}. Search looks through every question and answer.</p></section>
    <div class="csearch"><span aria-hidden="true">⌕</span><input id="chQ" type="search" placeholder="Search questions and answers" autocomplete="off" aria-label="Search your chats"></div>
    <div class="seg cseg" id="chKinds">${KINDS.map(([k, l]) => `<button data-k="${k}">${l}</button>`).join("")}</div>
    <p class="muted small cres" id="chRes"></p>
    <div class="chlist" id="chList"><p class="loading">Loading your chats…</p></div>
  </div><div class="stage"><div class="big enter"><div class="big-scroll" id="chPane"></div></div></div></div>`;
  const q = $("#chQ");
  q.value = S.q;
  q.oninput = () => { S.q = q.value; clearTimeout(S.qT); S.qT = setTimeout(fetchList, 200); };
  $$("#chKinds button").forEach(b => b.onclick = () => { S.kind = b.dataset.k; fetchList(); });
}

async function fetchList() {
  const n = ++S.req, r = await api("GET", `/api/chats?q=${encodeURIComponent(S.q)}&kind=${S.kind}`);
  if (n !== S.req || !here()) return;
  if (!r.ok) { $("#chList").innerHTML = `<p class="muted">${esc(r.error || "Couldn't load your chats.")}</p>`; return; }
  S.data = r.data;
  const ids = S.data.threads.map(t => t.id);
  if (!ids.includes(S.sel)) S.sel = ids[0] ?? null;
  renderList();
  renderPane();
}

function renderList() {
  const D = S.data, list = $("#chList");
  $$("#chKinds button").forEach(b => b.classList.toggle("on", b.dataset.k === S.kind));
  $("#chSub").textContent = `${D.total} conversation${D.total === 1 ? "" : "s"}, saved on this ${NS.B?.platform === "darwin" ? "Mac" : "machine"}. Search looks through every question and answer.`;
  $("#chRes").textContent = D.q ? `${D.threads.length} of ${D.total} match “${D.q}”` : "";
  if (!D.threads.length) {
    list.innerHTML = D.total ? `<p class="muted">No conversations match. Try another word or filter.</p>`
      : `<p class="muted">No saved chats yet. Open <b>Ask</b> and ask Gemma something; every conversation is kept here.</p>`;
    return;
  }
  list.innerHTML = NS.chatGrouped(D.threads).map(([g, ts]) => `<div class="hgrp">${g}</div>` + ts.map(t => `
    <div class="crow ${t.id === S.sel && !phone() ? "sel" : ""}" data-id="${t.id}" tabindex="0">
      <div class="cmeta">${NS.chatKindIcon(t)}<span class="clabel">${esc(t.label)}</span><span class="ctime">${NS.chatWhen(t.updated_at).text}</span></div>
      <h3>${esc(t.title)}</h3><p class="csnip">${mark(esc(t.snippet))}</p>
      <div class="cfoot">${[`${t.questions} question${t.questions === 1 ? "" : "s"}`, t.tools ? `${t.tools} tool call${t.tools === 1 ? "" : "s"}` : "", ...t.files.map(f => `📎 ${esc(f)}`)].filter(Boolean).join(" · ")}</div>
    </div>`).join("")).join("");
  $$(".crow", list).forEach(row => {
    const pick = () => {
      const t = S.data.threads.find(x => x.id === +row.dataset.id);
      if (phone()) return NS.openThread(t.id, NS.chatThreadCtx(t));
      S.sel = t.id; S.armed = null;
      history.replaceState(null, "", `#/chats/${t.id}`);
      renderList(); renderPane();
    };
    row.onclick = pick;
    row.onkeydown = e => { if (e.key === "Enter") pick(); };
  });
}

/* the search words, highlighted in already-escaped text */
function mark(html) {
  const q = (S.data?.q || "").trim();
  if (!q) return html;
  const needle = esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return html.split(/(<[^>]*>)/g).map(part => part.startsWith("<") ? part : part.replace(new RegExp(needle, "gi"), m => `<mark>${m}</mark>`)).join("");
}

async function renderPane() {
  const pane = $("#chPane"); if (!pane || phone()) return;
  const t = S.data?.threads.find(x => x.id === S.sel);
  if (!t) { pane.innerHTML = `<h2 class="serif mt0">Nothing to show.</h2><p class="summary">Pick a conversation on the left.</p>`; return; }
  const cacheKey = `${t.id}:${t.updated_at}`;
  let full = S.full.get(cacheKey);
  if (!full) {
    pane.innerHTML = `<p class="loading">Opening the conversation…</p>`;
    const r = await api("GET", `/api/chat/${t.id}`);
    if (S.sel !== t.id || !here()) return;
    if (!r.ok) { pane.innerHTML = `<p class="muted">${esc(r.error || "Couldn't open it.")}</p>`; return; }
    full = r.data; S.full.set(cacheKey, full);
  }
  const msgs = full.messages || [], turns = [...new Set(msgs.map(m => m.turn))];
  const started = new Date(t.created_at).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const model = msgs.find(m => m.role === "assistant" && m.model)?.model;
  pane.innerHTML = `<div class="cphead"><span class="chip">${NS.chatKindIcon(t)}<span class="cplabel">${esc(t.label)}</span></span>
      ${t.url ? `<a class="clink" href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">Open the source ↗</a>` : ""}</div>
    <h2 class="serif mt0 cptitle">${esc(t.title)}</h2>
    <p class="muted small">Started ${esc(started)} · ${t.questions} question${t.questions === 1 ? "" : "s"}${model ? ` · ${esc(model)}` : ""}</p>
    <div class="cpact"><button class="btn primary" id="chCont">Continue in chat</button><button class="btn ghost" id="chDel">${S.armed === t.id ? "Delete? Click again" : "Delete"}</button></div>
    <div class="ctrans" id="chTrans">${turns.map(n => {
      const q = msgs.find(m => m.turn === n && m.role === "user");
      const tools = msgs.filter(m => m.turn === n && m.role === "tool");
      const a = msgs.find(m => m.turn === n && m.role === "assistant");
      const err = msgs.find(m => m.turn === n && m.role === "error");
      return `<div class="cq-b">${esc(q?.content || "")}${(q?.attachments || []).map(f => `<a class="cfile" href="${esc(f.url)}" target="_blank" rel="noopener">📎 ${esc(f.name || "attachment")}</a>`).join("")}</div>
        ${tools.length ? `<div class="ctools">${tools.map(m => `<span class="chip">${esc(m.tool?.icon || "⚙")} ${esc(m.tool?.label || m.tool?.name || "tool")}</span>`).join("")}</div>` : ""}
        ${a ? `<div class="ca-b">${NS.chatMarkdown(a.content)}<div class="cafoot">${a.seconds != null ? `answered in ${Number(a.seconds).toFixed(1)} s` : "answered"}${a.model ? ` · ${esc(a.model)}` : ""}</div></div>`
          : err ? `<p class="muted small">This question didn't get an answer.</p>`
          : full.status === "running" ? `<p class="muted small">Gemma is still answering…</p>` : ""}`;
    }).join("")}</div>`;
  if (S.data.q) highlight($("#chTrans"), S.data.q);
  $("#chCont").onclick = () => NS.openThread(t.id, NS.chatThreadCtx(t));
  $("#chDel").onclick = () => {
    if (S.armed !== t.id) {
      S.armed = t.id; clearTimeout(S.armT);
      S.armT = setTimeout(() => { S.armed = null; if (here()) renderPane(); }, 4000);
      return renderPane();
    }
    S.armed = null; clearTimeout(S.armT);
    NS.deleteThread(t.id, () => { if (here()) fetchList(); });
  };
}

/* highlight the search words inside the rendered transcript (text nodes only) */
function highlight(root, q) {
  const needle = q.toLowerCase(), walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), hits = [];
  while (walk.nextNode()) if (walk.currentNode.nodeValue.toLowerCase().includes(needle)) hits.push(walk.currentNode);
  for (const node of hits) {
    const frag = document.createDocumentFragment();
    let rest = node.nodeValue, i;
    while ((i = rest.toLowerCase().indexOf(needle)) >= 0) {
      frag.append(rest.slice(0, i));
      const m = document.createElement("mark"); m.textContent = rest.slice(i, i + q.length); frag.append(m);
      rest = rest.slice(i + q.length);
    }
    frag.append(rest);
    node.replaceWith(frag);
  }
  root.querySelector("mark")?.scrollIntoView({ block: "center" });
}

window.addEventListener("resize", () => { if (here() && S.data) { renderList(); renderPane(); } });
})();
