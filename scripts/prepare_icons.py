from __future__ import annotations

import urllib.request
from pathlib import Path

import cairosvg
import truststore

truststore.inject_into_ssl()
directory = Path(__file__).resolve().parents[1] / "aibook" / "assets" / "icons"
directory.mkdir(parents=True, exist_ok=True)
for name in (
    "play",
    "pause",
    "square",
    "folder-open",
    "rotate-ccw",
    "save",
    "pencil",
    "trash-2",
):
    with urllib.request.urlopen(
        f"https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/icons/{name}.svg",
        timeout=30,
    ) as response:
        svg = response.read()
    cairosvg.svg2png(
        bytestring=svg.replace(b"currentColor", b"#30363d"),
        write_to=str(directory / f"{name}.png"),
        output_width=20,
        output_height=20,
    )
with urllib.request.urlopen(
    "https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/LICENSE", timeout=30
) as response:
    (directory / "LICENSE.txt").write_bytes(response.read())
