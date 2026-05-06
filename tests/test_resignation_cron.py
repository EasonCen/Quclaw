"""Tests for the resignation scenario cron definition."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from core.cron_loader import CronLoader
from runtime.events import DispatchEvent
from server.workers.cron import CronWorker, find_due_jobs


WORKSPACE = Path(__file__).resolve().parents[1] / "default_workspace"


class ResignationCronDefinitionTest(TestCase):
    def test_loader_discovers_resignation_monitor(self):
        config = SimpleNamespace(crons_path=WORKSPACE / "crons")
        cron_loader = CronLoader(config)

        jobs = {job.id: job for job in cron_loader.discover_crons()}
        job = jobs["resignation-monitor"]

        self.assertEqual(job.agent, "admin")
        self.assertEqual(job.schedule, "*/1 * * * *")
        self.assertIn("scan_timeouts", job.prompt)

    def test_find_due_jobs_matches_every_minute(self):
        config = SimpleNamespace(crons_path=WORKSPACE / "crons")
        job = CronLoader(config).load("resignation-monitor")

        due = find_due_jobs([job], datetime(2026, 5, 5, 10, 10, 30))
        next_minute_due = find_due_jobs([job], datetime(2026, 5, 5, 10, 11, 30))

        self.assertEqual([item.id for item in due], ["resignation-monitor"])
        self.assertEqual([item.id for item in next_minute_due], ["resignation-monitor"])


class EventBusStub:
    def __init__(self) -> None:
        self.events: list[DispatchEvent] = []

    async def publish(self, event):
        self.events.append(event)


class ResignationCronWorkerTest(IsolatedAsyncioTestCase):
    async def test_dispatch_job_publishes_dispatch_event(self):
        config = SimpleNamespace(crons_path=WORKSPACE / "crons")
        job = CronLoader(config).load("resignation-monitor")
        eventbus = EventBusStub()
        context = SimpleNamespace(config=config, eventbus=eventbus)
        worker = CronWorker(context)
        now_minute = datetime(2026, 5, 5, 10, 10)

        await worker._dispatch_job(job, now_minute)

        self.assertEqual(len(eventbus.events), 1)
        event = eventbus.events[0]
        self.assertEqual(event.session_id, "cron-resignation-monitor-20260505T1010")
        self.assertEqual(event.target_agent_id, "admin")
        self.assertEqual(str(event.source), "cron:resignation-monitor")
        self.assertEqual(event.content, job.prompt)
