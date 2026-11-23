"""Extract SEO signals and keep the source URL attached to each link and finding."""

import hashlib
import json
import re

from bs4 import BeautifulSoup

from .network import url_for


def extract(markup, url, headers, selectors):
    soup = BeautifulSoup(markup, "html.parser")
    base_tag = soup.find("base", href=True)
    base = (url_for(base_tag["href"], url) if base_tag else None) or url

    def text(element):
        return " ".join(element.stripped_strings).strip()[:4000]

    def texts(selector):
        return [text(el) for el in soup.select(selector)]

    def metas(name):
        return [
            el.get("content", "").strip()
            for el in soup.find_all(
                "meta", attrs={"name": re.compile("^" + name + "$", re.I)}
            )
        ]

    titles, desc, h1, h2 = (
        texts("title"),
        metas("description"),
        texts("h1"),
        texts("h2"),
    )
    robots = metas("robots") + metas("googlebot")
    xrobots = headers.get("x-robots-tag", "")
    # Labelled headers for unrelated crawlers do not govern Google indexability.
    agent = None
    for part in xrobots.split(","):
        if ":" in part and not part.strip().lower().startswith(
            ("max-", "unavailable_after")
        ):
            agent, part = part.split(":", 1)
        if agent is None or agent.strip().lower() in ("googlebot", "*"):
            robots.append(part.strip())
    directives = set(re.split(r"[\s,]+", " ".join(robots).lower()))
    noindex = bool({"noindex", "none"} & directives)
    nofollow = bool({"nofollow", "none"} & directives)
    edges, langs, images, canonicals, findings = [], [], [], [], []

    def edge(target, kind, anchor="", nofollow=False):
        target = url_for(target, base) if target else None
        if target:
            edges.append((url, target, kind, anchor[:500], int(nofollow)))
        return target

    for el in soup.select("a[href],area[href]"):
        edge(
            el["href"],
            "link",
            text(el)
            or el.get("aria-label", "")
            or " ".join(img.get("alt", "") for img in el.select("img")),
            nofollow or "nofollow" in [s.lower() for s in el.get("rel", [])],
        )
    for el in soup.select("link[href]"):
        rel = {s.lower() for s in el.get("rel", [])}
        if "canonical" in rel:
            canonicals.append(edge(el["href"], "canonical") or "")
        if "alternate" in rel and el.get("hreflang"):
            langs.append(
                {
                    "lang": el["hreflang"].lower(),
                    "url": edge(el["href"], "hreflang", el["hreflang"].lower()),
                }
            )
        if "stylesheet" in rel:
            edge(el["href"], "stylesheet")
    link_header = headers.get("link", "")
    for dest, attributes in re.findall(r"<([^>]+)>([^,]*)", link_header):
        if re.search(r'\brel\s*=\s*["\']?canonical\b', attributes, re.I):
            canonicals.append(edge(dest, "canonical") or "")
    for el in soup.select("img"):
        target = edge(el.get("src") or el.get("data-src"), "image", el.get("alt", ""))
        if not el.has_attr("alt"):
            images.append(target or "(inline image)")
    for el in soup.select("script[src]"):
        edge(el["src"], "script")
    schema_types, invalid_json = set(), 0
    for el in soup.find_all("script", type=re.compile(r"^application/ld\+json$", re.I)):
        try:
            stack = [json.loads(el.string or "")]
            while stack:
                obj = stack.pop()
                if isinstance(obj, list):
                    stack.extend(obj)
                if isinstance(obj, dict):
                    value = obj.get("@type", [])
                    schema_types.update(
                        v
                        for v in (value if isinstance(value, list) else [value])
                        if isinstance(v, str)
                    )
                    stack.extend(v for v in obj.values() if isinstance(v, (list, dict)))
        except (ValueError, RecursionError):
            invalid_json += 1
    custom = {
        name: [text(el) for el in soup.select(selector, limit=20)]
        for name, selector in selectors.items()
    }
    for el in soup.select("script,style,template,noscript,head"):
        el.decompose()
    body = " ".join((soup.body or soup).stripped_strings)
    digest = (
        hashlib.sha256(" ".join(body.casefold().split()).encode()).hexdigest()
        if body.strip()
        else None
    )
    canonical = canonicals[0] if canonicals else None
    indexability = (
        "noindex"
        if noindex
        else "canonicalised"
        if canonical and canonical != url
        else "indexable"
    )
    for group, values in (("title", titles), ("description", desc), ("h1", h1)):
        if not any(values):
            findings.append((f"missing_{group}", "medium", f"No non-empty {group}"))
        if len(values) > 1:
            findings.append((f"multiple_{group}", "low", len(values)))
    if noindex:
        findings.append(("noindex", "info", ", ".join(robots)))
    if len(set(canonicals)) > 1:
        findings.append(("conflicting_canonicals", "high", str(canonicals)[:1000]))
    if images:
        findings.append(
            (
                "missing_alt_attribute",
                "low",
                f"{len(images)} image(s), e.g. {images[0]}",
            )
        )
    if invalid_json:
        findings.append(("invalid_json_ld", "medium", invalid_json))
    details = {
        "titles": titles[:10],
        "h1": h1[:20],
        "h2": h2[:40],
        "robots": robots,
        "hreflang": langs[:100],
        "schema_types": sorted(schema_types)[:100],
        "missing_alt": images[:20],
        "custom": custom,
    }
    return (
        {
            "title": next(iter(titles), ""),
            "description": next(iter(desc), ""),
            "h1": next(iter(h1), ""),
            "canonical": canonical,
            "indexability": indexability,
            "words": len(body.split()),
            "content_hash": digest,
            "details": details,
        },
        edges,
        findings,
    )


