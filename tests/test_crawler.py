import asyncio
import csv
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flin_crawler_mcp import storage
from flin_crawler_mcp.analysis import extract
from flin_crawler_mcp.network import Response, Rules, checked_address, url_for
from flin_crawler_mcp.worker import run

BASE = "https://fixture.example"
INDEX = """<html><head><title>Shop</title><meta name="description" content="Shop guide"></head>
<body><h1>Shop</h1><a href="/404">Details</a><a href="/old">Offer</a><a href="/private">Private</a>
<a href="/skip" rel="nofollow">Ignore</a><img src="/image.png"><a href="https://elsewhere.example/">Outside</a>
<script>document.querySelector("h1").textContent = "Rendered heading";</script></body></html>"""
FIXTURE = {
    "/robots.txt": (
        200,
        "text/plain",
        "User-agent: *\nDisallow: /private\nSitemap: /map.xml",
    ),
    "/map.xml": (
        200,
        "application/xml",
        "<sitemapindex><sitemap><loc>/child.xml</loc></sitemap></sitemapindex>",
    ),
    "/child.xml": (
        200,
        "application/xml",
        "<urlset><url><loc>/extra</loc></url></urlset>",
    ),
    "/": (200, "text/html", INDEX),
    "/404": (404, "text/html", "Missing"),
    "/old": (301, "/offer", ""),
    "/offer": (302, "/extra", ""),
    "/extra": (
        200,
        "text/html",
        '<title>Shop</title><meta name="description" content="Shop guide"><h1>Extra</h1>',
    ),
    "/image.png": (404, "image/png", ""),
}


def fixture(url, **kwargs):
    status, kind, text = FIXTURE.get(urlsplit(url).path, (404, "text/html", ""))
    headers = {"location": kind} if status in (301, 302) else {"content-type": kind}
    return Response(status, headers, text.encode(), 1)


def config(**kwargs):
    return dict(
        dict(
            urls=[BASE + "/"],
            mode="spider",
            max_urls=50,
            max_depth=6,
            render_js=False,
            render_wait_ms=50,
            use_sitemaps=True,
            check_assets=True,
            check_external=False,
            delay_ms=0,
            include_paths=[],
            exclude_paths=[],
            extractors={"heading": "h1"},
        ),
        **kwargs,
    )


