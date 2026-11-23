"""Bounded HTTP GETs with a DNS-pinned connection and per-origin robots policy."""

import functools
import gzip
import http.client
import io
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import certifi

AGENT = "flin-seo-crawler"
USER_AGENT = f"{AGENT}/0.1 (+https://github.com/flin-agency/flin-crawler-mcp)"
MAX_BODY = 5 * 1024 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


def url_for(value, base=""):
    """Return a normalised HTTP URL, or None for non-web references."""
    try:
        item = urlsplit(urljoin(base, str(value).strip()))
        if (
            item.scheme not in ("http", "https")
            or not item.hostname
            or item.username is not None
            or item.password is not None
        ):
            return None
        host = item.hostname.encode("idna").decode().lower().rstrip(".")
        if not host or re.search(r"[\s\\/%#@]", host):
            return None
        netloc = f"[{host}]" if ":" in host else host
        if item.port and item.port != (443 if item.scheme == "https" else 80):
            netloc += f":{item.port}"
        return urlunsplit(
            (
                item.scheme,
                netloc,
                quote(item.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                quote(item.query, safe="/?:@!$&'()*+,;=%-._~"),
                "",
            )
        )
    except (ValueError, UnicodeError):
        return None


def origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def in_scope(url, seed):
    a, b = urlsplit(url), urlsplit(seed)
    return a.hostname == b.hostname and a.port == b.port


def checked_address(host, port):
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for entry in found:
        ip = ipaddress.ip_address(entry[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback and os.environ.get("FLIN_CRAWLER_ALLOW_LOOPBACK") == "1":
            continue
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or getattr(ip, "sixtofour", None)
            or getattr(ip, "teredo", None)
        ):
            raise ValueError("Destination is outside the public web address range")
    if not found:
        raise OSError("No DNS address")
    return found[0][4][0]


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    ms: int
    truncated: bool = False

    @property
    def mime(self):
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


@functools.lru_cache(maxsize=1)
def tls_context():
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context


def fetch(url, *, metadata_only=False):
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    start = time.monotonic()
    address = checked_address(host, port)
    connection = http.client.HTTPConnection(host, port, timeout=12)
    try:
        connection.sock = socket.create_connection((address, port), timeout=12)
        if parts.scheme == "https":
            connection.sock = tls_context().wrap_socket(
                connection.sock, server_hostname=host
            )
        target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection.request(
            "GET",
            target,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )
        result = connection.getresponse()
        headers = {
            k.lower(): ", ".join(result.headers.get_all(k)) for k in result.headers
        }
        content = b"" if metadata_only else result.read(MAX_BODY + 1)
        if headers.get("content-encoding", "").lower() == "gzip" and content:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                content = stream.read(MAX_BODY + 1)
        return Response(
            result.status,
            headers,
            content[:MAX_BODY],
            round((time.monotonic() - start) * 1000),
            len(content) > MAX_BODY,
        )
    finally:
        connection.close()


def _rep_path(path):
    # Match UTF-8 octets and decode only percent-encoded unreserved ASCII.
    def escaped(m):
        char = chr(int(m[1], 16))
        return char if re.fullmatch(r"[A-Za-z0-9._~-]", char) else m[0].upper()

    return re.sub(r"%([0-9a-fA-F]{2})", escaped, quote(path, safe="/%?&=:;+,$*!'@()"))


class Rules:
    def __init__(self, text=""):
        groups, agents, entries = [], [], []
        self.sitemaps = []
        for line in text.lstrip("\ufeff").splitlines():
            key, sep, value = line.partition("#")[0].partition(":")
            key, value = key.strip().lower(), value.strip()
            if not sep:
                continue
            if key == "sitemap":
                self.sitemaps.append(value)
            elif key == "user-agent":
                if entries:
                    groups.append((agents, entries))
                    agents, entries = [], []
                agents.append(value.lower())
            elif agents and key in ("allow", "disallow", "crawl-delay"):
                entries.append((key, value))
        groups.append((agents, entries))
        scored = [
            (
                max(
                    (
                        len(a) if a != "*" else 0
                        for a in names
                        if a == "*" or (a and a in AGENT)
                    ),
                    default=-1,
                ),
                group,
            )
            for names, group in groups
        ]
        best = max((s for s, _ in scored), default=-1)
        self.rules, self.delay = [], 0
        for score, group in scored:
            if score < 0 or score < best:
                continue
            for key, value in group:
                if key == "crawl-delay":
                    if re.fullmatch(r"\d+(\.\d+)?", value):
                        self.delay = max(self.delay, float(value))
                elif value:
                    path = _rep_path(value)
                    end = path.endswith("$")
                    path = path[:-1] if end else path
                    # Store literal segments; matching is linear in the number of segments.
                    self.rules.append(
                        (len(path.replace("*", "")), key == "allow", path, end)
                    )

    def allows(self, url):
        parts = urlsplit(url)
        target = _rep_path(parts.path + (("?" + parts.query) if parts.query else ""))

        def matches(pattern, end):
            segments = pattern.split("*")
            if not target.startswith(segments[0]):
                return False
            if len(segments) == 1:
                return not end or target == pattern
            pos = len(segments[0])
            for segment in segments[1:-1]:
                pos = target.find(segment, pos)
                if pos < 0:
                    return False
                pos += len(segment)
            return (
                target[pos:].endswith(segments[-1])
                if end
                else segments[-1] in target[pos:]
            )

        return max(
            (
                (size, allow)
                for size, allow, pattern, end in self.rules
                if matches(pattern, end)
            ),
            default=(0, True),
        )[1]


class SiteClient:
    def __init__(self, delay, stopped):
        self.delay, self.stopped = delay, stopped
        self.policies, self.next_at = {}, {}
        self.lock = threading.RLock()

    def get(self, url, **options):
        with self.lock:
            site = origin(url)
            while (wait := self.next_at.get(site, 0) - time.monotonic()) > 0:
                self.stopped()
                time.sleep(min(0.2, wait))
            self.stopped()
            self.next_at[site] = time.monotonic() + max(
                self.delay,
                self.policies.get(site, (None,))[0].delay
                if site in self.policies
                else 0,
            )
        return fetch(url, **options)

    def policy(self, url):
        site = origin(url)
        with self.lock:
            if site not in self.policies:
                robots = site + "/robots.txt"
                try:
                    for _ in range(6):
                        reply = self.get(robots)
                        if reply.status not in REDIRECTS:
                            break
                        robots = url_for(reply.headers.get("location", ""), robots)
                        if not robots:
                            raise ValueError("Invalid robots.txt redirect")
                    if (
                        reply.status in (401, 403, 429)
                        or reply.status >= 500
                        or reply.status in REDIRECTS
                    ):
                        raise ValueError(f"robots.txt HTTP {reply.status}")
                    rules = (
                        Rules(reply.body.decode("utf-8", "replace"))
                        if 200 <= reply.status < 300
                        else Rules()
                    )
                    self.policies[site] = (rules, "")
                    self.next_at[site] = max(
                        self.next_at.get(site, 0), time.monotonic() + rules.delay
                    )
                except (ValueError, OSError, http.client.HTTPException) as err:
                    self.policies[site] = (
                        Rules("User-agent: *\nDisallow: /"),
                        str(err),
                    )
            return self.policies[site]
