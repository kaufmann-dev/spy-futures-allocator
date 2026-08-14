from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.integrations.ibkr import IbkrUnavailable
from app.services.runtime import RuntimeUnavailable

logger = logging.getLogger(__name__)


class JobRuntime(Protocol):
    def maintain_ibkr(self) -> None: ...

    def refresh_market_data(self) -> None: ...

    def recalculate(self) -> None: ...

    def prune_auth(self) -> None: ...

    def bootstrap(self) -> None: ...


_runtime: JobRuntime | None = None
_runtime_lock = threading.Lock()


def configure_runtime(runtime: JobRuntime) -> None:
    global _runtime
    with _runtime_lock:
        _runtime = runtime


def _call(name: str) -> None:
    runtime = _runtime
    if runtime is None:
        logger.warning("scheduled job %s skipped before runtime initialization", name)
        return
    try:
        getattr(runtime, name)()
    except (IbkrUnavailable, RuntimeUnavailable) as exc:
        logger.warning("scheduled job %s blocked: %s", name, exc)
    except Exception:
        logger.exception("scheduled job %s failed closed", name)


def maintain_ibkr_job() -> None:
    _call("maintain_ibkr")


def refresh_market_data_job() -> None:
    _call("refresh_market_data")


def recalculate_job() -> None:
    _call("recalculate")


def prune_auth_job() -> None:
    _call("prune_auth")


def bootstrap_job() -> None:
    _call("bootstrap")


def build_scheduler(database_path: str | Path) -> BackgroundScheduler:
    path = Path(database_path).resolve()
    scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{path}")},
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    scheduler.add_job(
        maintain_ibkr_job,
        "interval",
        seconds=30,
        id="maintain-read-only-ibkr",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_market_data_job,
        "interval",
        seconds=30,
        id="refresh-ibkr-market-data",
        replace_existing=True,
    )
    scheduler.add_job(
        recalculate_job,
        "interval",
        minutes=5,
        id="recalculate-strategy-and-allocation",
        replace_existing=True,
    )
    scheduler.add_job(
        prune_auth_job,
        "interval",
        hours=6,
        id="prune-expired-auth-state",
        replace_existing=True,
    )
    return scheduler
