"""Shared HTTP fetching for the ingest modules: one client shape, retries, no secrets.

Every source GridCast reads is public and unauthenticated, so nothing sensitive ever passes
through here. Retries cover transient network errors and 429/5xx answers (IESO publishing
outages, Open-Meteo rate limits); a 404 is returned to the caller, because a missing IESO file
is a normal state ("not posted yet"), not an error.
"""

from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

USER_AGENT = "gridcast (github.com/hamzaziouine/gridcast)"
TIMEOUT_S = 60.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Apache-style directory listing: href="FILE"
_HREF_RE = re.compile(r'href="([^"?/]+)"')


class EmptyFeedError(ValueError):
    """A source file parsed to zero data rows (BUILD_PLAN §6 preamble)."""


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    text: str
    fetched_at: datetime  # UTC, when the response arrived
    last_modified: datetime | None  # HTTP Last-Modified (monitoring only, never availability)

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


class _RetryableStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, _RetryableStatusError | httpx.TransportError)


def fetch(
    url: str,
    *,
    params: dict[str, str] | None = None,
    attempts: int = 4,
    client: httpx.Client | None = None,
) -> Fetched:
    """GET `url`; retry transport errors and 429/5xx with exponential backoff.

    Returns 200 and 404 responses; raises httpx.HTTPStatusError for any other final status.
    """
    own = client is None
    cl = client or httpx.Client(
        timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                resp = cl.get(url, params=params)
                if resp.status_code in RETRY_STATUS:
                    raise _RetryableStatusError(resp.status_code)
                if resp.status_code not in (200, 404):
                    resp.raise_for_status()
                lm = resp.headers.get("Last-Modified")
                return Fetched(
                    url=str(resp.url),
                    status=resp.status_code,
                    text=resp.text if resp.status_code == 200 else "",
                    fetched_at=datetime.now(UTC),
                    last_modified=email.utils.parsedate_to_datetime(lm) if lm else None,
                )
    finally:
        if own:
            cl.close()
    raise AssertionError("unreachable")  # pragma: no cover


def list_directory(html: str) -> list[str]:
    """File names linked from an IESO reports-public directory listing."""
    return sorted(set(_HREF_RE.findall(html)))
