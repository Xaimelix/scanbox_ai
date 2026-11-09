"""One task, one immutable input generation, one durable output, then exit."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import struct
import tempfile
from uuid import UUID

from scripts.generate import make_dummy_glb

MAX_INPUT_BYTES = 16 * 1024 * 1024
log = logging.getLogger("scanbox.cloud_run.worker")


def generate(source: Path, destination: Path, engine: str):
    if engine == "mock":
        make_dummy_glb(str(destination))
        return
    if engine != "hunyuan-shape":
        raise ValueError(f"Unsupported engine: {engine}")
    import torch
    from PIL import Image
    from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for hunyuan-shape")
    model = os.environ.get("SCANBOX_MODEL_PATH", "/opt/models/Hunyuan3D-2.1")
    if not Path(model).is_dir():
        raise RuntimeError("Bake Hunyuan shape weights into SCANBOX_MODEL_PATH")
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        model, subfolder="hunyuan3d-dit-v2-1", use_safetensors=True)
    with Image.open(source) as image:
        # Prefer a segmented RGBA input; this adapter does not remove background.
        mesh = pipeline(image=image.convert("RGBA"), num_inference_steps=30,
                        octree_resolution=256)[0]
    if mesh is None:
        raise RuntimeError("Shape pipeline returned no mesh")
    mesh.export(str(destination), file_type="glb")


def validate_glb(path: Path):
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12:
        raise ValueError("Truncated GLB")
    magic, version, size = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or size != path.stat().st_size:
        raise ValueError("Invalid GLB header")


def run(bucket, job_id: str, extension: str, generation: int, engine: str):
    job_id = UUID(job_id).hex
    if extension not in {"png", "jpg", "jpeg", "webp"} or generation <= 0:
        raise ValueError("Invalid input reference")
    source_blob = bucket.blob(f"inputs/{job_id}/photo.{extension}", generation=generation)
    source_blob.reload()
    if not source_blob.size or source_blob.size > MAX_INPUT_BYTES:
        raise ValueError("Input must contain 1 byte to 16 MiB")
    with tempfile.TemporaryDirectory(prefix="scanbox-") as directory:
        source = Path(directory) / f"photo.{extension}"
        destination = Path(directory) / "model.glb"
        source_blob.download_to_filename(str(source), if_generation_match=generation)
        generate(source, destination, engine)
        validate_glb(destination)
        result = bucket.blob(f"outputs/{job_id}/model.glb")
        result.metadata = {"engine": engine, "input-generation": str(generation)}
        result.upload_from_filename(str(destination), content_type="model/gltf-binary",
                                    if_generation_match=0)
    log.info("Completed job=%s engine=%s", job_id, engine)


def main():
    from google.cloud import storage
    logging.basicConfig(level=logging.INFO)
    run(storage.Client().bucket(os.environ["SCANBOX_BUCKET"]),
        os.environ["SCANBOX_JOB_ID"], os.environ["SCANBOX_EXTENSION"],
        int(os.environ["SCANBOX_INPUT_GENERATION"]), os.environ["SCANBOX_ENGINE"])


if __name__ == "__main__":
    main()
