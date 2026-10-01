from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path, PurePosixPath


def installation_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        path.is_absolute() or "\\" in relative or ":" in relative
        or any(part in {".", "..", ""} for part in relative.split("/"))
    ):
        raise ValueError("Unsafe installation path")
    result = root.joinpath(*path.parts).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Installation file escapes its root")
    return result


def verify_files(root: Path, manifest: dict) -> None:
    def verify(item):
        path = installation_path(root, item["path"])
        if path.stat().st_size != item["size"]:
            raise ValueError(f"Incorrect installed file size: {item['path']}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"Installed file checksum mismatch: {item['path']}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(verify, manifest["files"]))


def check_dependencies(root: Path, manifest: dict) -> dict:
    from packaging.requirements import Requirement

    if sys.version_info[:2] != (3, 11) or site_enabled():
        raise RuntimeError("The bundled isolated Python 3.11 runtime is required")
    for name, version in manifest["packages"].items():
        if metadata.version(name) != version:
            raise RuntimeError(f"Incorrect installed package version: {name}")
        for specification in metadata.requires(name) or ():
            dependency = Requirement(specification)
            if dependency.marker and not dependency.marker.evaluate({"extra": ""}):
                continue
            actual = metadata.version(dependency.name)
            if dependency.specifier and not dependency.specifier.contains(actual):
                raise RuntimeError(f"Incompatible dependency: {name} -> {dependency.name}")
    modules = (
        "torch", "torchaudio", "transformers", "accelerate", "numpy", "soundfile",
        "pydub", "razdel", "ebooklib.epub", "bs4", "pypdf", "docx",
        "charset_normalizer", "lxml.etree", "filelock", "PIL.Image", "truststore",
        "stable_whisper", "whisper", "miniaudio", "tkinter",
    )
    for name in modules:
        importlib.import_module(name)
    sys.path.insert(0, str(root / "app/vendor/VoiceStudio"))
    importlib.import_module("omnivoice")
    import tkinter as tk

    import miniaudio
    import torch
    import whisper

    from aibook.voice_studio_config import model_ready

    window = tk.Tk()
    window.withdraw()
    window.update_idletasks()
    window.destroy()
    if torch.zeros(2).sum().item() != 0 or not model_ready():
        raise RuntimeError("Native Torch or OmniVoice weights are unavailable")
    checkpoint = root / "data/models/Whisper/tiny.pt"
    if not checkpoint.is_file():
        raise RuntimeError("Offline Whisper tiny checkpoint is missing")
    with checkpoint.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != whisper._MODELS["tiny"].split("/")[-2]:
        raise RuntimeError("Offline Whisper checkpoint checksum is incorrect")
    versions = {}
    for name in ("ffmpeg", "ffprobe"):
        path = root / "runtime/ffmpeg" / (name + ".exe")
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True,
            check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        versions[name] = result.stdout.splitlines()[0]
    return {
        "python": sys.version.split()[0], "packages_checked": len(manifest["packages"]),
        "imports_checked": len(modules) + 1, "tk": tk.TkVersion,
        "torch": torch.__version__, "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "model_ready": True, "alignment_model_ready": True,
        "playback_devices": len(miniaudio.Devices().get_playbacks()), **versions,
    }


def site_enabled() -> bool:
    import site

    return bool(site.ENABLE_USER_SITE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a clean Windows installation")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if Path(sys.executable).resolve() != root / "runtime/python/python.exe":
        raise RuntimeError("Audit must use this installation's bundled interpreter")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["AIBOOK_VOICESTUDIO_MODEL_DIR"] = str(root / "data/models/OmniVoice")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    verify_files(root, manifest)
    report = {"version": manifest["version"], **check_dependencies(root, manifest)}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(args.report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
