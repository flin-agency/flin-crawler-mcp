"""One SQLite database per crawl. Every fetched page commits with its evidence."""

import csv
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from .insights import page_changes
from .network import origin

ROBOTS_SCHEMA = """CREATE TABLE IF NOT EXISTS robots(url TEXT PRIMARY KEY, outcome TEXT,
    action TEXT, hops TEXT, reason TEXT)"""
SCHEMA = (
    """
CREATE TABLE job(id TEXT PRIMARY KEY, config TEXT, state TEXT, created REAL,
    finished REAL, pid INTEGER, message TEXT DEFAULT '', limited INTEGER DEFAULT 0);
CREATE TABLE pages(url TEXT PRIMARY KEY, depth INTEGER, source TEXT, status INTEGER,
    content_type TEXT, ms INTEGER, indexability TEXT, title TEXT, description TEXT,
    h1 TEXT, canonical TEXT, redirect TEXT, words INTEGER, content_hash TEXT, details TEXT, request_kind TEXT);
CREATE TABLE links(source TEXT, target TEXT, kind TEXT, anchor TEXT, nofollow INTEGER,
    PRIMARY KEY(source,target,kind,anchor,nofollow));
CREATE INDEX links_target ON links(target);
CREATE TABLE issues(code TEXT, severity TEXT, url TEXT, evidence TEXT,
    PRIMARY KEY(code,url,evidence));
CREATE INDEX issues_url ON issues(url);
CREATE TABLE sitemap_urls(url TEXT PRIMARY KEY);
CREATE TABLE alternates(url TEXT, code TEXT, target TEXT, channel TEXT, raw TEXT,
    PRIMARY KEY(url,code,target,channel,raw));
CREATE INDEX alternates_target ON alternates(target);
CREATE TABLE link_locations(url TEXT,target TEXT,region TEXT,PRIMARY KEY(url,target,region));
CREATE TABLE web_vitals(url TEXT,device TEXT,scope TEXT,measured_url TEXT,lcp_ms REAL,inp_ms REAL,cls REAL,
    assessment TEXT,period TEXT,PRIMARY KEY(url,device));
CREATE TABLE excluded_urls(url TEXT PRIMARY KEY,reason TEXT);
CREATE TABLE page_text(url TEXT PRIMARY KEY,language TEXT,body TEXT,unlinked TEXT);
CREATE TABLE render_changes(url TEXT,field TEXT,before TEXT,after TEXT);
CREATE INDEX render_url ON render_changes(url,field);
CREATE TABLE similar_pages(url TEXT,target TEXT,similarity REAL,PRIMARY KEY(url,target));
CREATE TABLE link_suggestions(url TEXT,target TEXT,anchor TEXT,snippet TEXT,PRIMARY KEY(url,target));
"""
    + ROBOTS_SCHEMA
    + ";"
)
TABLES = {
    "pages": "url",
    "robots": "url",
    "web_vitals": "url,device",
    "excluded_urls": "url",
    "render_changes": "url,field,before,after",
    "alternates": "url,code,target,channel,raw",
    "similar_pages": "similarity DESC,url,target",
    "link_suggestions": "url,target",
    "links": "source,target,kind,anchor,nofollow",
    "issues": "CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,code,url,evidence",
}
PAGE_COLUMNS = (
    "url",
    "depth",
    "source",
    "status",
    "content_type",
    "ms",
    "indexability",
    "title",
    "description",
    "h1",
    "canonical",
    "redirect",
    "words",
    "content_hash",
    "details",
    "request_kind",
)


def root():
    return (
        Path(os.environ.get("FLIN_CRAWLER_DATA_DIR", Path.home() / ".flin-crawler-mcp"))
        .expanduser()
        .resolve()
    )


def directory(crawl_id):
    try:
        valid = str(uuid.UUID(crawl_id)) == crawl_id
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Invalid crawl ID")
    return root() / crawl_id


def connect(crawl_id):
    path = directory(crawl_id) / "crawl.sqlite"
    if not path.is_file():
        raise ValueError("Unknown crawl ID")
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def create(config, crawl_id=None):
    crawl_id = crawl_id or str(uuid.uuid4())
    folder = directory(crawl_id)
    folder.mkdir(mode=0o700, parents=True)
    with sqlite3.connect(folder / "crawl.sqlite") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO job(id,config,state,created) VALUES(?,?,?,?)",
            (crawl_id, json.dumps(config), "queued", time.time()),
        )
    return crawl_id


