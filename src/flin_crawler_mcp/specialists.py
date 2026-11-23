"""Optional origin-bound staging credentials, CrUX field data and JSON-LD rules."""

import base64
import http.client
import json
import os
from urllib.parse import urlsplit

from .network import origin, tls_context, url_for


def staging_credentials(seed):
    configured = url_for(os.environ.get("FLIN_STAGING_ORIGIN", ""))
    if (
        not configured
        or urlsplit(configured).scheme != "https"
        or origin(seed) != origin(configured)
        or configured != origin(configured) + "/"
    ):
        raise ValueError("Set the exact HTTPS staging origin in the extension settings")
    user = os.environ.get("FLIN_STAGING_USER", "")
    password = os.environ.get("FLIN_STAGING_PASSWORD", "")
    cookie = os.environ.get("FLIN_STAGING_COOKIE", "")
    if not (user and password) and not cookie:
        raise ValueError(
            "Set staging Basic credentials or a current session cookie in the extension settings"
        )
    headers = {}
    if user and password:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        )
    if cookie:
        if any(ord(c) < 32 or ord(c) > 126 for c in cookie):
            raise ValueError("Session cookie must be a single ASCII header value")
        headers["Cookie"] = cookie
    return origin(configured), headers


VITALS = {
    "largest_contentful_paint": ("lcp_ms", 2500, 4000),
    "interaction_to_next_paint": ("inp_ms", 200, 500),
    "cumulative_layout_shift": ("cls", 0.10, 0.25),
}


def crux_record(key, scope, value, device):
    connection = http.client.HTTPSConnection(
        "chromeuxreport.googleapis.com", context=tls_context(), timeout=15
    )
    try:
        request = {scope: value, "metrics": list(VITALS)}
        if device != "ALL":
            request["formFactor"] = device
        connection.request(
            "POST",
            "/v1/records:queryRecord",
            json.dumps(request),
            {"Content-Type": "application/json", "X-Goog-Api-Key": key},
        )
        response = connection.getresponse()
        if response.status == 404:
            return None
        if response.status != 200:
            raise ValueError(
                f"CrUX API returned HTTP {response.status}. Check the API key, API activation and quota."
            )
        return json.loads(response.read(200_000))["record"]
    finally:
        connection.close()


def measure_vitals(db, urls, device, fallback):
    key = os.environ.get("FLIN_CRUX_API_KEY", "")
    if not key:
        raise ValueError(
            "Set the Google Chrome UX Report API key in the extension settings"
        )
    if json.loads(db.execute("SELECT config FROM job").fetchone()[0]).get(
        "staging_auth"
    ):
        raise ValueError(
            "CrUX looks up public URLs; this is an authenticated staging crawl"
        )
    if not 1 <= len(urls) <= 20:
        raise ValueError("Choose 1 to 20 crawled URLs per request")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='web_vitals'").fetchone():
        raise ValueError("Run a new crawl to collect this report")
    urls = list(dict.fromkeys(urls))
    for url in urls:
        if not db.execute(
            "SELECT 1 FROM pages WHERE url=? AND indexability IN ('indexable','canonicalised','noindex')",
            (url,),
        ).fetchone():
            raise ValueError("Choose an HTML URL successfully analysed in this crawl")
    results = []
    origins = {}
    for url in urls:
        scope = "url"
        record = crux_record(key, scope, url, device)
        if record is None and fallback:
            site = origin(url)
            if site not in origins:
                origins[site] = crux_record(key, "origin", site, device)
            record = origins[site]
            scope = "origin"
        data = {
            "url": url,
            "device": device,
            "scope": scope if record else "no_data",
            "measured_url": (url if scope == "url" else origin(url))
            if record
            else None,
            "lcp_ms": None,
            "inp_ms": None,
            "cls": None,
            "assessment": "insufficient_data",
            "period": None,
        }
        if record:
            data["measured_url"] = record.get("key", {}).get(
                scope, data["measured_url"]
            )
            data["period"] = json.dumps(record.get("collectionPeriod", {}))
            ratings = []
            for metric, (label, good, poor) in VITALS.items():
                p75 = (
                    record.get("metrics", {})
                    .get(metric, {})
                    .get("percentiles", {})
                    .get("p75")
                )
                if p75 is not None:
                    data[label] = float(p75)
                    ratings.append(
                        "good"
                        if float(p75) <= good
                        else "poor"
                        if float(p75) > poor
                        else "needs_improvement"
                    )
            if len(ratings) == 3:
                data["assessment"] = (
                    "pass" if all(r == "good" for r in ratings) else "fail"
                )
        results.append(data)
    db.executemany(
        """INSERT OR REPLACE INTO web_vitals(url,device,scope,measured_url,lcp_ms,inp_ms,cls,assessment,period)
        VALUES(:url,:device,:scope,:measured_url,:lcp_ms,:inp_ms,:cls,:assessment,:period)""",
        results,
    )
    return {"rows": results, "percentile": 75}


