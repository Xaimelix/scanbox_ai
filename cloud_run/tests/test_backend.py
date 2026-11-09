import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cloud_run.app import app, get_client
from cloud_run.control import CloudError, RunClient, Settings
from cloud_run.worker import MAX_INPUT_BYTES, generate, run, validate_glb

JOB = "a" * 32
CFG = Settings("test-project", "europe-west4", "test-bucket",
               "europe-west4-docker.pkg.dev/test-project/scanbox/worker@sha256:abc",
               "worker@test-project.iam.gserviceaccount.com")


class ControlTests(unittest.TestCase):
    def test_create_then_retry_never_calls_run(self):
        client = RunClient(CFG)
        body = client.specification(JOB, "png", 123, 1800)
        existing = dict(body, name=client.name(JOB))
        client.request = Mock(side_effect=[{"name": "operation"}, CloudError(409, "exists"), existing])
        first = client.create(JOB, "png", 123, 1800)
        second = client.create(JOB, "png", 123, 1800)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertFalse(any(":run" in str(call) for call in client.request.call_args_list))
        self.assertEqual(body["startExecutionToken"], "initial")
        task = body["template"]["template"]
        self.assertEqual(task["maxRetries"], 0)
        self.assertEqual(task["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], "1")

    def test_reused_id_with_different_input_conflicts(self):
        client = RunClient(CFG)
        old = client.specification(JOB, "png", 123, 1800)
        client.request = Mock(side_effect=[CloudError(409, "exists"), old])
        with self.assertRaises(CloudError) as error:
            client.create(JOB, "png", 456, 1800)
        self.assertEqual(error.exception.status, 409)

    def test_foreign_job_cannot_be_cancelled(self):
        client = RunClient(CFG)
        client.request = Mock(return_value={"labels": {}})
        with self.assertRaises(CloudError) as error:
            client.cancel(JOB)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(client.request.call_count, 1)

    def test_deletion_rejects_active_execution(self):
        client = RunClient(CFG)
        client.execution = Mock(return_value={"name": "execution"})
        client.get = Mock(return_value={"etag": "v1"})
        client.request = Mock()
        with self.assertRaises(CloudError):
            client.delete(JOB)
        client.request.assert_not_called()

    def test_terminal_deletion_uses_etag(self):
        client = RunClient(CFG)
        client.execution = Mock(return_value={"completionTime": "now"})
        client.get = Mock(return_value={"etag": "v1"})
        client.request = Mock(return_value={"name": "operation"})
        client.delete(JOB)
        client.request.assert_called_once_with("DELETE", client.name(JOB), params={"etag": "v1"})

    def test_cancel_waits_for_completion_before_deletion(self):
        client = RunClient(CFG)
        client.execution = Mock(return_value={"name": "execution"})
        client.request = Mock(return_value={"name": "cancel-operation"})
        client.cancel(JOB)
        client.request.assert_called_once_with("POST", "execution:cancel", json={})

    def test_delete_failed_provisioning(self):
        client = RunClient(CFG)
        client.execution = Mock(return_value=None)
        client.get = Mock(return_value={"etag": "v1", "terminalCondition": {"state": "CONDITION_FAILED"}})
        client.request = Mock()
        client.delete(JOB)
        client.request.assert_called_once()

    def test_upstream_error_is_sanitized(self):
        response = Mock(status_code=403, text="private IAM details")
        session = Mock()
        session.request.return_value = response
        with self.assertRaises(CloudError) as error:
            RunClient(CFG, session).get(JOB)
        self.assertNotIn("private IAM", str(error.exception))


async def request(method, path, data=None):
    """Small ASGI harness; avoids installing an HTTP client for offline tests."""
    messages = []
    body = json.dumps(data).encode() if data is not None else b""
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    # Restricted executors may not wake the selector for cross-thread callbacks.
    # Keep a bounded selector tick in this test harness only.
    async def tick():
        while True:
            await asyncio.sleep(0.01)
    heartbeat = asyncio.create_task(tick())
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
               "headers": [(b"content-type", b"application/json")], "scheme": "http",
               "server": ("test", 80), "client": ("test", 1), "root_path": ""}, receive, send)
    heartbeat.cancel()
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(payload)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(cfg=CFG)
        app.dependency_overrides[get_client] = lambda: self.client

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_accepts_create_without_waiting_for_gpu(self):
        self.client.create.return_value = {"job_id": JOB, "operation": {"name": "op"}}
        status, _ = asyncio.run(request("POST", "/v1/jobs", {"job_id": JOB, "input_generation": 123}))
        self.assertEqual(status, 202)
        self.client.create.assert_called_once_with(JOB, "png", 123, 1800)

    def test_rejects_timeout_and_arbitrary_container(self):
        for fields in ({"timeout_seconds": 3601}, {"image": "attacker/image"}, {"input_generation": 0}):
            with self.subTest(fields=fields):
                status, _ = asyncio.run(request("POST", "/v1/jobs", {
                    "job_id": JOB, "input_generation": 123, **fields}))
                self.assertEqual(status, 422)
        self.client.create.assert_not_called()

    def test_missing_job_is_404(self):
        self.client.get.side_effect = CloudError(404, "missing")
        status, _ = asyncio.run(request("GET", f"/v1/jobs/{JOB}"))
        self.assertEqual(status, 404)

    def test_status_uses_execution_not_object_presence(self):
        self.client.get.return_value = {}
        self.client.execution.return_value = {"completionTime": "now", "failedCount": 1}
        status, data = asyncio.run(request("GET", f"/v1/jobs/{JOB}"))
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "failed")
        self.assertIsNone(data["result_uri"])

    def test_success_returns_durable_uri(self):
        self.client.get.return_value = {}
        self.client.execution.return_value = {"completionTime": "now", "succeededCount": 1}
        _, data = asyncio.run(request("GET", f"/v1/jobs/{JOB}"))
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["result_uri"], f"gs://test-bucket/outputs/{JOB}/model.glb")


