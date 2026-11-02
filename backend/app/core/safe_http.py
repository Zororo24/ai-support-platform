"""One place that sends HTTP requests to company-provided URLs safely.

Used for webhook deliveries (and shaped the same way as action calls in the executor): the
destination is validated by the SSRF guard, the connection goes to the *validated IP* while
presenting the real hostname, redirects are never followed, and responses are size-capped.
"""

from dataclasses import dataclass

import httpx

from app.core import ssrf

MAX_RESPONSE_BYTES = 64 * 1024

# Tests (and the demo) can route outbound calls through a specific client.
_client_override: httpx.Client | None = None


def set_client_override(client: httpx.Client | None) -> None:
    global _client_override
    _client_override = client


@dataclass
class Sent:
    status_code: int | None
    error: str | None = None  # a short, safe description; no internal addresses
    body_preview: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def post(url: str, *, content: bytes, headers: dict[str, str], timeout: float = 10.0) -> Sent:
    try:
        target = ssrf.validate_url(url)
    except ssrf.BlockedDestination as exc:
        return Sent(None, error=str(exc))

    host_header = target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
    ip = f"[{target.ip}]" if ":" in target.ip else target.ip
    pinned = f"{target.scheme}://{ip}:{target.port}{target.path_and_query}"
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}

    client = _client_override or httpx.Client(follow_redirects=False)
    try:
        with client.stream(
            "POST",
            pinned,
            content=content,
            headers={**headers, "Host": host_header},
            timeout=timeout,
            extensions=extensions,
        ) as response:
            received = b""
            for chunk in response.iter_bytes():
                received += chunk
                if len(received) >= MAX_RESPONSE_BYTES:
                    break
            return Sent(response.status_code, body_preview=received[:300].decode("utf-8", "replace"))
    except httpx.TimeoutException:
        return Sent(None, error=f"no response within {int(timeout)} seconds")
    except httpx.HTTPError:
        return Sent(None, error="could not reach the endpoint")
    finally:
        if _client_override is None:
            client.close()
