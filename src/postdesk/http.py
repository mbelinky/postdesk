from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
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


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    payload: dict[str, Any]


class JsonTransport(Protocol):
    """Bearer-token JSON transport for APIs that are not Graph-shaped (YouTube)."""

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        data: bytes | None = None,
        timeout: float,
    ) -> HttpResponse: ...

    def fetch_file(self, url: str, destination: Path, *, timeout: float) -> int: ...


class UrlLibJsonTransport:
    RESUME_INCOMPLETE = 308

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        data: bytes | None = None,
        timeout: float,
    ) -> HttpResponse:
        target = url
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            target += ("&" if "?" in target else "?") + urlencode(clean, doseq=True)
        request_headers = {"Accept": "application/json", **headers}
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif form is not None:
            body = urlencode(form, doseq=True).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = Request(target, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = int(response.status)
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            if exc.code == self.RESUME_INCOMPLETE:
                return HttpResponse(exc.code, {key.lower(): value for key, value in exc.headers.items()}, {})
            raise ApiError(
                _google_detail(raw, f"HTTP {exc.code}"),
                path=url,
                definitive=True,
                not_found=exc.code in {404, 410},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"API unavailable: {exc}", path=url, definitive=False) from exc
        if not raw.strip():
            return HttpResponse(status, response_headers, {})
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("API returned invalid JSON.", path=url) from exc
        if not isinstance(payload, dict):
            raise ApiError("API returned invalid data.", path=url)
        if "error" in payload:
            raise ApiError(_google_detail(raw, "API request failed."), path=url, definitive=True)
        return HttpResponse(status, response_headers, payload)

    def fetch_file(self, url: str, destination: Path, *, timeout: float) -> int:
        request = Request(url, method="GET")
        size = 0
        try:
            with urlopen(request, timeout=timeout) as response, destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    size += len(chunk)
        except HTTPError as exc:
            exc.close()
            raise ApiError(f"Media download failed: HTTP {exc.code}", path=url, definitive=True, not_found=exc.code == 404) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"Media download failed: {exc}", path=url, definitive=False) from exc
        return size


def _google_detail(raw: bytes, fallback: str) -> str:
    """Google APIs answer {"error": {"message", "errors": [{"reason"}]}}; OAuth answers {"error": "...", "error_description"}."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, str):
        description = str(payload.get("error_description") or "").strip()
        return f"{error}: {description}" if description else error
    if isinstance(error, dict):
        message = str(error.get("message") or fallback)
        reasons = sorted(
            {str(item.get("reason")) for item in error.get("errors") or [] if isinstance(item, dict) and item.get("reason")}
        )
        return f"{message} ({', '.join(reasons)})" if reasons else message
    return fallback


def _api_detail(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    return str(error.get("message") or fallback) if isinstance(error, dict) else fallback
