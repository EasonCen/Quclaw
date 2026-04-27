"""Cron worker for scheduled job dispatch."""

import asyncio
import logging
import shutil
from datetime import datetime
from typing import TYPE_CHECKING

from croniter import croniter

from .base import Worker
from runtime.events import CronEventSource, DispatchEvent

if TYPE_CHECKING:
    from core.context import SharedContext
    from core.cron_loader import CronDef

logger = logging.getLogger(__name__)


def find_due_jobs(
    jobs: list["CronDef"],
    now: datetime | None = None,
) -> list["CronDef"]:
    """Find all jobs that are due to run."""
    if not jobs:
        return []

    now = now or datetime.now()
    now_minute = now.replace(second=0, microsecond=0)

    due_jobs: list["CronDef"] = []
    for job in jobs:
        try:
            if croniter.match(job.schedule, now_minute):
                due_jobs.append(job)
        except Exception as exc:
            logger.warning("Error checking schedule for job %s: %s", job.id, exc)
            continue

    return due_jobs


class CronWorker(Worker):
    """Finds due cron jobs, publishes DISPATCH events."""

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self._last_run_minutes: dict[str, datetime] = {}

    async def run(self) -> None:
        """Check every minute for due jobs."""
        self.logger.info("CronWorker started.")
        try:
            while True:
                now = datetime.now()
                await self._dispatch_due_jobs(now)
                await asyncio.sleep(self._seconds_until_next_minute(now))
        except asyncio.CancelledError:
            raise

    async def _dispatch_due_jobs(self, now: datetime) -> None:
        """Load cron definitions and dispatch jobs due at the current minute."""
        try:
            jobs = self.context.cron_loader.discover_crons()
        except Exception:
            self.logger.exception("Failed to discover cron jobs")
            return

        self._prune_last_run_cache(jobs)

        now_minute = now.replace(second=0, microsecond=0)
        for job in find_due_jobs(jobs, now):
            if self._already_dispatched(job.id, now_minute):
                continue

            try:
                await self._dispatch_job(job, now_minute)
            except Exception:
                self.logger.exception("Failed to dispatch cron job %s", job.id)
                continue

            self._last_run_minutes[job.id] = now_minute
            if job.one_off:
                self._delete_one_off_job(job)

    async def _dispatch_job(self, job: "CronDef", now_minute: datetime) -> None:
        """Publish one due cron job as an internal dispatch event."""
        event = DispatchEvent(
            session_id=self._session_id(job, now_minute),
            content=job.prompt,
            source=CronEventSource(job.id),
            target_agent_id=job.agent,
            timestamp=now_minute.timestamp(),
        )
        await self.context.eventbus.publish(event)
        self.logger.info(
            "Dispatched cron job %s to agent %s",
            job.id,
            job.agent,
        )

    def _delete_one_off_job(self, job: "CronDef") -> None:
        """Remove a one-off cron definition after it has been dispatched."""
        cron_dir = (self.context.config.crons_path / job.id).resolve()
        crons_path = self.context.config.crons_path.resolve()

        if cron_dir == crons_path or not cron_dir.is_relative_to(crons_path):
            self.logger.error("Refused to delete unsafe cron path %s", cron_dir)
            return

        try:
            shutil.rmtree(cron_dir)
            self.logger.info("Deleted one-off cron job %s", job.id)
        except FileNotFoundError:
            return
        except Exception:
            self.logger.exception("Failed to delete one-off cron job %s", job.id)

    def _already_dispatched(self, job_id: str, now_minute: datetime) -> bool:
        """Return whether a job already ran during this minute."""
        return self._last_run_minutes.get(job_id) == now_minute

    def _prune_last_run_cache(self, jobs: list["CronDef"]) -> None:
        """Drop run markers for cron definitions that no longer exist."""
        active_job_ids = {job.id for job in jobs}
        for job_id in list(self._last_run_minutes):
            if job_id not in active_job_ids:
                del self._last_run_minutes[job_id]

    @staticmethod
    def _session_id(job: "CronDef", now_minute: datetime) -> str:
        """Build a filesystem-safe session id for one cron dispatch."""
        safe_job_id = "".join(
            char if char.isalnum() or char in ("-", "_") else "_"
            for char in job.id
        ).strip("_") or "job"
        run_id = now_minute.strftime("%Y%m%dT%H%M")
        return f"cron-{safe_job_id}-{run_id}"

    @staticmethod
    def _seconds_until_next_minute(now: datetime) -> float:
        """Return sleep seconds until the next minute boundary."""
        return max(1.0, 60.0 - now.second - (now.microsecond / 1_000_000))
