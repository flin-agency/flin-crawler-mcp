# flin SEO Crawler

Ask Claude Desktop to crawl a website, then inspect the URLs and source links behind each finding. The crawler runs locally and uses its own HTTP client and optional Chrome/Chromium rendering. It requires no crawler licence, API key or hosted service.

## Install in Claude Desktop

Open the **flin-crawler-mcp-0.2.0.mcpb** file in Claude Desktop and select **Install**. Use a current Claude Desktop version with UV extension support. On first launch Claude downloads Python and the Python dependencies. For rendered crawls, install Google Chrome or use Playwright Chromium. The crawler uses an isolated browser profile.

After a release, download the extension from [GitHub Releases](https://github.com/flin-agency/flin-crawler-mcp/releases). To build the same extension from a checkout, run `python3 scripts/build_desktop.py`. The file appears in `dist/`.

Alternatively, after PyPI publication use this Claude Desktop developer configuration. If Claude cannot locate uvx, use the absolute path returned by `which uvx` or `where uvx`.

```json
{
  "mcpServers": {
    "flin-seo-crawler": {
      "command": "uvx",
      "args": ["flin-crawler-mcp"]
    }
  }
}
```

## Ask Claude

- «Crawle https://example.org mit maximal 300 URLs. Zeige mir die fünf wichtigsten technischen SEO-Probleme mit Beispielen.»
- «Welche Seiten verlinken auf eine 404-URL? Zeig mir Quellseite, Ziel und Ankertext.»
- «Rendere die Produktseiten mit JavaScript. Prüfe Canonicals, H1 und die im DOM vorhandenen Schema.org-Typen.»
- «Vergleiche diesen Crawl mit dem vorherigen und exportiere die aktuellen Findings als CSV.»

| Tool | What it returns |
| --- | --- |
| `start_crawl` | Background job ID. Spider mode follows links. List mode checks the seed and supplied URLs only. |
| `crawl_status` | Progress, completion state, coverage limit, issue counts and configuration. |
| `crawl_results` | Filterable, paginated pages, findings or link evidence. Filter links by target to get inlinks. |
| `export_crawl` | Full or filtered CSV file, row count and local path. |
| `compare_crawls` | Issue changes or granular old/new SEO values and link changes. |
| `group_crawl_issues` | Distinct affected URLs and a representative example for each group. |
| `check_web_vitals` | On-demand measured Core Web Vitals from CrUX. |
| `schedule_crawl`, `crawl_schedules`, `set_schedule_enabled` | Create, inspect and pause recurring audits and access completion notifications. |
| `list_crawls` | Recent local jobs across Desktop restarts. |
| `stop_crawl` | Cancellation at the next request boundary. Collected results remain available. |

## Checks and coverage

The crawler captures HTTP status and response times, redirect targets, chains and loops, broken internal links and linked resources, titles, descriptions, H1/H2, word counts, missing alt attributes, canonical targets, robots directives, hreflang return links, JSON-LD types and syntax errors. It groups exact duplicates on indexable pages. Link reports include the source, target, relation, anchor and nofollow flag. Custom CSS selectors extract text from up to 20 elements per selector. Crawl comparisons cover findings, SEO fields and link evidence.

Spider mode defaults to the seed hostname, supports explicitly included hosts, allows standard HTTP/HTTPS transitions, respects robots.txt and crawl delays, and discovers XML sitemaps and sitemap indexes. Seed the final hostname when a site switches between apex and www. External link checks are opt-in and do not expand the external site. Empty alt attributes are valid for decorative images. Technical indexability describes the crawl signals, not Google's index.

The default budget is 500 URLs, depth 8, with a 300 ms minimum interval per origin. Set `max_urls` up to 25,000. Linked assets and enabled external checks share that budget. HTML, sitemap and render responses are limited to 5 MiB. Sitemap discovery runs after linked pages; depth 0 on a sitemap URL describes its discovery as a seed. Source HTML is the default. In JavaScript mode, the browser loads public GET resources, waits for DOMContentLoaded and a configurable interval, and analyses the resulting DOM. Images, fonts, media, POST requests, service workers, WebSockets and additional navigations are excluded from rendering. The crawler operates independently of Search Console.

## Audit reports

| Report | What to ask Claude |
| --- | --- |
| Grouped issues | «Fasse kaputte Links nach gemeinsamem Ziel und Position im Footer oder Menü zusammen. Wie viele unterschiedliche Seiten sind betroffen?» |
| Release comparison | «Zeige mir pro URL den alten und den neuen Canonical und welche internen Links verschwunden sind.» Use `compare_crawls(view="fields")` for values on pages observed in both crawls. |
| JavaScript changes | «Welche Links kommen erst nach dem Rendering dazu?» Start with `render_js=true`, then query `render_changes` by field, e.g. `canonical`, `h1` or `link_added`. A render failure is recorded on the page. |
| International | «Zeige Konflikte zwischen HTML, HTTP-Headern und XML-Sitemaps für DE, FR und IT.» The `alternates` report retains language code, source channel, raw value and resolved target. Include language hosts explicitly via `additional_hosts`. |
| Similar content | «Welche deutschsprachigen Seiten verwenden fast denselben Haupttext?» The `similar_pages` report estimates five-word shingle overlap for indexable pages with at least 40 words. Scores start at 0.80. |
| Link opportunities | «Wo gibt es eine passende, bisher unverlinkte Textstelle für diese Zielseite?» Each `link_suggestions` row provides the actual anchor, excerpt and destination. The text must contain an unambiguous multi-word title or H1 and the page must have no link to the destination yet. |
| Rich results | «Welche Produkt-, Rezept- oder Breadcrumb-Auszeichnungen haben fehlende Pflichtfelder?» The `rich_result_required_field` issue identifies the JSON-LD type and property. Local checks cover selected Google-required properties and references within the document. |

Group issues by `cause`, `location`, `language`, `page_type` or `directory`. The source URL and HTML region are evidence for a possible common template. Define site-specific page types by path, for example `{"Produkt":"/de/produkte/"}`; the longest matching prefix wins. Supported schema types provide the default classification. Language comes from the HTML language or a recognised first path segment. Content matching uses the same language label, or the same host when both labels are unknown. It analyses at most 24,000 characters of main text per page, omits typical navigation and footers and uses bounded candidate matching. Results are review suggestions.

Version 0.1 crawls remain readable and support comparisons with their stored fields. New evidence such as DOM differences, exact HTML regions and link suggestions requires a new crawl.

## Optional specialist functions

**Core Web Vitals.** Enter a Chrome UX Report API key in the extension settings. Ask for the Core Web Vitals of up to 20 successfully crawled public HTML URLs per call. The tool sends the chosen URLs to Google and returns the device class, collection period, p75 LCP, INP and CLS and an assessment when all three are available. URL and origin-level measurements are labelled separately. Disable `origin_fallback` to restrict results to URL-level data. Missing field data is recorded explicitly. Read or export the `web_vitals` dataset afterwards. The [CrUX API documentation](https://developer.chrome.com/docs/crux/api/) describes how to enable the API.

**Authenticated staging.** Configure the exact HTTPS origin in Claude Desktop and enter HTTP Basic credentials or an existing session cookie. Start the audit with `staging_auth=true`. The crawler scopes credentials to that origin for robots.txt, page requests and browser resources. Interactive logins and MFA take place in the site's normal login flow before reusing a session cookie. Staging uses the usual destination restrictions. A VPN-only private address is outside the current network scope.

**URL parameters.** Exclude exact parameter names or prefixes, e.g. `exclude_parameters=["utm_*","sort"]`, and cap query variants per hostname and path with `max_query_variants_per_path=10`. A cap of zero skips every query variant. The original link targets remain available in `links`; `excluded_urls` explains skipped URLs (up to five times the URL budget).

**Recurring audits.** Ask «Wiederhole diesen Crawl jeden Montag um 08:30 Uhr in Europe/Zurich und benachrichtige mich nach Abschluss.» The schedule copies the crawl settings. One local dispatcher continues after Claude closes and coalesces missed slots into one run when the Mac wakes. After a reboot, reopening the extension restarts the dispatcher. Completion events include the crawl ID and the count of URLs with high-priority findings. Desktop banners on macOS and Linux depend on OS notification permissions; events are retained in `crawl_schedules`. Use `set_schedule_enabled` to pause or resume. A scheduled crawl never sends an email.

For a manual stdio installation, the optional settings use `FLIN_CRUX_API_KEY`, `FLIN_STAGING_ORIGIN`, `FLIN_STAGING_USER`, `FLIN_STAGING_PASSWORD` and `FLIN_STAGING_COOKIE`. Enter secrets in local client settings. Crawl configuration and schedules store the staging opt-in flag only.

## Local operation

Crawl data and CSV files are stored under `~/.flin-crawler-mcp/<crawl-id>/`. Set `FLIN_CRAWLER_DATA_DIR` to change the location. Background workers continue if Claude closes. Delete a completed crawl's folder to remove its data. The crawler connects to public websites. Loopback-only test/staging sites require the operator to set `FLIN_CRAWLER_ALLOW_LOOPBACK=1` in the server environment.

For development, install Python 3.11 or newer, then run:

```sh
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests -v
flin-crawler-mcp
```

## Releases

The package and Desktop extension share the version in `pyproject.toml` and `manifest.json`. Register a pending Trusted Publisher on PyPI for project `flin-crawler-mcp`, owner `flin-agency`, repository `flin-crawler-mcp`, workflow `release.yml`, environment `pypi`. Publish a GitHub Release tagged `v<version>`. The workflow tests Linux and macOS, checks the distributions and tag, publishes the wheel and sdist through OIDC, and attaches the MCPB to the GitHub Release. Pushes to main, pull requests and manual test runs build artifacts without publishing.
