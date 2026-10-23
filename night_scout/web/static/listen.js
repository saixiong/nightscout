/* Night Scout: Listen to your Digest (approved mockup, Oct 23). The ▶ Listen row in the Digest
   hero, ▶ Listen on each story, and a docked player that keeps playing across pages: over the
   left column on a desktop, a bar above the tab bar on a phone. Each chapter (intro, one per
   story, outro) is its own MP3; playback starts as soon as the first one is recorded.
   Only exists with the voice add-on (GET /api/listen says available). Strict CSP: no inline
   styles or handlers; the bar's fill and ticks are positioned with element.style (CSSOM). */
(() => {
"use strict";
const { $, $$, esc, api, failToast, toast } = NS;
const SPEEDS = [0.9, 1, 1.25, 1.5];
const mmss = s => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const minutes = s => `${Math.max(1, Math.round((s || 0) / 60))} min`;

/* S.info: GET /api/listen for the Digest on screen (S.day). P: the player (its own night). */
const S = { day: null, info: null, available: null };
const P = { info: null, day: null, idx: -1, open: false, wantPlay: false, rate: 1, waitingFor: null };
const audio = new Audio();
audio.preload = "auto";

const durs = info => info.chapters.map(c => c.seconds ?? c.est);
const starts = info => { let t = 0; return durs(info).map(d => { const s = t; t += d; return s; }); };
const total = info => durs(info).reduce((a, b) => a + b, 0);
const storyN = (info, idx) => info.chapters.slice(0, idx + 1).filter(c => c.story_id != null).length;
const recording = info => info && ["waiting", "recording"].includes(info.status);

/* ---------- the server's state ---------- */
async function fetchInfo(day) {
  const r = await api("GET", "/api/listen" + (day ? `?day=${encodeURIComponent(day)}` : ""));
  if (!r.ok) return null;
  S.available = !!r.data.available;
  return r.data;
}
/* The Digest asks when it renders a night; this fills its ▶ Listen row and story buttons. */
async function status(day) {
  if (S.day === day && S.info) { hero(); storyButtons(); mark(); } // paint now, refresh below
  S.day = day;
  const info = await fetchInfo(day);
  if (S.day !== day) return;
  S.info = info && info.available ? info : null;
  if (P.info && P.day === day && S.info && S.info.key === P.info.key) P.info = S.info;
  hero(); storyButtons(); mark(); watch();
}
/* Keep asking while anything shown is recording: the player's night, or the Digest on screen
   (its hero says "Recording… n of 16" even with no player open, say during a nightly one). */
let watchT = null;
function watch() {
  clearTimeout(watchT);
  const day = P.info && recording(P.info) ? P.day : S.info && recording(S.info) ? S.day : null;
  if (!day) return;
  watchT = setTimeout(async () => {
    const info = await fetchInfo(day);
    if (info && info.available) {
      if (P.info && P.day === info.day) {
        if (P.info.key === info.key) {
          if (info.status === "failed" && P.info.status !== "failed") toast(`<b>Couldn't record the rest.</b> ${esc(info.error || "")}`);
          P.info = info;
          if (P.waitingFor !== null && info.chapters[P.waitingFor]?.url) playChapter(P.waitingFor, 0);
        } else if (recording(P.info)) { // the voice or mode changed: this recording won't go on
          P.info = { ...P.info, status: "failed" };
          if (P.waitingFor !== null) toast("<b>The voice settings changed.</b> Press ▶ Listen to hear the new version.");
        }
      }
      if (S.day === info.day) S.info = info;
    }
    hero(); storyButtons(); draw(); watch();
  }, 1000);
}

/* ---------- starting ---------- */
async function start(day, storyId) {
  const r = await api("POST", "/api/listen", { day, ...(storyId ? { story: +storyId } : {}) });
  if (!r.ok) return failToast(r, "Listen");
  const info = r.data;
  const same = P.info && P.info.key === info.key;
  P.info = info; P.day = info.day; P.open = true; P.wantPlay = true;
  if (!same) P.rate = SPEEDS.includes(info.speed) ? info.speed : 1;
  if (S.day === info.day) S.info = info;
  let idx = 0;
  if (storyId) idx = Math.max(0, info.chapters.findIndex(c => c.story_id === +storyId));
  else if (same && P.idx >= 0) idx = P.idx;
  unlock();
  build(); playChapter(idx, same && !storyId && P.idx === idx ? audio.currentTime : 0);
  watch();
}
/* iOS/Safari only lets audio play from a tap: touch the element now, while we still have one. */
function unlock() { if (!audio.src) { try { audio.load(); } catch (e) { /* nothing to load yet */ } } }

function playChapter(idx, at) {
  const info = P.info; if (!info) return;
  idx = Math.max(0, Math.min(idx, info.chapters.length - 1));
  P.idx = idx;
  const ch = info.chapters[idx];
  if (!ch.url) { // not recorded yet: wait for it (watch() plays it when it lands)
    P.waitingFor = idx; audio.pause();
    hero(); draw(); mark(); return;
  }
  P.waitingFor = null;
  if (!audio.src.endsWith(ch.url)) { audio.src = ch.url; }
  const go = () => { if (at) { try { audio.currentTime = at; } catch (e) { /* not seekable yet */ } } audio.playbackRate = P.rate; if (P.wantPlay) audio.play().catch(() => { P.wantPlay = false; hero(); draw(); }); };
  if (audio.readyState >= 1) go(); else audio.addEventListener("loadedmetadata", go, { once: true });
  media(); hero(); draw(); mark();
}
audio.addEventListener("ended", () => {
  if (!P.info) return;
  if (P.idx < P.info.chapters.length - 1) return playChapter(P.idx + 1, 0);
  P.wantPlay = false; P.idx = 0; audio.removeAttribute("src"); audio.load(); // the end: back to the top, paused
  hero(); draw(); mark();
});
["play", "pause"].forEach(e => audio.addEventListener(e, () => { hero(); draw(); media(); }));
audio.addEventListener("ratechange", () => { if (audio.playbackRate !== P.rate) audio.playbackRate = P.rate; });
audio.addEventListener("timeupdate", () => tick());

function toggle() {
  if (!P.info) return;
  if (!audio.paused || (P.wantPlay && P.waitingFor !== null)) { P.wantPlay = false; audio.pause(); hero(); draw(); return; }
  P.wantPlay = true;
  if (P.idx < 0) P.idx = 0;
  if (audio.src && P.waitingFor === null) { audio.playbackRate = P.rate; audio.play().catch(() => {}); hero(); draw(); } // paused is false already
  else playChapter(P.idx, 0);
}
function step(d) { if (P.info) playChapter(P.idx + d, 0); }
function speed() {
  P.rate = SPEEDS[(SPEEDS.indexOf(P.rate) + 1) % SPEEDS.length];
  audio.playbackRate = P.rate; draw();
}
function close() {
  P.wantPlay = false; audio.pause(); audio.removeAttribute("src"); audio.load();
  Object.assign(P, { info: null, day: null, idx: -1, open: false, waitingFor: null });
  $("#lplayer")?.remove(); document.body.classList.remove("has-player");
  if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
  hero(); mark(); watch(); // the Digest's row keeps following a recording that's still going
}
function seek(frac) {
  const info = P.info; if (!info) return;
  const t = frac * total(info), st = starts(info), d = durs(info);
  let i = st.findIndex((s, k) => t < s + d[k]); if (i < 0) i = st.length - 1;
  P.wantPlay = true; playChapter(i, Math.max(0, t - st[i]));
}

/* ---------- the docked player ---------- */
function build() {
  let el = $("#lplayer");
  if (!el) {
    el = document.createElement("div");
    el.id = "lplayer"; el.className = "lplayer"; el.setAttribute("role", "region"); el.setAttribute("aria-label", "Listen to your Digest");
    el.innerHTML = `<div class="lp-top"><button class="lp-play" id="lpPlay" type="button" aria-label="Play">▶</button>
      <div class="lp-now"><small id="lpSub"></small><b id="lpTitle"></b></div>
      <div class="lp-ctl"><button id="lpPrev" class="lp-prev" type="button" title="Previous story" aria-label="Previous story">⏮</button><button id="lpNext" type="button" title="Next story" aria-label="Next story">⏭</button><button id="lpRate" type="button" title="Speed">1×</button><button id="lpClose" class="lp-close" type="button" title="Close" aria-label="Close the player">✕</button></div></div>
      <div class="lp-bar" id="lpBar"><i id="lpFill"></i></div>
      <div class="lp-time"><span id="lpAt">0:00</span><span id="lpLeft"></span></div>`;
    document.body.appendChild(el);
    $("#lpPlay").onclick = toggle; $("#lpPrev").onclick = () => step(-1); $("#lpNext").onclick = () => step(1);
    $("#lpRate").onclick = speed; $("#lpClose").onclick = close;
    $("#lpBar").onclick = e => { const r = $("#lpBar").getBoundingClientRect(); seek(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))); };
  }
  document.body.classList.add("has-player");
  place(); draw();
}
/* Desktop: over the left column (whatever page is open); phone: CSS docks it above the tab bar. */
function place() {
  const el = $("#lplayer"); if (!el) return;
  if (matchMedia("(max-width: 760px)").matches) { el.style.left = ""; el.style.width = ""; return; }
  const col = $(".col-left");
  if (!col) { el.style.left = ""; el.style.width = ""; return; }
  const r = col.getBoundingClientRect();
  el.style.left = Math.round(r.left + 4) + "px"; el.style.width = Math.round(r.width - 8) + "px";
}
function draw() {
  const el = $("#lplayer"), info = P.info; if (!el || !info) return;
  const ch = info.chapters[Math.max(0, P.idx)] || info.chapters[0];
  const n = info.stories, k = storyN(info, Math.max(0, P.idx));
  const playing = !audio.paused;
  $("#lpPlay").textContent = playing ? "❚❚" : "▶";
  $("#lpPlay").setAttribute("aria-label", playing ? "Pause" : "Play");
  const where = ch.story_id != null ? `${k} of ${n}` : ch.id === "intro" ? "Intro" : "The end";
  const wait = P.waitingFor !== null && P.wantPlay ? ` · recording… ${info.stories_done} of ${n}` : "";
  $("#lpSub").textContent = `${ch.story_id != null && !matchMedia("(max-width: 760px)").matches ? "Reading " : ""}${where} · ${info.voice_name}${wait}`;
  $("#lpTitle").textContent = ch.title;
  $("#lpRate").textContent = `${P.rate}×`;
  $("#lpPrev").disabled = P.idx <= 0; $("#lpNext").disabled = P.idx >= info.chapters.length - 1;
  const bar = $("#lpBar"), T = total(info) || 1, st = starts(info);
  const want = info.chapters.map((c, i) => c.story_id != null ? (100 * st[i] / T).toFixed(2) : null).filter(x => x !== null);
  const have = $$("em", bar);
  if (have.length !== want.length || have.some((e, i) => e.dataset.at !== want[i])) {
    have.forEach(e => e.remove());
    want.forEach(p => { const e = document.createElement("em"); e.dataset.at = p; e.style.left = p + "%"; bar.appendChild(e); });
  }
  tick();
}
function tick() {
  const info = P.info; if (!info || !$("#lplayer")) return;
  const st = starts(info), d = durs(info), i = Math.max(0, P.idx);
  const at = (st[i] || 0) + Math.min(P.waitingFor === null ? audio.currentTime || 0 : 0, d[i] || 0), T = total(info);
  $("#lpFill").style.width = (T ? Math.min(100, 100 * at / T) : 0) + "%";
  $("#lpAt").textContent = mmss(at);
  $("#lpLeft").textContent = `−${mmss(Math.max(0, T - at))}${info.estimated ? " est." : ""}`;
  if ("mediaSession" in navigator && navigator.mediaSession.setPositionState && T > at) {
    try { navigator.mediaSession.setPositionState({ duration: T, position: at, playbackRate: P.rate }); } catch (e) { /* older browsers */ }
  }
}

