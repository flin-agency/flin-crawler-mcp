"""Persistent, interruptible, bounded breadth-first crawl worker."""

import gzip
import http.client
import io
import json
import os
import sys
import time
from urllib.parse import urlsplit, unquote_plus
from collections import OrderedDict, deque
from contextlib import ExitStack

from defusedxml import ElementTree

from . import storage
from .specialists import staging_credentials
from .insights import content_opportunities, signal_changes
from .international import audit_locales, declaration, header_links
from .analysis import analyse_graph, extract
from .network import MAX_BODY, REDIRECTS, SiteClient, in_scope, origin, url_for


class Stopped(Exception):
    pass


def sitemap_entries(client, seed, db, seeds, budget, hosts=()):
    pending = deque(seeds)
    seen = set()
    while pending and len(seen) < 50:
        link = url_for(pending.popleft(), seed)
        if not link or link in seen or not in_scope(link, seed, hosts):
            continue
        seen.add(link)
        try:
            if not client.policy(link).rules.allows(link):
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
                if not target or not in_scope(target, seed, hosts):
                    continue
                if kind == "sitemapindex":
                    if len(pending) < 1000:
                        pending.append(target)
                else:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO sitemap_urls VALUES(?)", (target,)
                    ).rowcount
                    for alternate in entry.findall(
                        "{http://www.w3.org/1999/xhtml}link"
                    ):
                        if (
                            "alternate" not in alternate.get("rel", "").lower().split()
                            or "hreflang" not in alternate.attrib
                        ):
                            continue
                        row = declaration(
                            target,
                            alternate.get("hreflang"),
                            alternate.get("href"),
                            "sitemap",
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO alternates VALUES(?,?,?,?,?)", row
                        )
                        if row[2]:
                            db.execute(
                                "INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)",
                                (target, row[2], "hreflang", row[1], 0),
                            )
                            if in_scope(row[2], seed, hosts):
                                yield row[2]
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
    hosts = config.get("additional_hosts", [])
    max_resources = config.get("max_resource_urls")
    resource_budget = max_urls if max_resources is None else max_resources
    queue, assets, queued = deque(), OrderedDict(), set()
    page_count, resource_count = 0, 0
    limited = False
    variants = {}
    excluded = set()
    patterns = config.get("exclude_parameters", [])
    cap = config.get("max_query_variants_per_path")

    def schedule(url, depth, source, kind="page"):
        nonlocal limited
        if depth > config["max_depth"]:
            return
        if url in queued:
            if (
                kind == "page"
                and url in assets
                and len(queue) + page_count < max_urls * 5
            ):
                del assets[url]
                queue.append((url, depth, source, kind))
            return
        pending, checked, budget = (
            (queue, page_count, max_urls)
            if kind == "page"
            else (assets, resource_count, resource_budget)
        )
        if len(pending) + checked >= budget * 5:
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
        parts = urlsplit(url)
        if parts.query and (patterns or cap is not None):
            keys = {
                unquote_plus(pair.partition("=")[0]) for pair in parts.query.split("&")
            }
            key = (parts.netloc, parts.path)
            reason = None
            if any(
                any(k.startswith(p[:-1]) if p.endswith("*") else k == p for k in keys)
                for p in patterns
            ):
                reason = "excluded query parameter"
            elif cap is not None and variants.get(key, 0) >= cap:
                reason = "query variant limit"
            if reason:
                if len(excluded) < max_urls * 5 and url not in excluded:
                    excluded.add(url)
                    db.execute(
                        "INSERT OR IGNORE INTO excluded_urls VALUES(?,?)", (url, reason)
                    )
                return
            variants[key] = variants.get(key, 0) + 1
        queued.add(url)
        item = (url, depth, source, kind)
        if kind == "page":
            queue.append(item)
        else:
            assets[url] = item

    for url in urls:
        schedule(url, 0, "seed")
    loaded_sitemaps = False
    while max_resources is not None or page_count + resource_count < max_urls:
        stopped()
        if (
            not queue
            and page_count < max_urls
            and not loaded_sitemaps
            and config["use_sitemaps"]
            and config["mode"] == "spider"
        ):
            loaded_sitemaps = True
            sitemaps = []
            for site in dict.fromkeys(
                [origin(u) for u in urls]
                + [
                    f"{urlsplit(seed).scheme}://{host}"
                    + (f":{urlsplit(seed).port}" if urlsplit(seed).port else "")
                    for host in hosts
                ]
            ):
                rules = client.policy(site).rules
                sitemaps.extend(
                    url_for(candidate, site)
                    for candidate in (rules.sitemaps or [site + "/sitemap.xml"])
                )
            for dest in sitemap_entries(
                client, seed, db, sitemaps, max_urls * 5 + 1, hosts
            ):
                schedule(dest, 0, "sitemap")
            db.commit()
        if queue and page_count < max_urls:
            url, depth, source, kind = queue.popleft()
            page_count += 1
        elif assets and resource_count < resource_budget:
            _, (url, depth, source, kind) = assets.popitem(last=False)
            resource_count += 1
        else:
            break
        policy = client.policy(url)
        record = {"url": url, "source": source, "depth": depth, "request_kind": kind}
        findings, edges, changes, explicit_alternates = [], [], [], []
        if not policy.rules.allows(url):
            record["indexability"] = (
                "robots_unavailable" if policy.reason else "robots_blocked"
            )
            findings.append(
                (
                    "robots_unavailable" if policy.reason else "robots_disallow",
                    "high" if policy.reason else "info",
                    policy.reason or "Disallowed by robots.txt",
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
                            in_scope(dest, seed, hosts) or config["check_external"]
                        ):
                            schedule(
                                dest,
                                depth,
                                url,
                                kind if in_scope(dest, seed, hosts) else "external",
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
                    if kind != "external" and 200 <= reply.status < 300:
                        for dest, attrs in header_links(reply.headers.get("link", "")):
                            if (
                                "alternate" in attrs.get("rel", "").lower().split()
                                and "hreflang" in attrs
                            ):
                                item = declaration(url, attrs["hreflang"], dest, "http")
                                explicit_alternates.append(item)
                                if item[2]:
                                    edges.append((url, item[2], "hreflang", item[1], 0))
                                    if config["mode"] == "spider" and in_scope(
                                        item[2], seed, hosts
                                    ):
                                        schedule(item[2], depth + 1, url)
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
                        markup,
                        url,
                        reply.headers,
                        config["extractors"],
                        config.get("page_types"),
                    )
                    if render_info is not None and "error" not in render_info:
                        original, source_links, _ = extract(
                            reply.body,
                            url,
                            reply.headers,
                            config["extractors"],
                            config.get("page_types"),
                        )
                        changes = list(
                            signal_changes(original, data, source_links, edges)
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
                            if in_scope(dest, seed, hosts):
                                if relation in ("link", "canonical", "hreflang"):
                                    schedule(dest, depth + 1, url)
                                elif config["check_assets"]:
                                    schedule(dest, depth + 1, url, "asset")
                            elif relation == "link" and config["check_external"]:
                                schedule(dest, depth + 1, url, "external")
            except (OSError, ValueError, http.client.HTTPException) as err:
                record["indexability"] = "fetch_error"
                findings.append(("fetch_error", "high", str(err)))
        if policy.action == "continue_without_rules":
            record.setdefault("details", {})["robots_action"] = policy.action
        with db:
            if "_text" in record:
                db.execute("INSERT INTO page_text VALUES(?,?,?,?)", record.pop("_text"))
                db.executemany(
                    "INSERT OR IGNORE INTO link_locations VALUES(?,?,?)",
                    record.pop("_locations"),
                )
                explicit_alternates.extend(record.pop("_alternates"))
            db.executemany(
                "INSERT OR IGNORE INTO alternates VALUES(?,?,?,?,?)",
                explicit_alternates,
            )
            db.executemany(
                "INSERT INTO render_changes VALUES(?,?,?,?)",
                ((url, *c) for c in changes),
            )
            storage.save_page(db, record)
            db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", edges)
            for code, severity, evidence in findings:
                storage.issue(db, code, severity, url, evidence)
    with db:
        db.execute(
            "UPDATE job SET limited=?",
            (
                int(
                    limited
                    or bool(queue or assets)
                    or (
                        page_count == max_urls
                        and config["use_sitemaps"]
                        and config["mode"] == "spider"
                        and not loaded_sitemaps
                    )
                ),
            ),
        )
        stopped()
        analyse_graph(db, storage.issue)
        audit_locales(db, storage.issue)
    content_opportunities(db, stopped)
    db.commit()


def run(crawl_id):
    with storage.connect(crawl_id) as db:

        def stopped():
            if db.execute("SELECT state FROM job").fetchone()[0] == "stopping":
                raise Stopped()

        try:
            stopped()
            claimed = db.execute(
                "UPDATE job SET pid=?,state='running' WHERE state='queued'",
                (os.getpid(),),
            ).rowcount
            db.commit()
            if not claimed:
                stopped()
                return
            config = json.loads(db.execute("SELECT config FROM job").fetchone()[0])
            credentials = (
                staging_credentials(config["urls"][0])
                if config.get("staging_auth")
                else None
            )
            storage.prepare_worker(db)
            client = SiteClient(
                config["delay_ms"] / 1000,
                stopped,
                credentials,
                redirect_fallback_origin=origin(config["urls"][0])
                if config.get("allow_robots_redirect_failure")
                else None,
                on_policy=lambda policy: storage.save_robots(db, policy),
            )
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
