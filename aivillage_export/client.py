from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


class ExportError(RuntimeError):
    pass


class HttpError(ExportError):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class CachedResponse:
    url: str
    status: int
    fetched_at: float
    body: Any


class PoliteHttpClient:
    """Sequential JSON client with per-host pacing, disk cache and backoff."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        delay: float = 1.25,
        retries: int = 5,
        refresh: bool = False,
        user_agent: str = "AI-Village-Mass-Exporter/0.1 (public archival client)",
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_delay = max(0.75, float(delay))
        self.retries = max(0, int(retries))
        self.refresh = refresh
        self.user_agent = user_agent
        self._last_request_by_host: dict[str, float] = {}
        self._host_delays: dict[str, float] = {}
        self.network_requests = 0
        self.cache_hits = 0

    def set_host_delay(self, host: str, seconds: float) -> None:
        self._host_delays[host.casefold()] = max(0.75, float(seconds))

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _load_cache(self, url: str) -> CachedResponse | None:
        path = self._cache_path(url)
        if self.refresh or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("url") != url:
                return None
            self.cache_hits += 1
            return CachedResponse(
                url=url,
                status=int(raw.get("status", 200)),
                fetched_at=float(raw.get("fetchedAt", 0.0)),
                body=raw.get("body"),
            )
        except (OSError, ValueError, TypeError):
            return None

    def _save_cache(self, response: CachedResponse) -> None:
        path = self._cache_path(response.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "url": response.url,
                    "status": response.status,
                    "fetchedAt": response.fetched_at,
                    "body": response.body,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _pace(self, host: str) -> None:
        host_key = host.casefold()
        delay = self._host_delays.get(host_key, self.default_delay)
        last = self._last_request_by_host.get(host_key)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_by_host[host_key] = time.monotonic()

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        value = headers.get("Retry-After") if headers else None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                return max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def get_json(self, url: str, *, accept: str = "application/json") -> Any:
        cached = self._load_cache(url)
        if cached is not None:
            return cached.body

        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        last_error: BaseException | None = None

        for attempt in range(self.retries + 1):
            self._pace(host)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": self.user_agent,
                },
            )
            try:
                self.network_requests += 1
                with urllib.request.urlopen(request, timeout=45) as response:
                    payload = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = json.loads(payload.decode(charset, "replace"))
                    cached_response = CachedResponse(
                        url=url,
                        status=int(response.status),
                        fetched_at=time.time(),
                        body=body,
                    )
                    self._save_cache(cached_response)
                    return body
            except urllib.error.HTTPError as error:
                last_error = error
                status = int(error.code)
                try:
                    payload = error.read().decode("utf-8", "replace")
                    detail = json.loads(payload)
                    message = detail.get("error") or detail.get("message") or payload[:300]
                except Exception:
                    message = str(error.reason)

                if status not in {408, 425, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise HttpError(f"HTTP {status} from {url}: {message}", status, url) from error

                wait = self._retry_after(error.headers)
                if wait is None:
                    wait = min(60.0, (2 ** attempt) + random.uniform(0.25, 1.25))
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                last_error = error
                if attempt >= self.retries:
                    raise HttpError(f"Request failed for {url}: {error}", None, url) from error
                time.sleep(min(60.0, (2 ** attempt) + random.uniform(0.25, 1.25)))

        raise HttpError(f"Request failed for {url}: {last_error}", None, url)
