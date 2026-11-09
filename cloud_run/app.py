"""Private IAM-authenticated control API; deploy separately from the demo API."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud_run.control import CloudError, RunClient, Settings

app = FastAPI(title="ScanBox Cloud Run GPU", version="0.1.0")


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    extension: Literal["jpg", "jpeg", "png", "webp"] = "png"
    input_generation: int = Field(gt=0)
    timeout_seconds: int = Field(default=1800, ge=60, le=3600)


@lru_cache
def get_client():
    return RunClient(Settings.from_env())


@app.exception_handler(CloudError)
async def cloud_error(_, exc):
    status = exc.status if exc.status in {404, 409, 429} else 502
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/v1/jobs", status_code=202)
def create_job(body: CreateJob, client=Depends(get_client)):
    return client.create(body.job_id.hex, body.extension,
                         body.input_generation, body.timeout_seconds)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: UUID, client=Depends(get_client)):
    job = client.get(job_id.hex)
    execution = client.execution(job_id.hex)
    state = "provisioning"
    if execution:
        state = "running"
        if execution.get("completionTime"):
            if execution.get("succeededCount", 0) == 1:
                state = "succeeded"
            elif execution.get("cancelledCount", 0):
                state = "cancelled"
            else:
                state = "failed"
    elif job.get("terminalCondition", {}).get("state") == "CONDITION_FAILED":
        state = "failed"
    return {"job_id": job_id.hex, "status": state, "job": job, "execution": execution,
            "result_uri": (f"gs://{client.cfg.bucket}/outputs/{job_id.hex}/model.glb"
                           if state == "succeeded" else None)}


@app.post("/v1/jobs/{job_id}:cancel", status_code=202)
def cancel_job(job_id: UUID, client=Depends(get_client)):
    return client.cancel(job_id.hex)


@app.delete("/v1/jobs/{job_id}", status_code=202)
def delete_job(job_id: UUID, client=Depends(get_client)):
    return client.delete(job_id.hex)