def issue(db, code, severity, url, evidence):
    db.execute(
        "INSERT OR IGNORE INTO issues VALUES(?,?,?,?)",
        (code, severity, url, str(evidence)[:3000]),
    )


def prepare_worker(db):
    # A queued job can have been created by the previous Desktop version.
    db.execute(ROBOTS_SCHEMA)
    if "request_kind" not in {r[1] for r in db.execute("PRAGMA table_info(pages)")}:
        db.execute("ALTER TABLE pages ADD COLUMN request_kind TEXT")


def save_robots(db, policy):
    existing_transaction = db.in_transaction
    data = policy.report()
    db.execute(
        "INSERT OR REPLACE INTO robots VALUES(?,?,?,?,?)",
        (
            data["url"],
            data["outcome"],
            data["action"],
            json.dumps(data["hops"]),
            data["reason"],
        ),
    )
    if policy.action == "continue_without_rules":
        issue(db, "robots_redirect_fallback", "medium", data["url"], policy.reason)
    if not existing_transaction:
        # Expose the decision and release the write lock before fetching the page.
        db.commit()


def save_page(db, record):
    data = {
        **record,
        "details": json.dumps(record.get("details", {}), ensure_ascii=False),
    }
    db.execute(
        f"INSERT INTO pages VALUES({','.join('?' for _ in PAGE_COLUMNS)})",
        [data.get(k) for k in PAGE_COLUMNS],
    )


def job_status(db):
    row = dict(db.execute("SELECT * FROM job").fetchone())
    pid = row.pop("pid")
    if row["state"] in {"queued", "running", "stopping"}:
        missing = not pid and time.time() - row["created"] > 60
        if pid:
            try:
                if os.name == "nt":
                    import _winapi

                    try:
                        handle = _winapi.OpenProcess(0x1000, False, pid)
                    except OSError as exc:
                        if (
                            exc.winerror != 87
                        ):  # ERROR_INVALID_PARAMETER for an absent PID
                            raise
                        missing = True
                    else:
                        try:
                            missing = _winapi.GetExitCodeProcess(handle) != 259
                        finally:
                            _winapi.CloseHandle(handle)
                else:
                    os.kill(pid, 0)
            except ProcessLookupError:
                missing = True
            except PermissionError:
                pass
        if missing:
            db.execute(
                "UPDATE job SET state='interrupted',finished=? WHERE state IN ('queued','running','stopping')",
                (time.time(),),
            )
            db.commit()
            return job_status(db)
    row["config"] = json.loads(row["config"])
    row["limited"] = bool(row["limited"])
    row["pages_checked"] = db.execute("SELECT count(*) FROM pages").fetchone()[0]
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='robots'"
    ).fetchone():
        seed = row["config"]["urls"][0]
        evidence = report(db, "robots", 0, 1, url=origin(seed) + "/robots.txt")["rows"]
        row["seed_robots"] = evidence[0] if evidence else None
        row["checked_by_kind"] = dict(
            db.execute("SELECT request_kind,count(*) FROM pages GROUP BY request_kind")
        )
    row["coverage"] = dict(
        db.execute("SELECT indexability,count(*) FROM pages GROUP BY indexability")
    )
    row["top_issues"] = [
        dict(r)
        for r in db.execute("""SELECT code,severity,count(DISTINCT url) AS affected_urls FROM issues
        GROUP BY code,severity ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,affected_urls DESC,code LIMIT 15""")
    ]
    return row


def query(
    db,
    table,
    *,
    url=None,
    target=None,
    code=None,
    severity=None,
    status=None,
    field=None,
):
    if table not in TABLES:
        raise ValueError("Unknown report")
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        raise ValueError(
            "This crawl predates this report. Run a new crawl to collect the required evidence."
        )
    filters = {
        "source" if table == "links" else "url": url,
        "target": target,
        "code": code,
        "severity": severity,
        "field": field,
        "status": status,
    }
    allowed = {
        "links": {"source", "target"},
        "web_vitals": {"url"},
        "excluded_urls": {"url"},
        "alternates": {"url", "code", "target"},
        "render_changes": {"url", "field"},
        "similar_pages": {"url", "target"},
        "link_suggestions": {"url", "target"},
        "pages": {"url", "status"},
        "robots": {"url"},
        "issues": {"url", "code", "severity"},
    }[table]
    conditions, params = [], []
    for key, value in filters.items():
        if value is not None:
            if key not in allowed:
                raise ValueError(f"{key} is not a filter for {table}")
            conditions.append(key + "=?")
            params.append(value)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return f"SELECT * FROM {table}{where} ORDER BY {TABLES[table]}", params