class MemoryBlob:
    def __init__(self, payload=b"photo"):
        self.payload = payload
        self.size = len(payload)
        self.metadata = None
        self.generation = None

    def reload(self):
        pass

    def download_to_filename(self, filename, if_generation_match):
        if if_generation_match != self.generation:
            raise ValueError("Generation changed")
        Path(filename).write_bytes(self.payload)

    def upload_from_filename(self, filename, content_type, if_generation_match):
        if if_generation_match != 0 or self.payload:
            raise ValueError("Output already exists")
        self.payload = Path(filename).read_bytes()


class WorkerTests(unittest.TestCase):
    def test_mock_roundtrip_persists_valid_glb_and_prevents_overwrite(self):
        source, destination = MemoryBlob(), MemoryBlob(b"")
        source.generation = 123
        bucket = Mock()
        bucket.blob.side_effect = lambda name, **_: source if name.startswith("inputs/") else destination
        run(bucket, JOB, "png", 123, "mock")
        self.assertEqual(destination.payload[:4], b"glTF")
        self.assertEqual(destination.metadata["engine"], "mock")
        with self.assertRaisesRegex(ValueError, "Output already exists"):
            run(bucket, JOB, "png", 123, "mock")

    def test_oversize_input_rejected_before_download(self):
        source = Mock(size=MAX_INPUT_BYTES + 1)
        bucket = Mock()
        bucket.blob.return_value = source
        with self.assertRaises(ValueError):
            run(bucket, JOB, "png", 123, "mock")
        source.download_to_filename.assert_not_called()

    def test_invalid_glb_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.glb"
            path.write_bytes(b"not a glb")
            with self.assertRaises(ValueError):
                validate_glb(path)

    def test_engine_error_does_not_publish_output(self):
        source = MemoryBlob()
        source.generation = 123
        bucket = Mock()
        bucket.blob.return_value = source
        with patch("cloud_run.worker.generate", side_effect=RuntimeError("CUDA OOM")):
            with self.assertRaisesRegex(RuntimeError, "CUDA OOM"):
                run(bucket, JOB, "png", 123, "hunyuan-shape")
        self.assertEqual(bucket.blob.call_count, 1)

    def test_unknown_engine_fails_instead_of_falling_back_to_mock(self):
        with self.assertRaisesRegex(ValueError, "Unsupported engine"):
            generate(Path("input"), Path("output"), "auto")


if __name__ == "__main__":
    unittest.main()
