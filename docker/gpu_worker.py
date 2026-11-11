"""GPU container commands. No cloud credentials or VM provisioning here."""
import argparse
import json
import os
from pathlib import Path
import re


def doctor():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA недоступна: проверьте NVIDIA driver, Container Toolkit и --gpus")
    # Exercise CUDA, rather than merely finding nvidia-smi.
    value = (torch.ones((32, 32), device="cuda") @ torch.ones((32, 32), device="cuda"))[0, 0].item()
    if value != 32:
        raise RuntimeError("Проверка вычисления CUDA не пройдена")
    print(json.dumps({"cuda": "ok", "gpu": torch.cuda.get_device_name(0),
                      "vram_bytes": torch.cuda.get_device_properties(0).total_memory,
                      "torch": torch.__version__}, ensure_ascii=False))


def prepare_models(revision):
    from huggingface_hub import HfApi, snapshot_download
    revision = HfApi().model_info("tencent/Hunyuan3D-2.1", revision=revision).sha
    if not revision or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Не удалось получить commit SHA модели")
    destination = Path(os.environ.get("SCANBOX_MODEL_PATH", "/models/Hunyuan3D-2.1"))
    snapshot_download(repo_id="tencent/Hunyuan3D-2.1", revision=revision,
                      local_dir=str(destination), allow_patterns=["hunyuan3d-dit-v2-1/*"])
    (destination / "scanbox-revision.json").write_text(json.dumps({"revision": revision}) + "\n")
    print(json.dumps({"model_path": str(destination), "revision": revision}))


def infer(source, output, engine):
    from cloud_run.worker import generate, validate_glb
    if not source.is_file():
        raise ValueError("Входной файл не найден")
    if not 0 < source.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError("Размер входа должен быть от 1 байта до 16 MiB")
    output.parent.mkdir(parents=True, exist_ok=True)
    generate(source, output, engine)
    validate_glb(output)
    print(json.dumps({"output": str(output), "engine": engine}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    prepare = commands.add_parser("prepare-models")
    prepare.add_argument("--revision", default="main")
    job = commands.add_parser("infer")
    job.add_argument("--input", type=Path, required=True)
    job.add_argument("--output", type=Path, required=True)
    job.add_argument("--engine", choices=("hunyuan-shape", "mock"), default="hunyuan-shape")
    args = parser.parse_args()
    if args.command == "doctor":
        doctor()
    elif args.command == "prepare-models":
        prepare_models(args.revision)
    else:
        infer(args.input, args.output, args.engine)


if __name__ == "__main__":
    main()
