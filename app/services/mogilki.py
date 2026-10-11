from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "maps" / "cemeteries"
BUNDLE_PATH = ROOT / "burials.json"
MAP_PATH = ROOT / "data.js"

_bundle: dict | None = None
_map: dict | None = None


def load_bundle() -> dict:
    global _bundle
    if _bundle is None:
        _bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    return _bundle


def load_map() -> dict:
    global _map
    if _map is None:
        raw = MAP_PATH.read_text(encoding="utf-8")
        _map = json.loads(raw[len("window.CEMETERY_MAP=") : -2])
    return _map
