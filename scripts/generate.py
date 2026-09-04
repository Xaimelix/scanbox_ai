#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ScanBox AI — джоба на GPU-сервере: фото → GLB.

Вызывается оркестратором (orchestrator.py) на эфемерном сервере.

Режимы:
    --engine mock      валидный пустой GLB — проверка пайплайна без зависимостей;
    --engine hunyuan   реальный Hunyuan3D-2.1 (нужен HUNYUAN3D_HOME на сервере);
    --engine auto      hunyuan, если HUNYUAN3D_HOME есть, иначе mock.

Пример на сервере с золотым образом:
    HUNYUAN3D_HOME=/opt/Hunyuan3D-2.1 python3 generate.py --input photo.jpg --output model.glb
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time


def make_dummy_glb(path: str) -> int:
    """Минимальный валидный GLB (пустая сцена) — хватит, чтобы прогнать весь конвейер."""
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": []}], "nodes": []}
    payload = json.dumps(doc, separators=(",", ":")).encode()
    payload += b" " * ((4 - len(payload) % 4) % 4)  # выравнивание до 4 байт
    glb = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(payload))
    glb += struct.pack("<I4s", len(payload), b"JSON") + payload
    with open(path, "wb") as fh:
        fh.write(glb)
    return len(glb)


def find_glb(root: str) -> str:
    """Hunyuan кладёт результат не всегда в предсказуемое место — ищем свежайший."""
    hits = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith((".glb", ".gltf")):
                hits.append(os.path.join(dirpath, name))
    if not hits:
        raise RuntimeError("Hunyuan не отдал ни одного GLB/GLTF")
    return max(hits, key=os.path.getmtime)


def run_hunyuan(src: str, dst: str) -> None:
    home = os.environ["HUNYUAN3D_HOME"]
    out_dir = os.path.dirname(os.path.abspath(dst))
    # Точная команда зависит от сборки Hunyuan3D-2.1 — поправь под свою:
    cmd = [sys.executable, os.path.join(home, "run.py"), "--input_image", src, "--output_dir", out_dir]
    subprocess.run(cmd, check=True)
    shutil.copy(find_glb(out_dir), dst)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--engine", choices=("auto", "mock", "hunyuan"), default="auto")
    args = ap.parse_args()

    t0 = time.time()
    engine = args.engine
    has_hunyuan = os.path.isdir(os.environ.get("HUNYUAN3D_HOME", ""))
    if engine == "auto":
        engine = "hunyuan" if has_hunyuan else "mock"
    if engine == "hunyuan" and not has_hunyuan:
        print("HUNYUAN3D_HOME не найден — использую mock", file=sys.stderr)
        engine = "mock"

    if engine == "hunyuan":
        run_hunyuan(args.input, args.output)
    else:
        size = make_dummy_glb(args.output)
        print(f"[mock] пустой GLB ({size} байт)")

    print(f"done in {time.time() - t0:.1f}s -> {args.output}")


if __name__ == "__main__":
    main()