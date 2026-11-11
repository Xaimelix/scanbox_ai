import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.orchestrator import Config, container_args, container_command

spec = importlib.util.spec_from_file_location("scanbox_gpu_worker", Path(__file__).resolve().parents[2] / "docker/gpu_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class DockerWorkerTests(unittest.TestCase):
    def test_remote_command_is_offline_and_passes_no_cloud_credentials(self):
        cfg = Config("account", "user", "secret", worker_image="registry/worker@sha256:abc",
                     model_dir="/models/path with spaces")
        args = shlex.split(container_command(cfg, "/tmp/job with spaces"))
        self.assertEqual(args[args.index("--pull") + 1], "never")
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("/models/path with spaces:/models:ro", args)
        self.assertIn("/tmp/job with spaces:/work", args)
        self.assertNotIn("secret", args)
        self.assertNotIn("--env", args)
        self.assertEqual(args[-5:], ["infer", "--input", "/work/photo.png", "--output", "/work/model.glb"])

    def test_missing_image_or_relative_model_directory_fails_before_ssh(self):
        with self.assertRaises(ValueError):
            container_args(Config("", "", ""))
        with self.assertRaises(ValueError):
            container_command(Config("", "", "", worker_image="worker", model_dir="relative"), "/tmp/job")

    def test_gpu_doctor_fails_if_cuda_is_unavailable(self):
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict(sys.modules, {"torch": torch}):
            with self.assertRaisesRegex(RuntimeError, "CUDA"):
                worker.doctor()

    def test_model_download_resolves_revision_before_snapshot(self):
        revision = "a" * 40
        snapshot = Mock()
        api = Mock(return_value=SimpleNamespace(model_info=lambda *args, **kwargs: SimpleNamespace(sha=revision)))
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SCANBOX_MODEL_PATH": directory}), \
                    patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(HfApi=api, snapshot_download=snapshot)}):
                worker.prepare_models("main")
            self.assertEqual(snapshot.call_args.kwargs["revision"], revision)
            self.assertEqual(json.loads((Path(directory) / "scanbox-revision.json").read_text())["revision"], revision)

    def test_explicit_mock_container_entrypoint_writes_glb(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "photo.png", Path(directory) / "model.glb"
            source.write_bytes(b"test input")
            worker.infer(source, output, "mock")
            self.assertEqual(output.read_bytes()[:4], b"glTF")

    def test_failed_engine_does_not_produce_successful_glb(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "photo.png", Path(directory) / "model.glb"
            source.write_bytes(b"test input")
            with patch("cloud_run.worker.generate", side_effect=RuntimeError("CUDA OOM")):
                with self.assertRaisesRegex(RuntimeError, "CUDA OOM"):
                    worker.infer(source, output, "hunyuan-shape")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
