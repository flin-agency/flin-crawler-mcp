"""Hreflang declarations from HTML, HTTP Link headers and XML sitemaps."""

import re
from functools import lru_cache

import pycountry

from .network import url_for


@lru_cache(maxsize=1024)
def valid_locale(code):
    if code == "x-default":
        return True
    parts = code.split("-")
    if not re.fullmatch("[a-z]{2}", parts[0]) or not pycountry.languages.get(
        alpha_2=parts[0]
    ):
        return False
    tail = parts[1:]
    if tail and len(tail[0]) == 4:
        if not pycountry.scripts.get(alpha_4=tail[0].title()):
            return False
        tail = tail[1:]
    return not tail or (
        len(tail) == 1 and bool(pycountry.countries.get(alpha_2=tail[0].upper()))
    )


def header_links(value):
    """Split link-values outside URI references and quoted parameter strings."""
    for match in re.finditer(r'<([^>]+)>((?:[^,"<]|"(?:\\.|[^"\\])*")*)', value):
        attrs = {}
        for param in re.finditer(
            r';\s*([\w-]+)\s*=\s*(?:"([^"]*)"|([^;,\s]+))', match[2]
        ):
            attrs[param[1].lower()] = param[2] if param[2] is not None else param[3]
        yield match[1], attrs


def declaration(url, code, raw, channel, base=None):
    dest = url_for(raw, base or url)
    return (url, str(code).strip().lower(), dest or "", channel, str(raw or "")[:2000])


def audit_locales(db, issue):
    def report(sql, code, level):
        for url, evidence in db.execute(sql):
            issue(db, code, level, url, evidence)

    for url, code, target, channel, raw in db.execute("SELECT * FROM alternates"):
        if not valid_locale(code):
            issue(
                db,
                "hreflang_invalid_code",
                "medium",
                url,
                f"{channel}: {code or '(empty)'}",
            )
        if not target or not url_for(raw):
            issue(
                db,
                "hreflang_invalid_url",
                "medium",
                url,
                f"{channel}: {raw or '(empty)'}",
            )
    report(
        """SELECT DISTINCT a.url, 'No language annotation points back to this URL' FROM alternates a
        WHERE NOT EXISTS(SELECT 1 FROM alternates b WHERE b.url=a.url AND b.target=a.url AND b.code!='x-default')""",
        "hreflang_missing_self",
        "medium",
    )
    report(
        """SELECT url, code || ' points to multiple URLs: ' || GROUP_CONCAT(DISTINCT target)
        FROM alternates WHERE target!='' GROUP BY url,code HAVING COUNT(DISTINCT target)>1""",
        "hreflang_conflicting_targets",
        "high",
    )
    report(
        """SELECT DISTINCT a.url, a.code || ' points to ' || a.target || ', self-labelled ' || b.code
        FROM alternates a JOIN alternates b ON b.url=a.target AND b.target=b.url
        WHERE a.code!='x-default' AND b.code!='x-default' AND a.url!=a.target
        AND NOT EXISTS(SELECT 1 FROM alternates c WHERE c.url=b.url AND c.target=b.url AND c.code=a.code)""",
        "hreflang_language_conflict",
        "medium",
    )
