from __future__ import annotations

import hashlib
import json
import os
import urllib.request

from aibook.voice_studio_config import (
    MODEL_FILES,
    MODEL_REPO,
    MODEL_REVISION,
    configure_environment,
    model_ready,
    studio_model_dir,
)


def main() -> None:
    configure_environment()
    os.environ.pop("HF_HUB_OFFLINE", None)
    import truststore

    truststore.inject_into_ssl()
    import whisper

    checkpoint_dir = studio_model_dir().parent / "Whisper"
    print("Preparing offline Whisper tiny word alignment.", flush=True)
    whisper.load_model("tiny", device="cpu", download_root=str(checkpoint_dir))
    from filelock import FileLock
    from huggingface_hub import snapshot_download

    directory = studio_model_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / ".install.lock"), timeout=3600):
        if model_ready():
            print("OmniVoice model is already installed.", flush=True)
            return
        print("Downloading pinned OmniVoice weights (about 3.05 GiB).", flush=True)
        snapshot_download(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            local_dir=directory,
            token=False,
            max_workers=4,
        )
        request = urllib.request.Request(
            f"https://huggingface.co/api/models/{MODEL_REPO}/revision/{MODEL_REVISION}?blobs=true",
            headers={"User-Agent": "AIBook-VoiceStudio"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            metadata = json.load(response)
        hashes = {}
        for item in metadata["siblings"]:
            name = item["rfilename"]
            if name not in MODEL_FILES:
                continue
            path = directory / name
            if path.stat().st_size != MODEL_FILES[name]:
                raise RuntimeError(f"Incomplete model file: {name}")
            if item.get("lfs"):
                print(f"Verifying SHA-256: {name}", flush=True)
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != item["lfs"]["sha256"]:
                    raise RuntimeError(f"Model checksum mismatch: {name}")
                hashes[name] = digest
        manifest = directory / "AIBOOK_SHA256.json"
        manifest.write_text(json.dumps(hashes, indent=2), encoding="ascii")
        marker = directory / "AIBOOK_REVISION.txt"
        marker.write_text(MODEL_REVISION + "\n", encoding="ascii")
        if not model_ready():
            raise RuntimeError("Model verification failed")
        print(f"Verified model: {directory}", flush=True)


if __name__ == "__main__":
    main()
