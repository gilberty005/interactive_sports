from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

from src.data.cache import DiskCache


class NHLApiClient:
    def __init__(self, base_url: str = "https://api-web.nhle.com/v1", cache: Optional[DiskCache] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache or DiskCache()

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        query = urlencode(sorted(params.items()))
        url = f"{self.base_url}/{path.lstrip('/')}"
        key = f"{url}?{query}"

        cached = self.cache.get(key)
        if cached is not None:
            return cached

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        self.cache.set(key, payload)
        return payload


class NHLStatsApiClient:
    def __init__(self, base_url: str = "https://api.nhle.com/stats/rest", cache: Optional[DiskCache] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache or DiskCache()

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        query = urlencode(sorted(params.items()))
        url = f"{self.base_url}/{path.lstrip('/')}"
        key = f"{url}?{query}"

        cached = self.cache.get(key)
        if cached is not None:
            return cached

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        self.cache.set(key, payload)
        return payload

