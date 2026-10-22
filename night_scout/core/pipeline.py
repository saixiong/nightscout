"""The nightly run: fetch → dedupe → pre-filter → Gemma scoring → action proposals → digest
→ new-project ideas.

Each stage reports live progress to runs.stats.progress (store.set_run_progress).

Nothing here executes an action. The run only proposes; the user decides in the UI.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from .config import Config
from .db import Store
from .dedupe import near_duplicate_groups
from .digest import rank_for_digest, write_digest
from .feedback import Weights, compute_weights
from .fetch import fetch_all
from .ideas import propose_ideas
from .judge import Judged, judge_item, propose_actions
from .llm import LLM, ModelError, ModelUnavailable
from .lock import RunLock
from .models import utcnow
from .prefilter import doc_text, query_texts, rank_candidates
from .profile import Profile, load_profile
from .providers import Embedder, close_llm, embed_model_name, ensure_ready, make_embedder, make_llm
from .sources.youtube import fetch_transcript

log = logging.getLogger(__name__)
MAX_FAILURE_STREAK = 3


@dataclass
class RunResult:
    run_id: int
    ok: bool
    status_lines: list[str] = field(default_factory=list)
    digest_path: Path | None = None
    stats: dict[str, int] = field(default_factory=dict)


def run_once(
    cfg: Config,
    fetch_only: bool = False,
    store: Store | None = None,
    llm: LLM | None = None,
    transcript_fn=fetch_transcript,
    today: date | None = None,
) -> RunResult:
    lock = RunLock(cfg.data_path() / "run.lock")
    if not lock.acquire():
        return RunResult(
            run_id=0, ok=False, status_lines=["Run: another night-scout run is already in progress"]
        )
    try:
        return _run_locked(cfg, fetch_only, store, llm, transcript_fn, today)
    finally:
        lock.release()


def _run_locked(
    cfg: Config,
    fetch_only: bool,
    store: Store | None,
    llm: LLM | None,
    transcript_fn: Any,
    today: date | None,
) -> RunResult:
    own_store = store is None
    store = store or Store(cfg.db_file())
    own_llm = llm is None and not fetch_only
    embedder: Embedder | None = None
    run_id = store.start_run()
    result = RunResult(run_id=run_id, ok=True)
    try:
        statuses = fetch_all(
            cfg,
            store,
            run_id,
            progress=lambda done, total: store.set_run_progress(run_id, "fetch", done, total),
        )
        result.status_lines = [s.line() for s in statuses]
        result.stats["new"] = sum(s.new for s in statuses)
        result.stats["sources_failed"] = sum(1 for s in statuses if not s.ok)
        if statuses and all(not s.ok for s in statuses):
            result.ok = False
        if not fetch_only:
            if llm is None:
                llm = make_llm(cfg, "judge")
                embedder = make_embedder(cfg)
            _judge_and_digest(cfg, store, llm, run_id, result, transcript_fn, today, embedder)
        store.finish_run(run_id, "done" if result.ok else "failed", result.stats)
    except Exception as e:
        log.exception("run %s failed", run_id)
        result.status_lines.append(f"Run: failed ({type(e).__name__}: {e})")
        store.finish_run(run_id, "failed", result.stats)
        result.ok = False
    finally:
        if own_llm:
            close_llm(llm)
            close_llm(embedder)
        if own_store:
            store.close()
    return result


def _status(
    store: Store,
    run_id: int,
    result: RunResult,
    key: str,
    label: str,
    ok: bool,
    message: str,
    fetched: int = 0,
    new: int = 0,
) -> None:
    from .models import SourceStatus

    st = SourceStatus(key, label, ok=ok, fetched=fetched, new=new, message=message)
    store.record_source_status(run_id, st)
    result.status_lines.append(f"{label}: {message}" if ok else st.line())


def _judge_and_digest(
    cfg: Config,
    store: Store,
    llm: LLM,
    run_id: int,
    result: RunResult,
    transcript_fn,
    today: date | None,
    embedder: Embedder | None = None,
) -> None:
    emb: Embedder = embedder or llm
    try:
        profile = load_profile(cfg.profile_file())
    except FileNotFoundError as e:
        _status(store, run_id, result, "pipeline:profile", "Profile", False, str(e))
        result.ok = False
        return
    if not profile.projects:
        _status(store, run_id, result, "pipeline:profile", "Profile", False, "no projects")
        result.ok = False
        return

    try:
        ensure_ready(llm)
    except ModelUnavailable as e:
        _status(store, run_id, result, "pipeline:model", "Gemma", False, str(e))
        result.ok = False
        return

    def progress(stage: str, done: int, total: int) -> None:
        try:
            store.set_run_progress(run_id, stage, done, total)
        except Exception:  # progress is cosmetic; never let it sink the run
            log.exception("progress update failed")

    weights = compute_weights(store)
    cutoff = (utcnow() - timedelta(days=cfg.thresholds.max_age_days)).isoformat()
    # A run that was killed or lost the model mid-way leaves candidates behind; retry them.
    # (The run lock guarantees no other run is scoring them right now.)
    with store.tx() as conn:
        conn.execute("UPDATE items SET status='new' WHERE status='candidate'")
    pending = [r for r in store.items_with_status("new") if r["first_seen"] >= cutoff]

    # Embeddings: near-duplicate detection and semantic pre-filter. Optional.
    progress("dedupe", 0, len(pending))
    item_vecs: dict[int, list[float]] | None = None
    query_vecs: list[list[float]] | None = None
    if pending and emb.has_embed_model():
        try:
            vecs = emb.embed([doc_text(r) for r in pending])
            item_vecs = {r["id"]: v for r, v in zip(pending, vecs, strict=True)}
            query_vecs = emb.embed(query_texts(profile))
        except ModelError as e:
            log.warning("embeddings unavailable, keyword pre-filter only: %s", e)
            item_vecs = None
            _status(
                store,
                run_id,
                result,
                "pipeline:embed",
                "Embeddings",
                False,
                f"unavailable ({e}); keyword pre-filter only",
            )
    elif pending:
        _status(
            store,
            run_id,
            result,
            "pipeline:embed",
            "Embeddings",
            False,
            f"{embed_model_name(cfg)} not installed; keyword pre-filter only",
        )
    if item_vecs:
        dups = near_duplicate_groups(item_vecs, cfg.thresholds.near_duplicate)
        by_id = {r["id"]: r for r in pending}
        for dup, orig in dups.items():
            store.set_item_fields(dup, status="duplicate", dup_of=orig)
            seen = json.loads(store.item(orig)["also_seen"])  # type: ignore[index]
            tag = f"{by_id[dup]['source']}:{by_id[dup]['source_name']}"
            if tag not in seen:
                seen.append(tag)
                store.set_item_fields(orig, also_seen=json.dumps(seen))
        pending = [r for r in pending if r["id"] not in dups]
        result.stats["near_duplicates"] = len(dups)
        # re-read so also_seen changes count toward the buzz boost
        pending = [r for r in (store.item(r["id"]) for r in pending) if r is not None]

    progress("dedupe", len(pending), len(pending))
    progress("prefilter", 0, len(pending))
    ranked = rank_candidates(pending, profile, weights, item_vecs, query_vecs)
    chosen = ranked[: cfg.thresholds.max_scored]
    chosen_ids = {c.item_id for c in chosen}
    for c in ranked:
        store.set_item_fields(
            c.item_id,
            prefilter=c.score,
            status="candidate" if c.item_id in chosen_ids else "filtered",
        )
    result.stats["candidates"] = len(chosen)
    progress("prefilter", len(pending), len(pending))

    scored = failed = with_actions = 0
    lost_model: str | None = None
    streak: list[int] = []  # consecutive judge failures; a hung model looks like this
    t0 = time.monotonic()
    worth_actions: list[tuple[sqlite3.Row, Judged, str | None]] = []
    progress("judge", 0, len(chosen))
    for i, cand in enumerate(chosen, 1):
        if i > 1:
            progress("judge", i - 1, len(chosen))
        row = store.item(cand.item_id)
        if row is None:
            continue
        extra = json.loads(row["extra"] or "{}")
        transcript = None
        if row["source"] == "youtube" and extra.get("want_transcript") and extra.get("video_id"):
            transcript = transcript_fn(extra["video_id"])
            store.update_extra(row["id"], has_transcript=bool(transcript))
            if transcript:
                store.update_extra(row["id"], transcript_excerpt=transcript[:6000])
        try:
            judged = judge_item(llm, profile, row, transcript)
        except ModelUnavailable as e:
            # Ollama went away. Don't burn the remaining items as failures; they stay
            # candidates and the next run picks them up again.
            lost_model = str(e)
            log.error("model unavailable at item %d/%d: %s", i, len(chosen), e)
            break
        except ModelError as e:
            failed += 1
            streak.append(row["id"])
            store.set_item_fields(row["id"], status="failed")
            store.update_extra(row["id"], model_error=str(e)[:300])
            log.warning("[%d/%d] judge failed for item %s: %s", i, len(chosen), row["id"], e)
            if len(streak) >= MAX_FAILURE_STREAK:
                # Three in a row is the model, not the items: give them back for next time.
                for item_id in streak:
                    store.set_item_fields(item_id, status="candidate")
                failed -= len(streak)
                lost_model = f"{len(streak)} judgements failed in a row (last: {str(e)[:80]})"
                break
            continue
        streak.clear()
        store.save_score(
            row["id"],
            llm.model_name,
            judged.summary,
            judged.per_project,
            judged.topics,
            impact=judged.impact,
            matched=judged.matched,
            next_step=judged.next_step,
        )
        store.update_extra(row["id"], kind=judged.kind)
        scored += 1
        best = judged.best or {}
        relevance = judged.best_relevance
        log.info(
            "[%d/%d] %.2f %s :: %s",
            i,
            len(chosen),
            relevance,
            best.get("project"),
            row["title"][:80],
        )
        if relevance >= cfg.thresholds.relevance:
            worth_actions.append((row, judged, transcript))
    if lost_model is None:
        progress("judge", len(chosen), len(chosen))

    # Next steps for the items that cleared the bar, once judging is done.
    if lost_model is None:
        progress("actions", 0, len(worth_actions))
    for n, (row, judged, transcript) in enumerate(worth_actions, 1):
        if lost_model is not None:
            store.update_extra(row["id"], actions_error="model unavailable")
            continue
        try:
            actions = propose_actions(llm, profile, row, judged, transcript)
        except ModelUnavailable as e:
            lost_model = str(e)
            store.update_extra(row["id"], actions_error=f"model unavailable: {str(e)[:200]}")
            continue
        except ModelError as e:
            log.warning("actions failed for item %s: %s", row["id"], e)
            store.update_extra(row["id"], actions_error=str(e)[:300])
        else:
            for a in actions:
                aid = store.add_action(row["id"], a.type, a.project, a.title, a.detail)
                if a.status != "proposed":
                    store.set_action(aid, a.status)
            with_actions += 1
        progress("actions", n, len(worth_actions))
    elapsed = time.monotonic() - t0
    result.stats.update(scored=scored, judge_failed=failed, with_actions=with_actions)
    model_msg = f"scored {scored}/{len(chosen)} in {elapsed / 60:.1f} min"
    if failed:
        model_msg += f", {failed} failed (logged and skipped)"
    model_ok = lost_model is None and not (scored == 0 and failed > 0)
    if lost_model:
        model_msg += (
            f"; stopped early, model unavailable ({lost_model[:120]}). "
            "The rest wait for the next run"
        )
    elif not model_ok:
        model_msg += "; every judgement failed"
    if not model_ok:
        result.ok = False
    _status(
        store,
        run_id,
        result,
        "pipeline:model",
        f"Gemma ({llm.model_name})",
        model_ok,
        model_msg,
        fetched=len(chosen),
        new=scored,
    )

    # The digest covers everything judged today, so a second run the same day adds to
    # the morning list instead of replacing it.
    day = today or date.today()
    midnight = datetime.combine(day, dtime.min).astimezone().astimezone(UTC).isoformat()
    run_row = store.conn.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
    day_start = min(midnight, run_row["started_at"])
    weights = compute_weights(store)
    progress("digest", 0, 1)
    scored_today = store.scored_since(day_start)
    picks = rank_for_digest(
        scored_today,
        weights,
        cfg.thresholds.relevance,
        cfg.thresholds.digest_size,
    )
    result.digest_path = write_digest(
        store,
        cfg.digests_dir(),
        run_id,
        day,
        picks,
        result.stats.get("new", 0),
        result.status_lines,
        llm.model_name,
    )
    result.stats["digest_items"] = len(picks)
    progress("digest", 1, 1)

    if cfg.thresholds.ideas > 0 and lost_model is None:
        _ideas(cfg, store, llm, profile, run_id, result, day, scored_today, weights, progress)


def _ideas(
    cfg: Config,
    store: Store,
    llm: LLM,
    profile: Profile,
    run_id: int,
    result: RunResult,
    day: date,
    scored_today: list[sqlite3.Row],
    weights: Weights,
    progress: Callable[[str, int, int], None],
) -> None:
    """New side-project ideas from today's best items. A failure here never fails the run."""
    n = cfg.thresholds.ideas
    progress("ideas", 0, n)
    rows = [
        r
        for r in scored_today
        if r["id"] not in weights.ignored_items and not weights.muted(r["source_name"])
    ]
    if not rows:
        _status(store, run_id, result, "pipeline:ideas", "Ideas", True, "no items to draw on")
        progress("ideas", n, n)
        return
    try:
        ideas = propose_ideas(llm, profile, rows, n, avoid=store.recent_idea_titles())
        store.save_ideas(run_id, day.isoformat(), ideas)
    except Exception as e:  # ModelError, or anything else: ideas are a bonus
        log.warning("ideas failed: %s", e, exc_info=not isinstance(e, ModelError))
        _status(
            store,
            run_id,
            result,
            "pipeline:ideas",
            "Ideas",
            False,
            f"failed ({type(e).__name__}: {str(e)[:120]})",
        )
        return
    finally:
        progress("ideas", n, n)
    result.stats["ideas"] = len(ideas)
    _status(
        store,
        run_id,
        result,
        "pipeline:ideas",
        "Ideas",
        True,
        f"{len(ideas)} new project idea{'s' if len(ideas) != 1 else ''}",
    )