def rich_result_issues(nodes):
    """Check selected Google required fields, resolving local JSON-LD graph references."""
    by_id = {}
    for node in nodes:
        if isinstance(node.get("@id"), str):
            by_id.setdefault(node["@id"], {}).update(node)

    def resolve(value):
        if isinstance(value, dict) and isinstance(value.get("@id"), str):
            return by_id.get(value["@id"], {}) | value
        return value if isinstance(value, dict) else {}

    def present(value):
        if isinstance(value, str):
            return bool(value.strip())
        return value is not None and value != [] and value != {} and value is not False

    def has(obj, key):
        return present(obj.get(key))

    for index, raw in enumerate(nodes):
        node = resolve(raw)
        kinds = raw.get("@type", [])
        for name in kinds if isinstance(kinds, list) else [kinds]:
            if not isinstance(name, str):
                continue
            name = name.rsplit("/", 1)[-1]
            missing = []
            if name == "Product":
                if not has(node, "name"):
                    missing.append("name")
                if not any(
                    has(node, k) for k in ("offers", "review", "aggregateRating")
                ):
                    missing.append("offers | review | aggregateRating")
                offers = node.get("offers", [])
                for j, offer in enumerate(
                    offers if isinstance(offers, list) else [offers]
                ):
                    obj = resolve(offer)
                    if obj.get("@type") == "Offer" and not (
                        has(obj, "price")
                        or has(resolve(obj.get("priceSpecification")), "price")
                    ):
                        missing.append(f"offers[{j}].price | priceSpecification.price")
                    if obj.get("@type") == "AggregateOffer":
                        missing.extend(
                            f"offers[{j}].{k}"
                            for k in ("lowPrice", "priceCurrency")
                            if not has(obj, k)
                        )
            elif name == "Recipe":
                missing = [k for k in ("name", "image") if not has(node, k)]
            elif name == "BreadcrumbList":
                items = node.get("itemListElement", [])
                if not isinstance(items, list) or len(items) < 2:
                    missing.append("itemListElement (at least two items)")
                if isinstance(items, list):
                    positions = [resolve(item).get("position") for item in items]
                    numeric = [
                        int(v)
                        for v in positions
                        if isinstance(v, (str, int))
                        and str(v).isascii()
                        and str(v).isdigit()
                        and len(str(v)) <= 8
                    ]
                    last = max(numeric, default=len(items))
                    for j, item in enumerate(items):
                        obj = resolve(item)
                        pos = obj.get("position")
                        if (
                            not isinstance(pos, (str, int))
                            or not str(pos).isascii()
                            or not str(pos).isdigit()
                            or len(str(pos)) > 8
                            or int(pos) < 1
                        ):
                            missing.append(f"itemListElement[{j}].position")
                        elif int(pos) != last and not has(obj, "item"):
                            missing.append(f"itemListElement[{j}].item")
                        if not has(obj, "name") and not has(
                            resolve(obj.get("item")), "name"
                        ):
                            missing.append(f"itemListElement[{j}].name")
            for path in missing:
                yield (
                    "rich_result_required_field",
                    "medium",
                    f"{name} ({node.get('@id') or index}): {path}",
                )
