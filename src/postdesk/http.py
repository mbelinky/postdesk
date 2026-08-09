from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import ApiError


class Transport(Protocol):
    def request(
        self, method: str, url: str, parameters: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]: ...


class UrlLibTransport:
    def request(
        self, method: str, url: str, parameters: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        data = None
        target = url
        if method == "GET" or method == "DELETE":
            if parameters:
                target += "?" + urlencode(parameters, doseq=True)
        else:
            data = urlencode(parameters, doseq=True).encode("utf-8")
        request = Request(target, data=data, method=method, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            detail = _api_detail(raw, f"HTTP {exc.code}")
            raise ApiError(
                detail,
                path=url,
                definitive=True,
                not_found=exc.code == 404,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"API unavailable: {exc}", path=url, definitive=False) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("API returned invalid JSON.", path=url) from exc
        if not isinstance(payload, dict):
            raise ApiError("API returned invalid data.", path=url)
        if "error" in payload:
            error = payload.get("error")
            detail = str(error.get("message")) if isinstance(error, dict) else str(error)
            raise ApiError(detail or "API request failed.", path=url, definitive=True)
        return payload


def _api_detail(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    return str(error.get("message") or fallback) if isinstance(error, dict) else fallback
