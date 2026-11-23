"""Local content matching and repeatable SEO report queries."""

from collections import Counter, defaultdict
import hashlib
import heapq
import json
import re
from urllib.parse import urlsplit

WORD = re.compile(r"[^\W_]+", re.UNICODE)


def signal_changes(before, after, source_links, rendered_links):
    for key in (
        "title",
        "description",
        "canonical",
        "indexability",
        "words",
        "content_hash",
    ):
        if before[key] != after[key]:
            yield key, before[key], after[key]
    for key in ("h1", "h2", "robots", "hreflang", "schema_types"):
        old, new = before["details"][key], after["details"][key]
        if old != new:
            yield (
                key,
                json.dumps(old, ensure_ascii=False),
                json.dumps(new, ensure_ascii=False),
            )
    left, right = (
        {(e[1], e[3], e[4]) for e in edges if e[2] == "link"}
        for edges in (source_links, rendered_links)
    )
    for name, items in (("link_added", right - left), ("link_removed", left - right)):
        for entry in sorted(items):
            item = json.dumps(
                dict(zip(("target", "anchor", "nofollow"), entry)), ensure_ascii=False
            )
            yield (
                name,
                item if name == "link_removed" else None,
                item if name == "link_added" else None,
            )


def content_opportunities(db, stopped):
    # Prefer unambiguous, multi-word destination phrases in the same language.
    targets = defaultdict(lambda: defaultdict(set))
    signature_index = defaultdict(list)
    sketches = {}
    for (
        url,
        lang,
        h1,
        title,
    ) in db.execute("""SELECT p.url,t.language,p.h1,p.title FROM pages p
            JOIN page_text t USING(url) WHERE p.indexability='indexable' ORDER BY p.url"""):
        scope = lang or "@" + str(urlsplit(url).hostname)
        for phrase in (h1, title):
            words = tuple(WORD.findall((phrase or "").casefold()))
            if 2 <= len(words) <= 8 and len(" ".join(words)) >= 7:
                targets[scope][words].add(url)

    def pages():
        last_url = ""
        while True:
            rows = db.execute(
                """SELECT t.url,t.language,t.body,t.unlinked FROM page_text t
                JOIN pages p USING(url) WHERE p.indexability='indexable' AND t.url>? ORDER BY t.url LIMIT 32""",
                (last_url,),
            ).fetchall()
            if not rows:
                break
            yield from rows
            last_url = rows[-1][0]
            db.commit()

    for url, lang, body, unlinked in pages():
        stopped()
        scope = lang or "@" + str(urlsplit(url).hostname)
        existing = {
            r[0]
            for r in db.execute(
                "SELECT target FROM links WHERE source=? AND kind='link'", (url,)
            )
        }
        existing.add(url)
        for line in unlinked.splitlines():
            matches = list(WORD.finditer(line))
            tokens = tuple(m[0].casefold() for m in matches)
            for i, first in enumerate(matches):
                for n in range(min(8, len(matches) - i), 1, -1):
                    dests = targets[scope].get(tokens[i : i + n])
                    if not dests or len(dests) != 1:
                        continue
                    target = next(iter(dests))
                    if target in existing:
                        continue
                    end = matches[i + n - 1].end()
                    anchor = line[first.start() : end]
                    snippet = line[max(0, first.start() - 75) : end + 75]
                    db.execute(
                        "INSERT OR IGNORE INTO link_suggestions VALUES(?,?,?,?)",
                        (url, target, anchor, snippet),
                    )
                    existing.add(target)
                    break

        words = WORD.findall(body.casefold())
        if len(words) < 40:
            continue
        features = {" ".join(words[i : i + 5]) for i in range(len(words) - 4)}
        if len(features) < 20:
            continue
        hashes = (
            int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")
            for s in features
        )
        sketch = frozenset(heapq.nsmallest(64, hashes))
        hits = Counter()
        for token in sketch:
            bucket = signature_index[scope, token]
            if len(bucket) <= 100:
                hits.update(bucket)
        for candidate, shared in hits.most_common(50):
            if shared < 10:
                break
            previous = sketches[candidate]
            sample = heapq.nsmallest(64, sketch | previous)
            similarity = sum(k in sketch and k in previous for k in sample) / len(
                sample
            )
            if similarity >= 0.8:
                db.execute(
                    "INSERT INTO similar_pages VALUES(?,?,?)",
                    (candidate, url, round(similarity, 3)),
                )
        sketches[url] = sketch
        for token in sketch:
            bucket = signature_index[scope, token]
            if len(bucket) <= 100:
                bucket.append(url)


