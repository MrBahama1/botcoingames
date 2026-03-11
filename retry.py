"""Shared retry/backoff helper with jitter."""

import time
import random
import httpx
from config import BACKOFF_SCHEDULE


class RetryExhausted(Exception):
    def __init__(self, last_status, last_body):
        self.status = last_status
        self.body = last_body
        super().__init__(f"Retry exhausted: HTTP {last_status}")


class HTTPError(Exception):
    """Raised for non-retryable HTTP errors so callers can inspect status."""
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200]}")


def with_retry(fn, max_attempts=6, retry_on=(429, 500, 502, 503, 504), jitter_pct=0.25):
    """Call fn() with exponential backoff on retryable HTTP status codes.

    fn() should return an httpx.Response.
    Returns the parsed JSON response on success (2xx).
    Raises HTTPError for non-retryable 4xx errors (401, 403, 404, etc.).
    Raises RetryExhausted after max_attempts on retryable errors.
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            resp = fn()
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code in retry_on:
                # Retryable — will retry after backoff
                last_error = (resp.status_code, resp.text)
            elif resp.status_code == 403 and "<!DOCTYPE" in resp.text[:50]:
                # Cloudflare challenge page — transient, retry
                last_error = (resp.status_code, "Cloudflare challenge (retrying)")
            else:
                # Non-retryable error — raise immediately so caller can handle
                raise HTTPError(resp.status_code, resp.text)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in retry_on:
                last_error = (e.response.status_code, e.response.text)
            else:
                raise HTTPError(e.response.status_code, e.response.text)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout):
            last_error = (0, "connection/timeout error")
        except HTTPError:
            raise  # Don't catch our own HTTPError

        if attempt < max_attempts - 1:
            idx = min(attempt, len(BACKOFF_SCHEDULE) - 1)
            delay = BACKOFF_SCHEDULE[idx]
            # Check for retryAfterSeconds in response
            if last_error and last_error[1]:
                try:
                    import json
                    body = json.loads(last_error[1])
                    if "retryAfterSeconds" in body:
                        delay = max(delay, body["retryAfterSeconds"])
                except Exception:
                    pass
            jitter = delay * random.uniform(0, jitter_pct)
            time.sleep(delay + jitter)

    status, body = last_error or (0, "unknown")
    raise RetryExhausted(status, body)
