"""ScanBox AI — фоновый воркер: очередь джоб → эфемерный GPU-сервер → GLB.

Берёт задачу из очереди и гоняет через `scripts.orchestrator.run_job()`.
В mock-режиме (по умолчанию) — всё локально, без денег.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from scripts.orchestrator import Config, run_job

log = logging.getLogger("scanbox.worker")

JobStatus = Literal[
    "queued",
    "provisioning",
    "generating",
    "done",
    "failed",
]

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"


class JobRecord:
    """Внутреннее состояние одной джобы."""

    __slots__ = (
        "job_id",
        "status",
        "photo_path",
        "out_dir",
        "glb_path",
        "error",
        "created_at",
        "finished_at",
        "_event",
    )

    def __init__(self, job_id: str, photo_path: Path, out_dir: Path):
        self.job_id = job_id
        self.status: JobStatus = "queued"
        self.photo_path = photo_path
        self.out_dir = out_dir
        self.glb_path: Path | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self._event = threading.Event()

    def wait(self, timeout: float | None = None) -> None:
        self._event.wait(timeout)

    def set_done(self) -> None:
        self.finished_at = time.time()
        self._event.set()

    def to_dict(self) -> dict:
        d = {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "has_glb": self.glb_path is not None and self.glb_path.exists(),
        }
        if self.error:
            d["error"] = self.error
        return d


class JobManager:
    """Фоновая очередь: submit → воркер обрабатывает → результат готов."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.from_env()
        self._jobs: dict[str, JobRecord] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_loop, daemon=True, name="scanbox-worker")
        self._worker.start()
        log.info("воркер запущен (mock=%s)", self.cfg.mock)

    def submit(self, photo_path: Path, job_id: str | None = None) -> str:
        job_id = job_id or uuid.uuid4().hex[:12]
        out_dir = OUTPUTS_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        rec = JobRecord(job_id, photo_path, out_dir)
        with self._lock:
            self._jobs[job_id] = rec
        self._queue.put(job_id)
        log.info("джоба %s: поставлена в очередь", job_id)
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            rec = self.get(job_id)
            if rec is None:
                continue
            try:
                rec.status = "provisioning"
                glb = rec.out_dir / "model.glb"

                run_job(
                    self.cfg,
                    photo=rec.photo_path,
                    out=glb,
                    job_id=job_id,
                )

                rec.status = "done"
                rec.glb_path = glb
                log.info("джоба %s: ✅ модель готова", job_id)
            except Exception as exc:
                rec.status = "failed"
                rec.error = str(exc)
                log.exception("джоба %s: ❌ %s", job_id, exc)
            finally:
                rec.set_done()