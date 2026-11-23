"""Persistent, interruptible, bounded breadth-first crawl worker."""

import gzip
import http.client
import io
import json
import os
import sys
import time
from collections import deque
from contextlib import ExitStack
from urllib.parse import urlsplit

from defusedxml import ElementTree

from . import storage
from .analysis import analyse_graph, extract
from .network import MAX_BODY, REDIRECTS, SiteClient, in_scope, origin, url_for


class Stopped(Exception):
    pass


def sitemap_entries(client, seed, db, seeds, budget):
    pending = deque(seeds)
    seen = set()
    while pending and len(seen) < 50:
        link = url_for(pending.popleft(), seed)
        if not link or link in seen or not in_scope(link, seed):
            continue
        seen.add(link)
        try:
            rules, _ = client.policy(link)
            if not rules.allows(link):
                continue
            result = client.get(link)
            if result.status in REDIRECTS:
                if loc := result.headers.get("location"):
                    pending.append(url_for(loc, link))
                continue
            if result.status != 200:
                if result.status != 404:
                    storage.issue(
                        db, "sitemap_fetch_error", "medium", link, result.status
                    )
                continue
            data = result.body
            if data[:2] == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    data = stream.read(MAX_BODY + 1)
            if len(data) > MAX_BODY or result.truncated:
                raise ValueError("Sitemap exceeds 5 MiB")
            doc = ElementTree.fromstring(data)
            kind = doc.tag.split("}")[-1]
            if kind not in ("urlset", "sitemapindex"):
                raise ValueError("Not an XML sitemap")
            child = "url" if kind == "urlset" else "sitemap"
            for entry in doc.findall("{*}" + child):
                target = (
                    url_for(entry.findtext("{*}loc", default=""), link)
                    if entry.findtext("{*}loc")
                    else None
                )
                if not target or not in_scope(target, seed):
                    continue
                if kind == "sitemapindex":
                    if len(pending) < 1000:
                        pending.append(target)
                else:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO sitemap_urls VALUES(?)", (target,)
                    ).rowcount
                    budget -= inserted
                    if inserted:
                        yield target
                    if budget <= 0:
                        return
        except (
            OSError,
            ValueError,
            http.client.HTTPException,
            ElementTree.ParseError,
        ) as exc:
            storage.issue(db, "sitemap_error", "medium", link, exc)


