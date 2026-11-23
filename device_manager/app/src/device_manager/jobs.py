"""Hintergrund-Jobs für lang laufende Aktionen (Firmware, Zertifikat, Gen4-Workflow)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import steps
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[str], None]
JobFn = Callable[[Progress], Awaitable[Any]]


@dataclass
class UserAction:
    """Aufforderung an den Anwender, die der laufende Job anzeigt (z. B. „Gerät jetzt neu starten“).

    Der Job führt die Aktion nie selbst aus; erst der Klick auf den Button ruft `run` auf.
    """

    message: str
    label: str
    run: Callable[[], Awaitable[str]]  # liefert die Meldung für das Job-Protokoll


_current_job: ContextVar[Job | None] = ContextVar("sm_current_job", default=None)


def request_user_action(message: str, label: str, run: Callable[[], Awaitable[str]]) -> bool:
    """Zeigt im laufenden Job eine Aufforderung mit Button. Liefert False, wenn kein Job läuft."""
    job = _current_job.get()
    if job is None:
        return False
    job.action = UserAction(message, label, run)
    log.warning("[Job %s] Anwender-Aktion angefordert: %s", job.id, message)
    return True


def clear_user_action() -> None:
    job = _current_job.get()
    if job is not None:
        job.action = None


@dataclass
class Job:
    id: int
    title: str
    device_key: str | None
    status: str = "running"  # running | ok | error
    messages: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    step: str | None = None  # aktueller (bzw. beim Fehler: fehlgeschlagener) Schritt
    error_detail: str | None = None  # Fehlertyp und Ursachenkette
    action: UserAction | None = None  # offene Aufforderung an den Anwender
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def done(self) -> bool:
        return self.status != "running"

    async def run_action(self) -> str:
        """Vom Anwender bestätigte Aktion ausführen (einmalig)."""
        action, self.action = self.action, None
        if action is None or self.done:
            raise LookupError("Keine offene Aktion für diesen Job")
        log.info("[Job %s] Anwender bestätigt: %s", self.id, action.label)
        message = await action.run()
        self.messages.append(message)
        return message


class JobManager:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def start(self, title: str, device_key: str | None, fn: JobFn) -> Job:
        job = Job(next(self._ids), title, device_key)
        self.jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job, fn))
        return job

    async def _run(self, job: Job, fn: JobFn) -> None:
        ref = f"Job {job.id} '{job.title}'" + (f" [{job.device_key}]" if job.device_key else "")

        def progress(message: str) -> None:
            job.messages.append(message)
            log.info("[%s] %s", ref, message)

        def on_step(label: str) -> None:
            job.step = label

        steps.set_listener(on_step)  # gilt nur in diesem Task (ContextVar)
        _current_job.set(job)
        log.info("[%s] gestartet", ref)
        started = time.monotonic()
        try:
            job.result = await fn(progress)
        except Exception as exc:  # noqa: BLE001 - jeder Fehler wird protokolliert und angezeigt
            elapsed = time.monotonic() - started
            job.status = "error"
            job.action = None
            where = steps.failed_step(exc) or job.step
            job.step = where
            job.error = str(exc) or type(exc).__name__
            job.error_detail = steps.describe_error(exc)
            log.error(
                "[%s] FEHLER nach %.1fs%s: %s",
                ref,
                elapsed,
                f" im Schritt '{where}'" if where else "",
                job.error_detail,
                exc_info=exc,
            )
            prefix = f"Schritt '{where}': " if where else ""
            self._store.log_action(job.device_key, job.title, "error", prefix + job.error_detail)
        else:
            job.status = "ok"
            job.action = None
            job.step = None
            summary = job.messages[-1] if job.messages else "erfolgreich"
            log.info("[%s] erfolgreich nach %.1fs", ref, time.monotonic() - started)
            self._store.log_action(job.device_key, job.title, "ok", summary)

    async def wait(self, job_id: int) -> Job:
        task = self._tasks.get(job_id)
        if task:
            await task
        return self.jobs[job_id]