def group_issues(db, dimensions, offset, limit, code=None, severity=None):
    dims = {
        "cause": "cause",
        "location": "location",
        "language": "language",
        "page_type": "page_type",
        "directory": "directory",
    }
    chosen = list(dict.fromkeys(dimensions))
    if any(d not in dims for d in chosen):
        raise ValueError("Unknown grouping dimension")
    has_locations = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='link_locations'"
    ).fetchone()
    location_join = (
        "LEFT JOIN link_locations l ON l.url=i.url AND l.target=target_url"
        if has_locations
        else ""
    )
    location_col = "COALESCE(l.region,'unknown')" if has_locations else "'unknown'"
    fields = ["code", "severity", *chosen]
    group = ", ".join(fields)
    sql = f"""WITH candidates AS (
        SELECT i.*,CASE WHEN i.code IN ('broken_link','broken_resource','canonical_target_problem','hreflang_target_problem')
            AND instr(i.evidence,' (')>0 THEN substr(i.evidence,1,instr(i.evidence,' (')-1) END AS target_url
        FROM issues i WHERE (? IS NULL OR i.code=?) AND (? IS NULL OR i.severity=?)
    ), classified AS (
        SELECT i.code,i.severity,i.url,i.evidence, COALESCE(target_url,i.code) cause,{location_col} location,
        COALESCE(NULLIF(json_extract(p.details,'$.language'),''),'unknown') language,
        COALESCE(json_extract(p.details,'$.page_type'),'unknown') page_type,
        COALESCE(json_extract(p.details,'$.directory'),'unknown') directory
        FROM candidates i LEFT JOIN pages p ON p.url=i.url {location_join}
    ) SELECT {group},count(DISTINCT url) affected_urls,
        json_extract(min(json_array(url,evidence)),'$[0]') example_url,
        json_extract(min(json_array(url,evidence)),'$[1]') example_evidence FROM classified GROUP BY {group}
        ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
        affected_urls DESC,{group} LIMIT ? OFFSET ?"""
    rows = [
        dict(r)
        for r in db.execute(sql, (code, code, severity, severity, limit + 1, offset))
    ]
    return {
        "rows": rows[:limit],
        "next_offset": offset + limit if len(rows) > limit else None,
        "interpretation": "Shared targets and HTML regions suggest a common cause. Confirm the template before applying a fix.",
    }


def page_changes(db, offset, limit, field=None, url=None):
    # Compare observable signals only when the relevant response was collected on both visits.
    html = "('indexable','noindex','canonicalised')"
    both_html = f"a.indexability IN {html} AND b.indexability IN {html}"
    parts = []
    params = []

    def select(name, old, new, guard):
        if field and field != name:
            return
        parts.append(f"""SELECT a.url, '{name}' field,{old} before,{new} after FROM previous.pages a JOIN main.pages b
            USING(url) WHERE ({guard}) AND {old} IS NOT {new} AND (? IS NULL OR a.url=?)""")
        params.extend((url, url))

    for key in (
        "title",
        "description",
        "h1",
        "canonical",
        "words",
        "content_hash",
        "status",
        "indexability",
        "redirect",
        "depth",
    ):
        guard = (
            both_html
            if key
            in ("title", "description", "h1", "canonical", "words", "content_hash")
            else "a.status IS NOT NULL AND b.status IS NOT NULL"
            if key in ("redirect", "status")
            else "1"
        )
        select(key, "a." + key, "b." + key, guard)
    for key in ("h2", "robots", "hreflang", "schema_types"):
        select(
            key,
            f"json_extract(a.details,'$.{key}')",
            f"json_extract(b.details,'$.{key}')",
            both_html,
        )
    for name, old, new in (
        ("link_added", "main", "previous"),
        ("link_removed", "previous", "main"),
    ):
        if field and field != name:
            continue
        value = "json_object('target',l.target,'anchor',l.anchor,'nofollow',l.nofollow)"
        parts.append(f"""SELECT l.source,'{name}',{value if name == "link_removed" else "NULL"},{value if name == "link_added" else "NULL"}
            FROM {old}.links l JOIN previous.pages a ON l.source=a.url JOIN main.pages b ON l.source=b.url
            WHERE l.kind='link' AND {both_html} AND (? IS NULL OR l.source=?) AND NOT EXISTS (
              SELECT 1 FROM {new}.links r WHERE r.source=l.source AND r.target=l.target AND r.kind='link'
              AND r.anchor=l.anchor AND r.nofollow=l.nofollow)""")
        params.extend((url, url))
    if not parts:
        raise ValueError("Unsupported change field")
    sql = " UNION ALL ".join(parts) + " ORDER BY 1,2,3,4 LIMIT ? OFFSET ?"
    rows = [
        dict(zip(("url", "field", "before", "after"), r))
        for r in db.execute(sql, (*params, limit + 1, offset))
    ]
    return {
        "rows": rows[:limit],
        "next_offset": offset + limit if len(rows) > limit else None,
    }