def perform(db, config, client, stopped, renderer=None):
    urls, max_urls = config["urls"], config["max_urls"]
    seed = urls[0]
    queue, assets, queued = deque(), deque(), set()
    limited = False

    def schedule(url, depth, source, kind="page"):
        nonlocal limited
        if url in queued or depth > config["max_depth"]:
            return
        if len(queued) >= max_urls * 5:
            limited = True
            return
        if config["exclude_paths"] and urlsplit(url).path.startswith(
            tuple(config["exclude_paths"])
        ):
            return
        if config["include_paths"] and not urlsplit(url).path.startswith(
            tuple(config["include_paths"])
        ):
            return
        queued.add(url)
        (queue if kind == "page" else assets).append((url, depth, source, kind))

    for url in urls:
        schedule(url, 0, "seed")
    count, loaded_sitemaps = 0, False
    while count < max_urls:
        stopped()
        if (
            not queue
            and not loaded_sitemaps
            and config["use_sitemaps"]
            and config["mode"] == "spider"
        ):
            loaded_sitemaps = True
            rules, _ = client.policy(seed)
            sitemaps = rules.sitemaps or [origin(seed) + "/sitemap.xml"]
            for dest in sitemap_entries(client, seed, db, sitemaps, max_urls * 5 + 1):
                schedule(dest, 0, "sitemap")
            db.commit()
        if not queue and not assets:
            break
        url, depth, source, kind = (queue or assets).popleft()
        rules, reason = client.policy(url)
        record = {"url": url, "source": source, "depth": depth}
        findings, edges = [], []
        if not rules.allows(url):
            record["indexability"] = "robots_blocked"
            findings.append(
                (
                    "robots_unavailable" if reason else "robots_disallow",
                    "high" if reason else "info",
                    reason or "Disallowed by robots.txt",
                )
            )
        else:
            try:
                reply = client.get(url, metadata_only=kind in ("asset", "external"))
                record.update(status=reply.status, content_type=reply.mime, ms=reply.ms)
                if reply.status in REDIRECTS:
                    loc = reply.headers.get("location", "")
                    dest = url_for(loc, url) if loc else None
                    record.update(indexability="redirect", redirect=dest)
                    if dest:
                        edges.append((url, dest, "redirect", "", 0))
                        if config["mode"] == "spider" and (
                            in_scope(dest, seed) or config["check_external"]
                        ):
                            schedule(
                                dest,
                                depth,
                                url,
                                kind if in_scope(dest, seed) else "external",
                            )
                    else:
                        findings.append(
                            ("invalid_redirect", "high", loc or "No Location header")
                        )
                elif reply.status >= 400:
                    record["indexability"] = "http_error"
                    findings.append(
                        (
                            "http_5xx" if reply.status >= 500 else "http_4xx",
                            "high",
                            reply.status,
                        )
                    )
                elif kind == "external":
                    record["indexability"] = "external_checked"
                elif (
                    kind == "asset"
                    or reply.mime not in ("text/html", "application/xhtml+xml")
                    or not 200 <= reply.status < 300
                ):
                    record["indexability"] = "asset"
                elif reply.truncated:
                    record["indexability"] = "incomplete"
                    findings.append(("html_too_large", "medium", "HTML exceeds 5 MiB"))
                else:
                    markup = reply.body
                    render_info = None
                    if renderer:
                        try:
                            markup, render_info = renderer.html(
                                url, reply, config["render_wait_ms"]
                            )
                        except Stopped:
                            raise
                        except Exception as err:
                            render_info = {
                                "error": str(err)[:700],
                                "fallback": "source HTML",
                            }
                    data, edges, signals = extract(
                        markup, url, reply.headers, config["extractors"]
                    )
                    findings.extend(signals)
                    record.update(data)
                    if render_info is not None:
                        record["details"]["rendering"] = render_info
                        if "error" in render_info:
                            record["indexability"] = "render_error"
                            findings.append(
                                ("render_error", "high", render_info["error"])
                            )
                        elif render_info.get("failed_resources") or render_info.get(
                            "resource_limit_reached"
                        ):
                            findings.append(
                                ("render_incomplete", "medium", json.dumps(render_info))
                            )
                    if config["mode"] == "spider":
                        for _, dest, relation, _, nofollow in edges:
                            if nofollow:
                                continue
                            if in_scope(dest, seed):
                                if relation in ("link", "canonical", "hreflang"):
                                    schedule(dest, depth + 1, url)
                                elif config["check_assets"]:
                                    schedule(dest, depth + 1, url, "asset")
                            elif relation == "link" and config["check_external"]:
                                schedule(dest, depth + 1, url, "external")
            except (OSError, ValueError, http.client.HTTPException) as err:
                record["indexability"] = "fetch_error"
                findings.append(("fetch_error", "high", str(err)))
        with db:
            storage.save_page(db, record)
            db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", edges)
            for code, severity, evidence in findings:
                storage.issue(db, code, severity, url, evidence)
        count += 1
    with db:
        db.execute(
            "UPDATE job SET limited=?",
            (
                int(
                    limited
                    or bool(queue or assets)
                    or (
                        count == max_urls
                        and config["use_sitemaps"]
                        and config["mode"] == "spider"
                        and not loaded_sitemaps
                    )
                ),
            ),
        )
        stopped()
        analyse_graph(db, storage.issue)


def run(crawl_id):
    with storage.connect(crawl_id) as db:

        def stopped():
            if db.execute("SELECT state FROM job").fetchone()[0] == "stopping":
                raise Stopped()

        try:
            stopped()
            db.execute(
                "UPDATE job SET pid=?,state='running' WHERE state='queued'",
                (os.getpid(),),
            )
            db.commit()
            config = json.loads(db.execute("SELECT config FROM job").fetchone()[0])
            client = SiteClient(config["delay_ms"] / 1000, stopped)
            with ExitStack() as resources:
                renderer = None
                if config["render_js"]:
                    from .render import Renderer

                    renderer = resources.enter_context(Renderer(client))
                perform(db, config, client, stopped, renderer)
            state, message = "completed", ""
        except Stopped:
            state, message = "cancelled", "Partial page evidence retained"
        except Exception as err:
            state, message = "failed", f"{type(err).__name__}: {err}"[:1200]
        db.execute(
            "UPDATE job SET state=CASE WHEN state='stopping' THEN 'cancelled' ELSE ? END,message=?,finished=?",
            (state, message, time.time()),
        )
        db.commit()


if __name__ == "__main__":
    run(sys.argv[1])
