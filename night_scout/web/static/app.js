// Night Scout: small progressive enhancement. Everything works as plain links/forms too.
(() => {
  const csrf = document.body.dataset.csrf;

  const post = async (url, data) => {
    const body = new URLSearchParams({ csrf_token: csrf, ...data });
    const res = await fetch(url, { method: "POST", body, headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  };

  // Relevance bars: widths come from data attributes (the CSP forbids inline styles).
  requestAnimationFrame(() => {
    document.querySelectorAll(".bar > span[data-w]").forEach((el) => {
      el.style.width = `${Math.max(2, Math.min(100, Number(el.dataset.w)))}%`;
    });
  });

  // 👍 / 👎 / ✕ on cards.
  document.querySelectorAll(".votes").forEach((box) => {
    box.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".vote");
      if (!btn) return;
      try {
        await post(`/item/${box.dataset.item}/feedback`, { value: btn.dataset.vote });
        box.querySelectorAll(".vote").forEach((b) => b.classList.toggle("on", b === btn));
        const card = box.closest(".card");
        if (card) card.classList.toggle("gone", btn.dataset.vote !== "up");
      } catch (e) { alert(`Couldn't save the vote: ${e.message}`); }
    });
  });

  // Source / topic tuning chips.
  document.querySelectorAll(".js-fb").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const out = document.querySelector(".js-fb-out");
      try {
        const r = await post(`/feedback/${btn.dataset.kind}`, { target: btn.dataset.target, value: btn.dataset.value });
        const what = btn.dataset.kind === "source" ? `Source “${btn.dataset.target}”` : `Topic “${btn.dataset.target}”`;
        out.textContent = `${what} weight is now ${r.weight > 0 ? "+" : ""}${r.weight}${r.muted ? " (muted)" : ""}. Applies from the next run.`;
      } catch (e) { out.textContent = `Couldn't save: ${e.message}`; }
    });
  });

  // Research more: start, then poll until Gemma is done.
  const brief = document.getElementById("brief");
  const pollResearch = (item) => {
    const tick = async () => {
      const r = await (await fetch(`/item/${item}/research/status`)).json();
      if (r.state === "running") { setTimeout(tick, 2000); return; }
      if (r.state === "error") { brief.innerHTML = ""; const p = document.createElement("p"); p.className = "warn"; p.textContent = r.error; brief.append(p); }
      else if (r.html) { brief.innerHTML = r.html; }
      const b = document.querySelector(".js-research button");
      if (b) { b.disabled = false; b.textContent = "Rewrite"; }
    };
    tick();
  };
  document.querySelectorAll(".js-research").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const b = form.querySelector("button");
      b.disabled = true; b.textContent = "Writing…";
      brief.innerHTML = '<p class="muted">Gemma is reading the full source…</p>';
      try { await post(`/item/${form.dataset.item}/research`, {}); pollResearch(form.dataset.item); }
      catch (e) { b.disabled = false; brief.textContent = e.message; }
    });
    if (brief && brief.dataset.state === "running") pollResearch(form.dataset.item);
  });

  // Try-it: poll while running, then show the verdict and log.
  document.querySelectorAll(".runlog[data-running='yes']").forEach((box) => {
    const id = box.dataset.action;
    const pill = document.querySelector(`[data-status-for='${id}']`);
    const verdict = box.querySelector(".verdict");
    const log = box.querySelector(".log");
    box.classList.remove("hidden");
    const tick = async () => {
      const r = await (await fetch(`/action/${id}/status`)).json();
      if (pill) { pill.textContent = r.status; pill.className = `pill st-${r.status}`; }
      if (r.status === "running") {
        verdict.textContent = "⏳ Running in the sandbox…";
        setTimeout(tick, 2000);
        return;
      }
      verdict.textContent = `${r.status === "passed" ? "✅ Passed" : "❌ Failed"} · ${r.reason}${r.seconds ? ` · ${r.seconds}s in ${r.mode}` : ""}`;
      log.textContent = r.log || "";
    };
    tick();
  });
})();