/* ---------- the Digest: hero row, story buttons, NOW READING ---------- */
function heroState() {
  const info = S.info; if (!info || !info.stories) return null;
  const mine = P.info && P.info.key === info.key;
  const pinfo = mine ? P.info : info;
  const meta = `${esc(pinfo.voice_name)} · ${pinfo.stories} stor${pinfo.stories === 1 ? "y" : "ies"} · ${minutes(total(pinfo))}`;
  if (mine && !audio.paused) return { label: "❚❚ Listening", meta, on: true };
  if (mine && P.wantPlay && P.waitingFor !== null) return { label: `Recording… ${pinfo.stories_done} of ${pinfo.stories}`, meta: "Playback starts as soon as a story is ready", on: true };
  if (recording(pinfo)) return { label: "▶ Listen", meta: `Recording… ${pinfo.stories_done} of ${pinfo.stories}` };
  if (pinfo.status === "failed") return { label: "▶ Listen", meta: `Couldn't record it last time · tap to try again` };
  return { label: "▶ Listen", meta };
}
function hero() {
  const row = $("#listenRow"); if (!row) return;
  const h = heroState();
  if (!h) { row.innerHTML = ""; row.hidden = true; return; }
  row.hidden = false;
  row.innerHTML = `<button class="btn primary listenbtn ${h.on ? "on" : ""}" id="listenBtn" type="button">${esc(h.label)}</button><span class="muted" id="listenMeta">${h.meta}</span>`;
  $("#listenBtn").onclick = () => {
    const mine = P.info && P.info.key === S.info?.key;
    if (mine && P.idx >= 0) return toggle();
    start(S.day);
  };
}
/* ▶ Listen in each story's vote row (a text label: a speaker icon would read as Mute) */
function storyButtons() {
  const ok = !!(S.info && S.info.stories);
  const ids = new Set((S.info?.chapters || []).map(c => c.story_id).filter(x => x != null));
  $$("#feed .story").forEach(s => {
    const v = $(".votes", s), b = $(".vote.listen", s), id = +s.dataset.id;
    if (!v) return;
    if (ok && ids.has(id) && !b) v.insertAdjacentHTML("afterbegin", `<button class="vote listen" type="button" data-listen="${id}" title="Listen to this story">▶ Listen</button>`);
    if ((!ok || !ids.has(id)) && b) b.remove();
  });
}
function mark() {
  const ch = P.info && P.open && P.idx >= 0 ? P.info.chapters[P.idx] : null;
  const id = ch && P.day === S.day ? ch.story_id : null;
  $$(".dmini").forEach(m => m.classList.toggle("reading", id != null && +m.dataset.id === id));
}
document.addEventListener("click", e => {
  const b = e.target.closest?.("[data-listen]"); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  start(S.day, +b.dataset.listen);
});

