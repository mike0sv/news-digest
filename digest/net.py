"""Small bounded HTTP client. Errors never contain provider bodies or credentials."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

USER_AGENT = "MorningDigest/0.1 (+https://github.com/mike0sv/news-digest)"


def http(url, *, payload=None, headers=None, timeout=30, retries=2, limit=3_000_000):
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username:
        raise ValueError("Expected an HTTP(S) URL without credentials")
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeError("HTTP response exceeds size limit")
                charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace")
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise RuntimeError(f"HTTP {status}") from None
        except (URLError, TimeoutError, OSError):
            if attempt == retries:
                raise RuntimeError("Network request failed or timed out") from None
        time.sleep(2 ** attempt * 2)
    raise RuntimeError("HTTP request failed")


def post_json(url, payload, headers, **kwargs):
    try:
        return json.loads(http(url, payload=payload, headers=headers, **kwargs))
    except json.JSONDecodeError:
        raise RuntimeError("Endpoint returned invalid JSON") from None