class UnitCrawl(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, FLIN_CRAWLER_DATA_DIR=temp.name)
        env.start()
        self.addCleanup(env.stop)

    def crawl(self, **opts):
        crawl_id = storage.create(config(**opts))
        with patch("flin_crawler_mcp.network.fetch", side_effect=fixture):
            run(crawl_id)
        with closing(storage.connect(crawl_id)) as db:
            job = storage.job_status(db)
            self.assertEqual(job["state"], "completed", job)
        return crawl_id

    def test_evidence_graph_sitemaps_and_export(self):
        crawl_id = self.crawl()
        with closing(storage.connect(crawl_id)) as db:
            rows = storage.report(db, "issues", 0, 100)["rows"]
            codes = {r["code"] for r in rows}
            self.assertTrue(
                {
                    "http_4xx",
                    "broken_link",
                    "broken_resource",
                    "missing_alt_attribute",
                    "robots_disallow",
                    "duplicate_title",
                    "redirect_chain",
                }
                <= codes,
                codes,
            )
            urls = {r[0] for r in db.execute("SELECT url FROM pages")}
            self.assertIn(BASE + "/extra", urls)
            self.assertNotIn(BASE + "/skip", urls)
            self.assertNotIn("https://elsewhere.example/", urls)
            links = storage.report(db, "links", 0, 5, target=BASE + "/404")["rows"]
            self.assertEqual(
                (links[0]["source"], links[0]["anchor"]), (BASE + "/", "Details")
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM sitemap_urls").fetchone()[0], 1
            )
            self.assertEqual(storage.report(db, "pages", 0, 2)["next_offset"], 2)
            path = storage.export(db, crawl_id, "issues", code="broken_link")["path"]
            with open(path, encoding="utf-8-sig") as f:
                self.assertEqual(list(csv.DictReader(f))[0]["url"], BASE + "/")

    def test_list_mode_and_uncrawled_comparison(self):
        before = self.crawl()
        after = self.crawl(mode="list", urls=[BASE + "/extra"])
        change = storage.compare(before, after, 0, 100)["rows"]
        self.assertTrue(
            any(
                r["change"] == "not_rechecked" and r["url"] == BASE + "/"
                for r in change
            )
        )
        with closing(storage.connect(after)) as db:
            self.assertEqual(storage.job_status(db)["pages_checked"], 1)
        with patch.dict(
            FIXTURE, {"/404": (200, "text/html", "<title>Repaired</title><h1>OK</h1>")}
        ):
            repaired = self.crawl()
        self.assertIn(
            {
                "change": "resolved",
                "code": "http_4xx",
                "severity": "high",
                "url": BASE + "/404",
            },
            storage.compare(before, repaired, 0, 100)["rows"],
        )

    def test_url_budget_and_cancellation(self):
        with closing(storage.connect(self.crawl(max_urls=1))) as db:
            job = storage.job_status(db)
            self.assertTrue(job["limited"])
            self.assertEqual(job["pages_checked"], 1)
        crawl_id = storage.create(config())
        with closing(storage.connect(crawl_id)) as db:
            db.execute("UPDATE job SET state='stopping'")
            db.commit()
            run(crawl_id)
            self.assertEqual(storage.job_status(db)["state"], "cancelled")

    def test_robots_and_url_handling(self):
        rules = Rules(
            "User-agent: *\nDisallow: /\nUser-agent: flin-seo-crawler\nDisallow: /a*?x=$\nAllow: /a/b?x=$"
        )
        self.assertFalse(rules.allows(BASE + "/abc?x="))
        self.assertTrue(rules.allows(BASE + "/a/b?x="))
        self.assertTrue(rules.allows(BASE + "/abc?x=1"))
        self.assertEqual(
            url_for("../bücher#intro", BASE + "/x/"), BASE + "/b%C3%BCcher"
        )
        self.assertIsNone(url_for("javascript:alert(1)", BASE))
        with patch.dict(os.environ, FLIN_CRAWLER_ALLOW_LOOPBACK="0"):
            with self.assertRaises(ValueError):
                checked_address("127.0.0.1", 80)

    def test_seo_signals(self):
        doc = """<link rel="canonical" href="/canonical"><link rel="alternate" hreflang="de" href="/de">
        <meta name="robots" content="noindex"><h1>First</h1><h1>Second</h1><h2>Details</h2>
        <script type="application/ld+json">{"@graph":[{"@type":"Product"}]}</script>
        <script type="application/ld+json">{</script><img src="/decor" alt="">"""
        data, links, issues = extract(doc, BASE + "/", {}, {"headings": "h1"})
        self.assertEqual(data["indexability"], "noindex")
        self.assertEqual(data["canonical"], BASE + "/canonical")
        self.assertEqual(data["details"]["schema_types"], ["Product"])
        self.assertEqual(data["details"]["custom"]["headings"], ["First", "Second"])
        self.assertEqual(data["details"]["missing_alt"], [])
        self.assertIn("invalid_json_ld", {f[0] for f in issues})
        self.assertIn((BASE + "/", BASE + "/de", "hreflang", "de", 0), links)


@contextmanager
def website():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            reply = fixture(BASE + self.path)
            self.send_response(reply.status)
            for k, v in reply.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(reply.body)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        if os.getenv("FLIN_REQUIRE_BROWSER_TEST"):
            raise
        raise unittest.SkipTest("Loopback sockets unavailable in this runtime")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Protocol(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, url=None):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(
                os.environ,
                FLIN_CRAWLER_DATA_DIR=folder,
                FLIN_CRAWLER_ALLOW_LOOPBACK="1",
            )
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "flin_crawler_mcp.server"], env=env
            )
            async with (
                stdio_client(params) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual(len(tools.tools), 7)

                async def call(name, **kwargs):
                    res = await session.call_tool(name, kwargs)
                    self.assertFalse(res.isError, res.content)
                    return json.loads(res.content[0].text)

                self.assertEqual((await call("list_crawls"))["crawls"], [])
                if url is None:
                    return
                started = await call(
                    "start_crawl",
                    start_url=url,
                    max_urls=4,
                    mode="list",
                    render_js=True,
                    render_wait_ms=100,
                )
                cid = started["crawl_id"]
                for _ in range(150):
                    state = await call("crawl_status", crawl_id=cid)
                    if state["state"] not in ("queued", "running"):
                        break
                    await asyncio.sleep(0.2)
                self.assertEqual(state["state"], "completed", state)
                pages = await call("crawl_results", crawl_id=cid, dataset="pages")
                self.assertEqual(pages["rows"][0]["h1"], "Rendered heading", pages)
                file = await call("export_crawl", crawl_id=cid, dataset="pages")
                self.assertTrue(Path(file["path"]).is_file())

    async def test_stdio(self):
        await self.exercise()

    async def test_real_chromium_through_stdio(self):
        with website() as url:
            await self.exercise(url)
