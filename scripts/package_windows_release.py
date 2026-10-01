from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from filelock import FileLock

GIB = 1024**3
MAX_ASSET_BYTES = 1500 * 1024**2
ROOT_FILES = {
    "AIBook VoiceStudio.exe", "AIBook.ico", "README.md", "NOTICE.txt",
    "BUILD_INFO.txt", "RESOLVED_PACKAGES.txt",
}
ROOT_DIRECTORIES = {"app", "runtime", "source", "examples", "licenses"}


def release_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or path.is_symlink()
            or any(part in {"__pycache__", ".cache", ".git"} for part in relative.parts)
            or path.suffix in {".pyc", ".pyo", ".lock", ".tmp"}
        ):
            continue
        if (
            relative.as_posix() in ROOT_FILES
            or relative.parts[0] in ROOT_DIRECTORIES
            or relative.parts[:2] == ("data", "models")
        ):
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record_file(root: Path, path: Path) -> dict:
    before = path.stat()
    digest = file_digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"Release source changed during packaging: {path.name}")
    return {
        "path": path.relative_to(root).as_posix(),
        "size": before.st_size,
        "sha256": digest,
    }


def make_archive(root: Path, output: Path, number: int, paths: list[Path]) -> dict:
    path = output / f"windows-{number:02d}.zip"
    temporary = path.with_suffix(".part")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3
        ) as archive:
            for source in paths:
                archive.write(source, source.relative_to(root).as_posix())
        if temporary.stat().st_size >= 2 * GIB:
            raise ValueError("A release asset exceeds GitHub's 2 GiB limit")
        temporary.replace(path)
        return {
            "name": path.name, "kind": "zip", "size": path.stat().st_size,
            "sha256": file_digest(path),
        }
    finally:
        temporary.unlink(missing_ok=True)


def split_file(source: Path, output: Path, number: int, limit: int) -> list[dict]:
    assets = []
    remaining = source.stat().st_size
    with source.open("rb") as stream:
        while remaining:
            path = output / f"weights-{number:02d}-{len(assets) + 1:02d}.bin"
            temporary = path.with_suffix(".part")
            length = min(remaining, limit)
            digest = hashlib.sha256()
            try:
                with temporary.open("wb") as destination:
                    left = length
                    while left:
                        block = stream.read(min(left, 4 * 1024**2))
                        if not block:
                            raise RuntimeError("Model file changed during packaging")
                        destination.write(block)
                        digest.update(block)
                        left -= len(block)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            assets.append({
                "name": path.name, "kind": "part", "size": length,
                "sha256": digest.hexdigest(),
            })
            remaining -= length
    return assets


def build_release(
    root: Path, output: Path, *, version: str, repository: str, source_commit: str,
    limit: int = MAX_ASSET_BYTES, packages: dict | None = None,
) -> Path:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Release output must be outside the portable input")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Version must be a numeric semantic version")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("A verified Git commit is required")
    if not 1024 <= limit <= MAX_ASSET_BYTES:
        raise ValueError("Invalid asset size limit")
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output / ".package.lock"), timeout=1):
        files = release_files(root)
        if not files:
            raise ValueError("Portable input has no release files")
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda path: record_file(root, path), files))
        groups: list[list[Path]] = []
        sizes: list[int] = []
        large: list[Path] = []
        for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True):
            size = path.stat().st_size
            if size > limit:
                large.append(path)
                continue
            index = next((i for i, total in enumerate(sizes) if total + size <= limit), None)
            if index is None:
                groups.append([])
                sizes.append(0)
                index = len(groups) - 1
            groups[index].append(path)
            sizes[index] += size
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(make_archive, root, output, index + 1, group)
                for index, group in enumerate(groups)
            ]
            assets = [future.result() for future in futures]
        joins = []
        for index, path in enumerate(large, 1):
            parts = split_file(path, output, index, limit)
            assets.extend(parts)
            joins.append({
                "path": path.relative_to(root).as_posix(),
                "parts": [part["name"] for part in parts],
            })
        if packages is None:
            result = subprocess.run(
                [str(root / "runtime/python/python.exe"), "-I", "-c",
                 ("import importlib.metadata as m, json; "
                  "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))")],
                check=True, text=True, capture_output=True, timeout=60,
            )
            packages = json.loads(result.stdout)
        manifest = {
            "schema": 1, "application": "AIBookVoiceStudio", "version": version,
            "repository": repository, "tag": "v" + version,
            "source_commit": source_commit, "platform": "windows-x64",
            "installed_bytes": sum(item["size"] for item in records),
            "download_bytes": sum(item["size"] for item in assets),
            "packages": dict(sorted(packages.items())), "assets": assets,
            "joins": joins, "files": records,
        }
        path = output / "install-manifest.json"
        temporary = path.with_suffix(".part")
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Package a verified Windows release")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    path = build_release(
        args.root, args.output, version=args.version, repository=args.repository,
        source_commit=args.source_commit,
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({
        "manifest": str(path), "assets": len(manifest["assets"]),
        "installed_gib": round(manifest["installed_bytes"] / GIB, 2),
        "download_gib": round(manifest["download_bytes"] / GIB, 2),
    }, indent=2))


if __name__ == "__main__":
    main()