/* ---------- lock screen / headphone controls ---------- */
function media() {
  if (!("mediaSession" in navigator) || !P.info) return;
  const ch = P.info.chapters[Math.max(0, P.idx)];
  try {
    navigator.mediaSession.metadata = new MediaMetadata({ title: ch?.title || "Your Digest", artist: `Night Scout · ${P.info.voice_name}`, album: `Digest · ${P.day}` });
    navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing";
  } catch (e) { /* MediaMetadata missing */ }
}
if ("mediaSession" in navigator) {
  const on = (a, fn) => { try { navigator.mediaSession.setActionHandler(a, fn); } catch (e) { /* unsupported action */ } };
  on("play", () => { if (audio.paused) toggle(); });
  on("pause", () => { if (!audio.paused) toggle(); });
  on("previoustrack", () => step(-1));
  on("nexttrack", () => step(1));
}

addEventListener("resize", () => { place(); draw(); });
addEventListener("hashchange", () => setTimeout(place, 60)); // the new page's left column
NS.listen = {
  status, start, toggle, close, place, mark, storyButtons,
  available: () => S.available === true && !!S.info,
  defaultRate: r => { if (SPEEDS.includes(r) && !P.info) P.rate = r; },
  pause: () => { if (!audio.paused) { P.wantPlay = false; audio.pause(); } },
  state: () => ({ open: P.open, idx: P.idx, day: P.day, key: P.info?.key, playing: !audio.paused, rate: P.rate, audioRate: audio.playbackRate, waiting: P.waitingFor, src: audio.src, time: audio.currentTime }),
};
})();
