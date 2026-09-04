#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScanBox AI — FastAPI-сервис.

Запуск:
    cd scanbox_ai
    pip install fastapi uvicorn python-multipart

    # mock-режим (без GPU, локально):
    SELECTEL_MOCK=1 uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload

    # реальный режим (нужны креды Selectel):
    uvicorn api.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.worker import JobManager, OUTPUTS_DIR
from scripts.orchestrator import Config

log = logging.getLogger("scanbox.api")

# ── конфиг ──────────────────────────────────────────────────────────────

cfg = Config.from_env()
cfg.mock = os.environ.get("SELECTEL_MOCK", "1") in ("1", "true", "yes")

MAX_UPLOAD_MB = 16
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# ── приложение ──────────────────────────────────────────────────────────

app = FastAPI(
    title="ScanBox AI",
    version="0.1.0",
    description="Фото → 3D через эфемерный GPU",
)

API_DIR = Path(__file__).resolve().parent

TEMPLATES_DIR = API_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# статика (CSS, JS-зависимости)
STATIC_DIR = Path("api/static").resolve()
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# demo-пресеты (GLB + фото)
DEMO_DIR = Path("demo").resolve()
if DEMO_DIR.exists():
    app.mount("/demo", StaticFiles(directory=str(DEMO_DIR)), name="demo")

# выходные GLB — монтируем /outputs/{job_id}/model.glb
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

# менеджер джоб
manager = JobManager(cfg)


# ── ручки ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница: загрузка фото + 3D-просмотрщик."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "mock": str(cfg.mock).lower()},
    )


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Принять фото, поставить в очередь, вернуть job_id."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext}. Нужно JPG, PNG или WebP.")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            400,
            f"Файл слишком большой: {len(contents) / 1024 / 1024:.1f} МБ (макс {MAX_UPLOAD_MB} МБ)",
        )

    # сохраняем фото ДО постановки в очередь (воркер стартует сразу)
    job_id = uuid.uuid4().hex[:12]
    photo_dir = OUTPUTS_DIR / job_id
    photo_dir.mkdir(parents=True, exist_ok=True)
    photo_path = photo_dir / f"photo{ext}"
    photo_path.write_bytes(contents)

    manager.submit(photo_path, job_id=job_id)

    return {"job_id": job_id, "status": "queued", "mock": cfg.mock}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    """Статус джобы: queued → provisioning → generating → done / failed."""
    rec = manager.get(job_id)
    if rec is None:
        raise HTTPException(404, f"Джоба {job_id} не найдена")
    return rec.to_dict()


@app.get("/api/result/{job_id}")
async def result(job_id: str):
    """Скачать готовый GLB."""
    rec = manager.get(job_id)
    if rec is None:
        raise HTTPException(404, f"Джоба {job_id} не найдена")
    if rec.status != "done":
        raise HTTPException(425, f"Модель ещё не готова (статус: {rec.status})")
    if not rec.glb_path or not rec.glb_path.exists():
        raise HTTPException(500, "Файл модели не найден на сервере")
    return FileResponse(
        path=str(rec.glb_path),
        media_type="model/gltf-binary",
        filename=f"scanbox_{job_id}.glb",
    )


@app.get("/api/presets")
async def presets():
    """Список готовых пресетов (GLB) для мгновенной демо."""
    if not DEMO_DIR.exists():
        return {"presets": []}
    items = []
    for f in sorted(DEMO_DIR.iterdir()):
        if f.suffix == ".glb":
            stem = f.stem.replace("scanbox_", "").replace("_", " ").strip()
            items.append({
                "name": stem or f.stem,
                "glb": f"/demo/{f.name}",
                "photo": f"/demo/{f.stem}.jpg"
                if (DEMO_DIR / f"{f.stem}.jpg").exists()
                else None,
            })
    return {"presets": items}


# ── запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("🚀 mock=%s | http://localhost:8000", cfg.mock)
    uvicorn.run("api.app:app", host="0.0.0.0", port=8000, reload=True)