def analyse_graph(db, issue):
    for field in ("title", "description", "content_hash"):
        rows = db.execute(f"""SELECT p.url, d.n FROM pages p JOIN
            (SELECT {field} k,count(*) n FROM pages WHERE indexability='indexable' AND {field} != ''
             GROUP BY {field} HAVING count(*)>1) d ON d.k=p.{field} WHERE p.indexability='indexable'""")
        for url, count in rows:
            issue(
                db,
                "duplicate_" + ("content" if field == "content_hash" else field),
                "medium",
                url,
                f"{count} indexable URLs share this value",
            )
    for link in db.execute("""SELECT l.source,l.target,l.kind,p.status,p.indexability FROM links l
            JOIN pages p ON l.target=p.url WHERE p.status>=400 OR
            (l.kind IN ('canonical','hreflang') AND p.indexability NOT IN ('indexable','asset'))"""):
        src, target, kind, status, indexability = link
        code = {
            "canonical": "canonical_target_problem",
            "hreflang": "hreflang_target_problem",
            "link": "broken_link",
        }.get(kind, "broken_resource")
        issue(
            db,
            code,
            "high" if kind in ("link", "canonical", "hreflang") else "medium",
            src,
            f"{target} ({status or indexability})",
        )
    for (
        src,
        target,
    ) in db.execute("""SELECT DISTINCT l.source,l.target FROM links l JOIN pages p ON p.url=l.target
            WHERE l.kind='hreflang' AND l.target!=l.source AND p.status BETWEEN 200 AND 299
            AND p.indexability IN ('indexable','canonicalised','noindex')
            AND NOT EXISTS(SELECT 1 FROM links r WHERE r.kind='hreflang' AND r.source=l.target AND r.target=l.source)"""):
        issue(db, "hreflang_missing_return", "medium", src, target)
    redirects = dict(
        db.execute("SELECT url,redirect FROM pages WHERE redirect IS NOT NULL")
    )
    for url in redirects:
        seen, current = {url}, url
        while current in redirects:
            current = redirects[current]
            if current in seen:
                issue(db, "redirect_loop", "high", url, current)
                break
            seen.add(current)
            if len(seen) == 3:
                issue(db, "redirect_chain", "medium", url, current)
            if len(seen) > 100:
                break
    for (
        url,
        reason,
    ) in db.execute("""SELECT s.url,p.indexability FROM sitemap_urls s JOIN pages p ON p.url=s.url
        WHERE p.indexability NOT IN ('indexable','asset','external_unchecked')"""):
        issue(db, "sitemap_non_indexable", "medium", url, reason)
