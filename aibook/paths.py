from __future__ import annotations

import os
from pathlib import Path

PORTABLE_PREFIX = "$AIBOOK_ROOT$/"


def encode_portable_path(value: str | Path) -> str:
    path_text = str(value)
    root_text = os.environ.get("AIBOOK_ROOT")
    if not path_text or not root_text:
        return path_text
    root = Path(root_text).resolve(strict=False)
    path = Path(path_text).expanduser().resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path_text
    return PORTABLE_PREFIX + relative.as_posix()


def decode_portable_path(value: str | Path) -> Path:
    path_text = str(value)
    if not path_text.startswith(PORTABLE_PREFIX):
        return Path(path_text)
    root_text = os.environ.get("AIBOOK_ROOT")
    if not root_text:
        return Path(path_text)
    relative = path_text[len(PORTABLE_PREFIX) :]
    return Path(root_text).resolve(strict=False) / Path(relative)
