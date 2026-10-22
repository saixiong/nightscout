"""Stage 1 of the nightly run: fetch every source, store new items, record status lines."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from .config import Config
from .db import Store
from .http import PoliteClient, SourceError
from .models import SourceStatus, utcnow
from .sources import SourceTask, all_tasks

log = logging.getLogger(__name__)


def fetch_all(
    cfg: Config,
    store: Store,
    run_id: int,
    client: PoliteClient | None = None,
    tasks: list[SourceTask] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[SourceStatus]:
    """Fetch each source independently. One failing source never stops the others.

    `progress(done, total)` is called before the first source and after each one.
    """
    tasks = all_tasks(cfg) if tasks is None else tasks
    own_client = client is None
    client = client or PoliteClient(contact=cfg.contact)
    cutoff = utcnow() - timedelta(days=cfg.thresholds.max_age_days)
    statuses: list[SourceStatus] = []
    try:
        if progress:
            progress(0, len(tasks))
        for n, task in enumerate(tasks, 1):
            try:
                items = task.fetch(client)
            except SourceError as e:
                st = SourceStatus(task.key, task.label, ok=False, message=str(e))
                log.warning("%s: %s", task.label, e)
            except Exception as e:  # an adapter bug must not sink the run
                st = SourceStatus(
                    task.key, task.label, ok=False, message=f"failed ({type(e).__name__}: {e})"
                )
                log.exception("%s failed", task.label)
            else:
                new = 0
                for item in items:
                    if item.published_at and item.published_at < cutoff:
                        continue
                    if store.upsert_item(item, run_id).new:
                        new += 1
                st = SourceStatus(task.key, task.label, ok=True, fetched=len(items), new=new)
            store.record_source_status(run_id, st)
            statuses.append(st)
            log.info(st.line())
            if progress:
                progress(n, len(tasks))
    finally:
        if own_client:
            client.close()
    return statuses
