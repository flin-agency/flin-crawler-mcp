import asyncio
import csv
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
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

    def test_uncrawled_link_target_is_not_a_fix(self):
        before = self.crawl()
        after = self.crawl(mode="list")
        rows = storage.compare(before, after, 0, 100)["rows"]
        self.assertIn(
            {
                "url": BASE + "/",
                "code": "broken_link",
                "severity": "high",
                "change": "not_rechecked",
            },
            rows,
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

    def test_group_shared_footer_link_across_templates(self):
        from flin_crawler_mcp.server import group_crawl_issues

        def page(h1):
            return (
                200,
                "text/html",
                f'<html lang="de-CH"><main><h1>{h1}</h1></main><footer><a href="/404">Downloads</a></footer></html>',
            )

        with patch.dict(
            FIXTURE, {"/de/shop/one": page("Eins"), "/de/shop/two": page("Zwei")}
        ):
            cid = self.crawl(
                urls=[BASE + "/de/shop/one", BASE + "/de/shop/two"],
                use_sitemaps=False,
                page_types={"product": "/de/shop/"},
            )
        groups = group_crawl_issues(
            cid,
            ["cause", "location", "language", "page_type", "directory"],
            code="broken_link",
        )["rows"]
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(
            (
                groups[0]["affected_urls"],
                groups[0]["location"],
                groups[0]["page_type"],
                groups[0]["language"],
                groups[0]["directory"],
            ),
            (2, "footer", "product", "de-ch", "/de/"),
        )
        self.assertEqual(groups[0]["cause"], BASE + "/404")

    def test_changed_fields_on_rechecked_pages(self):
        a = self.crawl()
        changed = (
            200,
            "text/html",
            INDEX.replace("<title>Shop</title>", "<title>New shop</title>").replace(
                'href="/404"', 'href="/fixed"'
            ),
        )
        with patch.dict(FIXTURE, {"/": changed}):
            b = self.crawl()
        for field in ("title", "link_added", "link_removed"):
            changes = storage.compare(a, b, 0, 100, "fields", field, BASE + "/")["rows"]
            self.assertEqual(len(changes), 1, changes)
            if field == "title":
                self.assertEqual(
                    (changes[0]["before"], changes[0]["after"]), ("Shop", "New shop")
                )
            else:
                key = "after" if field == "link_added" else "before"
                self.assertEqual(json.loads(changes[0][key])["anchor"], "Details")
        self.assertTrue(storage.compare(a, b, 0, 1, "fields")["next_offset"])

    def test_locale_sources_codes_conflicts_and_explicit_hosts(self):
        from flin_crawler_mcp.international import valid_locale

        self.assertTrue(
            all(valid_locale(s) for s in ("de-ch", "fr", "zh-hant-tw", "x-default"))
        )
        self.assertFalse(
            any(valid_locale(s) for s in ("en-uk", "zz-ch", "de-zz", "ch_zh", "en-419"))
        )
        de = BASE + "/de/a"
        fr = "https://fr.fixture.example/fr/a"
        xml = f'<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml"><url><loc>{de}</loc><xhtml:link rel="alternate" hreflang="fr" href="{fr}"/><xhtml:link rel="alternate" hreflang="de" href="{de}"/></url></urlset>'

        def network(url, **kwargs):
            if url == de:
                return Response(
                    200,
                    {
                        "content-type": "text/html",
                        "link": f'<{BASE}/wrong>; rel="alternate"; hreflang="fr"',
                    },
                    b'<html lang="de"><head><link rel="alternate" hreflang="zz" href="/bad"></head></html>',
                    1,
                )
            if url == fr:
                return Response(
                    200,
                    {"content-type": "text/html"},
                    f'<link rel="alternate" hreflang="it" href="{fr}">'.encode(),
                    1,
                )
            if url == BASE + "/child.xml":
                return Response(
                    200, {"content-type": "application/xml"}, xml.encode(), 1
                )
            return fixture(url, **kwargs)

        cid = storage.create(config(urls=[de], additional_hosts=["fr.fixture.example"]))
        with patch("flin_crawler_mcp.network.fetch", side_effect=network):
            run(cid)
        with closing(storage.connect(cid)) as db:
            self.assertEqual(storage.job_status(db)["state"], "completed")
            self.assertTrue(
                db.execute("SELECT 1 FROM pages WHERE url=?", (fr,)).fetchone()
            )
            self.assertEqual(
                {
                    r["channel"]
                    for r in storage.report(db, "alternates", 0, 100, url=de)["rows"]
                },
                {"http", "sitemap", "html"},
            )
            codes = {
                r[0] for r in db.execute("SELECT code FROM issues WHERE url=?", (de,))
            }
            self.assertTrue(
                {
                    "hreflang_invalid_code",
                    "hreflang_conflicting_targets",
                    "hreflang_language_conflict",
                    "hreflang_missing_return",
                }
                <= codes,
                codes,
            )

    def test_suggest_unlinked_phrases_and_similar_copy(self):
        content = " ".join("word" + str(i) for i in range(150))
        docs = {
            "/guide": (
                "de",
                "Bergtour planen",
                "Zu dieser Route passen unsere Leichten Bergschuhe besonders gut.",
            ),
            "/boots": ("de", "Leichte Bergschuhe", "Sommerwandern in der Schweiz."),
            "/fr": (
                "fr",
                "Voyages en train",
                "Auch Leichte Bergschuhe werden erwähnt.",
            ),
            "/linked": (
                "de",
                "Schuhe am Berg",
                '<a href="/boots">Produkt</a> und Leichte Bergschuhe',
            ),
            "/a": ("de", "Textfassung eins", content),
            "/b": ("de", "Textfassung zwei", content.replace("word100", "updated")),
        }
        mapping = {
            p: (
                200,
                "text/html",
                f'<html lang="{lang}"><main><h1>{h1}</h1><p>{body}</p></main></html>',
            )
            for p, (lang, h1, body) in docs.items()
        }
        mapping["/guide"] = (
            200,
            "text/html",
            mapping["/guide"][2].replace("Leichten Bergschuhe", "Leichte Bergschuhe"),
        )
        with patch.dict(FIXTURE, mapping):
            cid = self.crawl(mode="list", urls=[BASE + p for p in docs])
        with closing(storage.connect(cid)) as db:
            rows = storage.report(
                db, "link_suggestions", 0, 100, target=BASE + "/boots"
            )["rows"]
            self.assertEqual(
                [(r["url"], r["anchor"]) for r in rows],
                [(BASE + "/guide", "Leichte Bergschuhe")],
            )
            self.assertIn("Route passen", rows[0]["snippet"])
            matched = storage.report(db, "similar_pages", 0, 10)["rows"]
            self.assertEqual(
                (matched[0]["url"], matched[0]["target"]), (BASE + "/a", BASE + "/b")
            )
            self.assertGreaterEqual(matched[0]["similarity"], 0.8)

    def test_read_version_one_crawl(self):
        from flin_crawler_mcp.server import group_crawl_issues

        cid = self.crawl()
        with closing(storage.connect(cid)) as db:
            for name in (
                "link_locations",
                "alternates",
                "render_changes",
                "similar_pages",
                "link_suggestions",
                "page_text",
            ):
                db.execute("DROP TABLE " + name)
            db.execute("UPDATE pages SET details='{}'")
            db.commit()
            with self.assertRaisesRegex(ValueError, "predates"):
                storage.report(db, "render_changes", 0, 10)
            self.assertTrue(storage.report(db, "issues", 0, 10)["rows"])
        self.assertEqual(
            group_crawl_issues(cid, code="broken_link")["rows"][0]["location"],
            "unknown",
        )

    def test_parameter_budget_and_original_link_evidence(self):
        listing = "<h1>Shop</h1>" + "".join(
            f'<a href="/shop?{q}">Item</a>'
            for q in ("utm_source=ad", "colour=red", "colour=blue")
        )
        with patch.dict(FIXTURE, {"/": (200, "text/html", listing)}):
            cid = self.crawl(
                use_sitemaps=False,
                exclude_parameters=["utm_*"],
                max_query_variants_per_path=1,
            )
        with closing(storage.connect(cid)) as db:
            self.assertEqual(
                {r["url"] for r in storage.report(db, "excluded_urls", 0, 10)["rows"]},
                {BASE + "/shop?utm_source=ad", BASE + "/shop?colour=blue"},
            )
            self.assertEqual(len(storage.report(db, "links", 0, 10)["rows"]), 3)
            self.assertEqual(storage.job_status(db)["pages_checked"], 2)

    def test_staging_auth_is_opt_in_and_exact_origin(self):
        from flin_crawler_mcp.network import SiteClient
        from flin_crawler_mcp.specialists import staging_credentials

        with patch.dict(
            os.environ,
            FLIN_STAGING_ORIGIN=BASE,
            FLIN_STAGING_USER="fixture-user",
            FLIN_STAGING_PASSWORD="fixture-password",
            FLIN_STAGING_COOKIE="",
        ):
            auth = staging_credentials(BASE + "/")
            with self.assertRaises(ValueError):
                staging_credentials("https://other.example/")
            client = SiteClient(0, lambda: None, auth)
            with patch("flin_crawler_mcp.network.fetch", side_effect=fixture) as fetch:
                client.get(BASE + "/")
                self.assertIn(
                    "Authorization", fetch.call_args.kwargs["request_headers"]
                )
                for dest in (
                    "https://outside.example/",
                    "http://fixture.example/",
                    "https://fixture.example:8080/",
                ):
                    client.get(dest)
                    self.assertNotIn("request_headers", fetch.call_args.kwargs)
                SiteClient(0, lambda: None).get(BASE + "/")
                self.assertNotIn("request_headers", fetch.call_args.kwargs)

    def test_crux_response_url_origin_missing_and_export(self):
        from flin_crawler_mcp.server import check_web_vitals

        cid = self.crawl()
        record = {
            "metrics": {
                "largest_contentful_paint": {"percentiles": {"p75": 2500}},
                "interaction_to_next_paint": {"percentiles": {"p75": 600}},
                "cumulative_layout_shift": {"percentiles": {"p75": "0.10"}},
            },
            "collectionPeriod": {"firstDate": {"year": 2026, "month": 1, "day": 1}},
        }
        with (
            patch.dict(os.environ, FLIN_CRUX_API_KEY="fixture"),
            patch(
                "flin_crawler_mcp.specialists.crux_record",
                side_effect=[record, None, record],
            ),
        ):
            rows = check_web_vitals(cid, [BASE + "/", BASE + "/extra"])["rows"]
            self.assertEqual(
                [(r["scope"], r["assessment"]) for r in rows],
                [("url", "fail"), ("origin", "fail")],
            )
            self.assertEqual(rows[1]["measured_url"], BASE)
        with (
            patch.dict(os.environ, FLIN_CRUX_API_KEY="fixture"),
            patch("flin_crawler_mcp.specialists.crux_record", return_value=None),
        ):
            self.assertEqual(
                check_web_vitals(cid, [BASE + "/"])["rows"][0]["scope"], "no_data"
            )
        with closing(storage.connect(cid)) as db:
            self.assertEqual(storage.export(db, cid, "web_vitals")["rows"], 2)

    def test_rich_results_required_fields_and_graph_references(self):
        from flin_crawler_mcp.specialists import rich_result_issues

        nodes = [
            {"@type": "Product", "name": "Shoe", "offers": {"@id": "#offer"}},
            {"@id": "#offer", "@type": "Offer", "price": 0},
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"position": 2, "name": "End"},
                    {"position": 1, "name": "Home", "item": BASE},
                ],
            },
            {"@type": "Article", "headline": "Example"},
        ]
        self.assertEqual(list(rich_result_issues(nodes)), [])
        issues = list(
            rich_result_issues(
                [
                    {"@type": "Product"},
                    {"@type": "BreadcrumbList", "itemListElement": []},
                    {"@type": "Recipe", "name": "Suppe"},
                ]
            )
        )
        self.assertEqual(len(issues), 4)
        self.assertTrue(any("itemListElement" in r[2] for r in issues))

    def test_local_schedule_claim_notify_pause_and_timezone(self):
        from flin_crawler_mcp import schedules
        from datetime import datetime

        testtime = datetime.fromisoformat("2026-03-28T12:00:00+01:00").timestamp()
        next_run = schedules.next_due(testtime, "daily", "08:30", "Europe/Zurich", 0)
        self.assertEqual(
            datetime.fromtimestamp(
                next_run, schedules.ZoneInfo("Europe/Zurich")
            ).isoformat(),
            "2026-03-29T08:30:00+02:00",
        )
        cid = self.crawl()
        with patch.object(schedules, "wake"):
            schedule = schedules.create(
                cid, "weekly", "08:30", "Europe/Zurich", 0, True
            )
        with closing(schedules.connect()) as db, db:
            db.execute("INSERT INTO dispatcher VALUES(1,'owner')")
            db.execute("UPDATE schedules SET due=0")

        def launch_fixture(child_id):
            with patch("flin_crawler_mcp.network.fetch", side_effect=fixture):
                run(child_id)

        with (
            patch("flin_crawler_mcp.jobs.launch", side_effect=launch_fixture) as launch,
            patch.object(schedules, "desktop_notice") as notice,
        ):
            self.assertFalse(schedules.tick("obsolete", testtime))
            for _ in range(4):
                schedules.tick("owner", testtime)
            launch.assert_called_once()
            notice.assert_called_once()
        info = schedules.overview(10)
        self.assertEqual(len(info["events"]), 1)
        self.assertNotEqual(info["events"][0]["crawl_id"], cid)
        schedules.toggle(schedule["schedule_id"], False)
        self.assertIsNone(schedules.overview(10)["schedules"][0]["next_run"])

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
            if url and os.environ.get("FLIN_TEST_DESKTOP_PACKAGE"):
                project = Path(__file__).resolve().parents[1]
                version = json.loads((project / "manifest.json").read_text())["version"]
                bundle = project / "dist" / f"flin-crawler-mcp-{version}.mcpb"
                extension = Path(folder) / "extension"
                with zipfile.ZipFile(bundle) as archive:
                    archive.extractall(extension)
                manifest = json.loads((extension / "manifest.json").read_text())
                entry = manifest["server"]["mcp_config"]
                args = [
                    a.replace("${__dirname}", str(extension)) for a in entry["args"]
                ]
                env.pop("PYTHONPATH", None)
                params = StdioServerParameters(
                    command=entry["command"], args=args, env=env
                )
            async with (
                stdio_client(params) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                self.assertIn("group_crawl_issues", {tool.name for tool in tools.tools})

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
                changes = await call(
                    "crawl_results", crawl_id=cid, dataset="render_changes", field="h1"
                )
                self.assertEqual(json.loads(changes["rows"][0]["before"]), ["Shop"])
                self.assertEqual(
                    json.loads(changes["rows"][0]["after"]), ["Rendered heading"]
                )
                file = await call("export_crawl", crawl_id=cid, dataset="pages")
                self.assertTrue(Path(file["path"]).is_file())

    async def test_stdio(self):
        await self.exercise()

    async def test_real_chromium_through_stdio(self):
        with website() as url:
            await self.exercise(url)
