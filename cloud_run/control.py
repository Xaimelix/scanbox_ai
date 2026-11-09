"""Cloud Run v2 REST client. No cloud credentials are needed at import time."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


class CloudError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    project: str
    region: str
    bucket: str
    worker_image: str
    worker_sa: str
    engine: str = "mock"

    @classmethod
    def from_env(cls):
        return cls(*(os.environ[k] for k in (
            "GOOGLE_CLOUD_PROJECT", "CLOUD_RUN_REGION", "SCANBOX_BUCKET",
            "SCANBOX_WORKER_IMAGE", "SCANBOX_WORKER_SA")),
            engine=os.environ.get("SCANBOX_ENGINE", "mock"))

    @property
    def parent(self):
        return f"projects/{self.project}/locations/{self.region}"


class RunClient:
    def __init__(self, settings: Settings, session=None):
        self.cfg = settings
        self.session = session

    def request(self, method, path, **kwargs):
        if self.session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=[
                "https://www.googleapis.com/auth/cloud-platform"])
            self.session = AuthorizedSession(credentials)
        response = self.session.request(
            method, f"https://run.googleapis.com/v2/{path}", timeout=30, **kwargs)
        if response.status_code >= 400:
            # Do not forward upstream response bodies (IAM details) to clients.
            raise CloudError(response.status_code, f"Cloud Run returned {response.status_code}")
        return response.json() if response.content else {}

    def name(self, job_id):
        return f"{self.cfg.parent}/jobs/sb-{job_id}"

    def specification(self, job_id, extension, generation, timeout):
        cfg = self.cfg
        if cfg.engine not in {"mock", "hunyuan-shape"}:
            raise ValueError("SCANBOX_ENGINE must be mock or hunyuan-shape")
        env = {
            "SCANBOX_BUCKET": cfg.bucket, "SCANBOX_JOB_ID": job_id,
            "SCANBOX_EXTENSION": extension, "SCANBOX_INPUT_GENERATION": str(generation),
            "SCANBOX_ENGINE": cfg.engine,
        }
        task = {
            "serviceAccount": cfg.worker_sa, "timeout": f"{timeout}s", "maxRetries": 0,
            "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
            "gpuZonalRedundancyDisabled": True,
            "nodeSelector": {"accelerator": "nvidia-l4"},
            "containers": [{
                "image": cfg.worker_image,
                "command": ["python", "-m", "cloud_run.worker"],
                "env": [{"name": key, "value": value} for key, value in env.items()],
                "resources": {"limits": {
                    "cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"}},
            }],
        }
        body = {
            "labels": {"app": "scanbox", "managed-by": "scanbox-api"},
            # Creation and initial execution are a single API operation. A retry
            # uses the same job ID; it never issues a second jobs.run request.
            "startExecutionToken": "initial",
            "template": {"taskCount": 1, "parallelism": 1, "template": task},
        }
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        body["annotations"] = {"scanbox.ai/request-sha256": fingerprint}
        return body

    def create(self, job_id, extension, generation, timeout):
        body = self.specification(job_id, extension, generation, timeout)
        try:
            operation = self.request("POST", f"{self.cfg.parent}/jobs",
                                     params={"jobId": f"sb-{job_id}"}, json=body)
            return {"job_id": job_id, "operation": operation, "reused": False}
        except CloudError as exc:
            if exc.status != 409:
                raise
        existing = self.get(job_id)
        if existing.get("annotations", {}).get("scanbox.ai/request-sha256") != body["annotations"]["scanbox.ai/request-sha256"]:
            raise CloudError(409, "job_id already exists with different parameters")
        return {"job_id": job_id, "job": existing, "reused": True}

    def get(self, job_id):
        job = self.request("GET", self.name(job_id))
        if job.get("labels", {}).get("managed-by") != "scanbox-api":
            raise CloudError(404, "Job not managed by ScanBox")
        return job

    def execution(self, job_id):
        job = self.get(job_id)
        name = job.get("latestCreatedExecution", {}).get("name")
        if not name:
            return None
        if not name.startswith(self.name(job_id) + "/executions/"):
            raise CloudError(502, "Unexpected execution resource")
        return self.request("GET", name)

    def cancel(self, job_id):
        execution = self.execution(job_id)
        if not execution:
            raise CloudError(409, "Execution has not started; poll status before cancelling")
        if execution.get("completionTime"):
            return {"already_finished": True}
        return self.request("POST", execution["name"] + ":cancel", json={})

    def delete(self, job_id):
        execution = self.execution(job_id)
        job = self.get(job_id)
        # Do not race a not-yet-started execution or asynchronous cancellation.
        failed_before_start = (not execution and
                               job.get("terminalCondition", {}).get("state") == "CONDITION_FAILED")
        if job.get("reconciling") or (not failed_before_start and
                                      (not execution or not execution.get("completionTime"))):
            raise CloudError(409, "Wait for execution completion before deleting")
        return self.request("DELETE", self.name(job_id), params={"etag": job["etag"]})