def report(db, table, offset, limit, **filters):
    sql, params = query(db, table, **filters)
    rows = [
        dict(r)
        for r in db.execute(sql + " LIMIT ? OFFSET ?", [*params, limit + 1, offset])
    ]
    more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        if "details" in row:
            row["details"] = json.loads(row["details"])
        if table == "robots":
            row["hops"] = json.loads(row["hops"])
    return {"rows": rows, "next_offset": offset + len(rows) if more else None}


def export(db, crawl_id, table, **filters):
    folder = directory(crawl_id) / "exports"
    folder.mkdir(exist_ok=True)
    sql, params = query(db, table, **filters)
    cursor = db.execute(sql, params)

    def cell(v):
        if isinstance(v, str) and (
            v.lstrip().startswith(("=", "+", "-", "@"))
            or v.startswith(("\t", "\r", "\n"))
        ):
            return "'" + v
        return v

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        newline="",
        prefix=table + "-",
        suffix=".csv",
        dir=folder,
        delete=False,
    ) as f:
        writer = csv.writer(f)
        writer.writerow([col[0] for col in cursor.description])
        count = 0
        for row in cursor:
            writer.writerow([cell(v) for v in row])
            count += 1
    return {"path": f.name, "rows": count}


def compare(before, after, offset, limit, view="issues", field=None, url=None):
    with closing(connect(after)) as db:
        if not (directory(before) / "crawl.sqlite").is_file():
            raise ValueError("Unknown earlier crawl")
        db.execute(
            "ATTACH DATABASE ? AS previous", (str(directory(before) / "crawl.sqlite"),)
        )
        runs = [
            dict(
                db.execute(f"SELECT config,state,limited FROM {schema}.job").fetchone()
            )
            for schema in ("previous", "main")
        ]
        same = json.loads(runs[0]["config"]) == json.loads(runs[1]["config"])
        complete_graph = (
            same and runs[1]["state"] == "completed" and not runs[1]["limited"]
        )
        if view == "fields":
            return {
                **page_changes(db, offset, limit, field, url),
                "earlier_state": runs[0]["state"],
                "later_state": runs[1]["state"],
                "same_settings": same,
                "earlier_limited": bool(runs[0]["limited"]),
                "later_limited": bool(runs[1]["limited"]),
                "coverage": "Field changes compare observed values on URLs visited in both crawls.",
            }
        if field is not None or url is not None:
            raise ValueError("The field and url filters are available for view=fields")
        diff = """WITH absent AS (
            SELECT DISTINCT o.code,o.severity,o.url,p.indexability,p.status,p.details
            FROM previous.issues o LEFT JOIN main.pages p ON p.url=o.url
            WHERE NOT EXISTS (SELECT 1 FROM main.issues n WHERE n.code=o.code AND n.url=o.url)
        ) SELECT 'new' change, n.code,n.severity,n.url FROM main.issues n
          WHERE NOT EXISTS (SELECT 1 FROM previous.issues o WHERE o.code=n.code AND o.url=n.url)
          UNION SELECT CASE
            WHEN indexability IS NULL OR indexability IN ('fetch_error','robots_blocked','robots_unavailable','render_error','incomplete') THEN 'not_rechecked'
            WHEN code IN ('robots_disallow','robots_unavailable') AND json_extract(details,'$.robots_action')='continue_without_rules' THEN 'not_rechecked'
            WHEN code IN ('http_4xx','http_5xx','fetch_error','robots_disallow','robots_unavailable') AND status IS NOT NULL THEN 'resolved'
            WHEN (code LIKE 'duplicate_%' OR code LIKE 'hreflang_%' OR code IN ('broken_link','broken_resource','hreflang_missing_return',
                'canonical_target_problem','hreflang_target_problem','sitemap_non_indexable','redirect_chain','redirect_loop'))
                AND NOT ? THEN 'not_rechecked'
            WHEN indexability IN ('indexable','canonicalised','noindex','redirect') THEN 'resolved'
            ELSE 'not_rechecked' END, code,severity,url FROM absent"""
        results = [
            dict(r)
            for r in db.execute(
                diff + " ORDER BY 1,2,4 LIMIT ? OFFSET ?",
                (complete_graph, limit + 1, offset),
            )
        ]
        return {
            "earlier_state": runs[0]["state"],
            "later_state": runs[1]["state"],
            "same_settings": same,
            "later_limited": bool(runs[1]["limited"]),
            "rows": results[:limit],
            "next_offset": offset + limit if len(results) > limit else None,
        }
