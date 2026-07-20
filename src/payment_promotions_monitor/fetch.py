from __future__ import annotations

import hashlib
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 TaiwanPromotionMonitor/0.1"
)

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 10


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    body: bytes
    text: str
    content_type: str
    content_hash: str


def is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    domains = (domain.lower().rstrip(".") for domain in allowed_domains)
    return any(domain and (host == domain or host.endswith(f".{domain}")) for domain in domains)


class _AllowListRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject an HTTP redirect before urllib issues the redirected request."""

    def __init__(self, allowed_domains: list[str]) -> None:
        super().__init__()
        self.allowed_domains = list(allowed_domains)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_url = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_url(target_url, self.allowed_domains):
            raise ValueError(f"Redirected outside official domain allow-list: {target_url}")
        return super().redirect_request(req, fp, code, msg, headers, target_url)


def _open_with_safe_redirects(
    request: urllib.request.Request,
    allowed_domains: list[str],
    *,
    timeout: float,
    context: ssl.SSLContext,
):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _AllowListRedirectHandler(allowed_domains),
    )
    return opener.open(request, timeout=timeout)


def canonical_url(url: str, base_url: str | None = None) -> str:
    absolute = urllib.parse.urljoin(base_url or url, url)
    parsed = urllib.parse.urlsplit(absolute)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
    query = sorted((key, value) for key, value in query if key.lower() not in ignored)
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urllib.parse.urlencode(query), "")
    )


def _charset(headers: Message, body: bytes) -> str:
    declared = headers.get_content_charset()
    if declared:
        return declared
    prefix = body[:4096].decode("ascii", errors="ignore").lower()
    for marker in ('charset="', "charset='", "charset="):
        index = prefix.find(marker)
        if index >= 0:
            value = prefix[index + len(marker) :].split(marker[-1] if marker[-1] in {'"', "'"} else ">", 1)[0]
            value = value.split()[0].strip("'\"; />")
            if value:
                return value
    return "utf-8"


def fetch_url(
    url: str,
    allowed_domains: list[str],
    *,
    timeout: float = 20.0,
    attempts: int = 2,
    max_bytes: int = 8_000_000,
) -> FetchResult:
    if not is_allowed_url(url, allowed_domains):
        raise ValueError(f"URL domain is not in this provider's allow-list: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
        },
    )
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with _open_with_safe_redirects(
                request,
                allowed_domains,
                timeout=timeout,
                context=context,
            ) as response:
                final_url = response.geturl()
                if not is_allowed_url(final_url, allowed_domains):
                    raise ValueError(f"Redirected outside official domain allow-list: {final_url}")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError(f"Response exceeded {max_bytes} bytes")
                content_type = response.headers.get_content_type()
                encoding = _charset(response.headers, body)
                try:
                    text = body.decode(encoding, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return FetchResult(
                    requested_url=url,
                    final_url=final_url,
                    status_code=response.status,
                    body=body,
                    text=text,
                    content_type=content_type,
                    content_hash=hashlib.sha256(body).hexdigest(),
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.7 * (attempt + 1))
    if last_error and "CERTIFICATE_VERIFY_FAILED" in str(last_error):
        return _fetch_with_system_curl(url, allowed_domains, timeout=timeout, max_bytes=max_bytes)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def post_json_url(
    url: str,
    payload: object,
    allowed_domains: list[str],
    *,
    timeout: float = 20.0,
) -> FetchResult:
    if not is_allowed_url(url, allowed_domains):
        raise ValueError(f"URL domain is not in this provider's allow-list: {url}")
    body_out = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body_out,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": "https://www.taiwanpay.com.tw/fisc-tpay/news/event",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with _open_with_safe_redirects(
            request,
            allowed_domains,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            final_url = response.geturl()
            if not is_allowed_url(final_url, allowed_domains):
                raise ValueError(f"Redirected outside official domain allow-list: {final_url}")
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise ValueError("JSON response exceeded 8000000 bytes")
            text = body.decode("utf-8", errors="replace")
            return FetchResult(
                requested_url=url,
                final_url=final_url,
                status_code=response.status,
                body=body,
                text=text,
                content_type="application/json",
                content_hash=hashlib.sha256(body).hexdigest(),
            )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Failed to POST {url}: {exc}") from exc


def _fetch_with_system_curl(
    url: str,
    allowed_domains: list[str],
    *,
    timeout: float,
    max_bytes: int,
) -> FetchResult:
    """Use the platform TLS stack when Python rejects an otherwise valid legacy chain.

    Certificate verification remains enabled; this never uses curl's insecure mode.
    Redirects are followed one request at a time so every target is allow-listed before
    curl connects to it.
    """
    marker = b"\n__PAYMENT_MONITOR_META__"
    current_url = url
    for redirect_count in range(_MAX_REDIRECTS + 1):
        if not is_allowed_url(current_url, allowed_domains):
            raise ValueError(f"Redirected outside official domain allow-list: {current_url}")
        command = [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(timeout))),
            "--max-filesize",
            str(max_bytes),
            "--user-agent",
            USER_AGENT,
            "--header",
            "Accept-Language: zh-TW,zh;q=0.9,en;q=0.5",
            "--write-out",
            marker.decode() + "%{http_code}\t%{url_effective}\t%{content_type}\t%{redirect_url}",
            current_url,
        ]
        completed: subprocess.CompletedProcess[bytes] | None = None
        for attempt in range(2):
            completed = subprocess.run(command, capture_output=True, check=False, timeout=timeout + 5)
            if completed.returncode == 0:
                break
            if attempt == 0:
                time.sleep(0.7)
        assert completed is not None
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"System curl fallback failed for {current_url}: {error}")
        try:
            body, metadata = completed.stdout.rsplit(marker, 1)
            status_value, effective_url, content_type, redirect_url = metadata.decode(
                "utf-8", errors="replace"
            ).split("\t", 3)
            status_code = int(status_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"System curl fallback returned malformed metadata for {current_url}") from exc
        if len(body) > max_bytes:
            raise RuntimeError(f"System curl fallback response exceeded {max_bytes} bytes")
        if not is_allowed_url(effective_url, allowed_domains):
            raise ValueError(f"Redirected outside official domain allow-list: {effective_url}")

        if status_code in _REDIRECT_STATUS_CODES:
            if not redirect_url:
                raise RuntimeError(f"System curl fallback received redirect without Location for {current_url}")
            next_url = urllib.parse.urljoin(effective_url, redirect_url)
            if not is_allowed_url(next_url, allowed_domains):
                raise ValueError(f"Redirected outside official domain allow-list: {next_url}")
            if redirect_count >= _MAX_REDIRECTS:
                raise RuntimeError(f"System curl fallback exceeded {_MAX_REDIRECTS} redirects for {url}")
            current_url = next_url
            continue

        text = body.decode("utf-8", errors="replace")
        return FetchResult(
            requested_url=url,
            final_url=effective_url,
            status_code=status_code,
            body=body,
            text=text,
            content_type=content_type.split(";", 1)[0] or "text/html",
            content_hash=hashlib.sha256(body).hexdigest(),
        )

    raise RuntimeError(f"System curl fallback exceeded {_MAX_REDIRECTS} redirects for {url}")


def looks_like_json(result: FetchResult) -> bool:
    stripped = result.text.lstrip()
    return result.content_type == "application/json" or stripped.startswith("{") or stripped.startswith("[")


def load_json(result: FetchResult) -> object:
    return json.loads(result.text)
