"""Database-backed job queue used by the UI, the scheduler and `run_worker`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_tz

from ..exchanges.gate import get_client
from ..models import Coin, Heartbeat, Job
from .levels import compute_levels
from .sync import run_sync
from .verify import verify_coin

log = logging.getLogger(__name__)
ACTIVE = (Job.Status.QUEUED, Job.Status.RUNNING)

# Other apps (e.g. trading) register handlers for their own job kinds: handler(job) -> result message.
_HANDLERS: dict[str, Callable[[Job], str]] = {}


def register_handler(kind: str, handler: Callable[[Job], str]) -> None:
    _HANDLERS[kind] = handler


def enqueue(
    kind: str, coin: Coin, params: dict | None = None, origin: str = Job.Origin.USER, dedupe: bool = True
) -> tuple[Job, bool]:
    """Queue a job. With dedupe, an already queued/running job of the same kind for this coin is reused.

    Returns (job, created).
    """
    with transaction.atomic():
        if dedupe:
            existing = Job.objects.filter(kind=kind, coin=coin, status__in=ACTIVE).order_by("created_at").first()
            if existing:
                return existing, False
        return Job.objects.create(kind=kind, coin=coin, params=params or {}, origin=origin), True


def claim_next() -> Job | None:
    """Atomically move the oldest queued job to RUNNING and return it (safe with several workers)."""
    for job_id in Job.objects.filter(status=Job.Status.QUEUED).order_by("created_at").values_list("id", flat=True)[:5]:
        claimed = Job.objects.filter(id=job_id, status=Job.Status.QUEUED).update(
            status=Job.Status.RUNNING, started_at=dj_tz.now()
        )
        if claimed:
            return Job.objects.select_related("coin").get(id=job_id)
    return None


def run_job(job: Job) -> Job:
    coin = job.coin
    try:
        if job.kind in _HANDLERS:
            message = _HANDLERS[job.kind](job)
        elif job.kind == Job.Kind.SYNC:
            stats = run_sync(coin.pk, days=job.params.get("days"), use_archive=job.params.get("archive", True))
            message = str(stats)
        elif job.kind == Job.Kind.VERIFY:
            res = verify_coin(get_client(), coin)
            if not res.ok:
                raise RuntimeError(res.summary)
            message = res.summary
        elif job.kind == Job.Kind.LEVELS:
            end = datetime.now(timezone.utc).date() + timedelta(days=1)
            days = job.params.get("days") or settings.MARKET["HISTORY_DAYS"]
            message = f"{compute_levels(coin, end - timedelta(days=days + 1), end)} day(s) recomputed"
        else:
            raise ValueError(f"Unknown job kind {job.kind}")
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        log.exception("Job %s failed", job.pk)
        job.status, job.message = Job.Status.FAILED, str(exc)[:4000]
    else:
        job.status, job.message = Job.Status.DONE, message[:4000]
    job.finished_at = dj_tz.now()
    job.save(update_fields=["status", "message", "finished_at"])
    return job


def recover_interrupted() -> int:
    """Jobs left RUNNING by a worker that died are marked failed so they can be re-queued."""
    return Job.objects.filter(status=Job.Status.RUNNING).update(
        status=Job.Status.FAILED, message="Interrupted (worker stopped)", finished_at=dj_tz.now()
    )


def prune(keep_days: int = 14) -> int:
    cutoff = dj_tz.now() - timedelta(days=keep_days)
    return Job.objects.filter(finished_at__lt=cutoff).delete()[0]


def beat(name: str, info: str = "") -> None:
    Heartbeat.objects.update_or_create(name=name, defaults={"last_seen": dj_tz.now(), "info": info[:255]})


def service_status() -> dict[str, Heartbeat | None]:
    beats = {h.name: h for h in Heartbeat.objects.filter(name__in=["worker", "stream"])}
    return {"worker": beats.get("worker"), "stream": beats.get("stream")}
