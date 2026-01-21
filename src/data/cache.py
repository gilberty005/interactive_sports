from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional


class DiskCache:
    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        if cache_dir is None:
            cache_dir = Path(__file__).resolve().parents[2] / ".cache" / "nhl_api"
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key_to_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._key_to_path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set(self, key: str, value: Dict[str, Any]) -> None:
        path = self._key_to_path(key)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

