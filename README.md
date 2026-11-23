# flin SEO Crawler

Ask Claude Desktop to crawl a website, then inspect the URLs and source links behind each finding. The crawler runs locally and uses its own HTTP client and optional Chrome/Chromium rendering. It requires no crawler licence, API key or hosted service.

## Install in Claude Desktop

Open the **flin-crawler-mcp-0.1.0.mcpb** file in Claude Desktop and select **Install**. Use a current Claude Desktop version with UV extension support. On first launch Claude downloads Python and the Python dependencies. For rendered crawls, install Google Chrome or use Playwright Chromium. The crawler uses an isolated browser profile.

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
| `compare_crawls` | New and resolved issue types by URL, and URLs that were not rechecked. |
| `list_crawls` | Recent local jobs across Desktop restarts. |
| `stop_crawl` | Cancellation at the next request boundary. Collected results remain available. |

## Checks and coverage

The crawler captures HTTP status and response times, redirect targets, chains and loops, broken internal links and linked resources, titles, descriptions, H1/H2, word counts, missing alt attributes, canonical targets, robots directives, hreflang return links, JSON-LD types and syntax errors. It groups exact duplicates on indexable pages. Link reports include the source, target, relation, anchor and nofollow flag. Custom CSS selectors extract text from up to 20 elements per selector. Crawl comparisons cover issue types per URL.

Spider mode stays on the seed hostname, allows standard HTTP/HTTPS transitions, respects robots.txt and crawl delays, and discovers XML sitemaps and sitemap indexes. Seed the final hostname when a site switches between apex and www. External link checks are opt-in and do not expand the external site. Empty alt attributes are valid for decorative images. Technical indexability describes the crawl signals, not Google's index.

The default budget is 500 URLs, depth 8, with a 300 ms minimum interval per origin. Set `max_urls` up to 25,000. Linked assets and enabled external checks share that budget. HTML, sitemap and render responses are limited to 5 MiB. Sitemap discovery runs after linked pages; depth 0 on a sitemap URL describes its discovery as a seed. Source HTML is the default. In JavaScript mode, the browser loads public GET resources, waits for DOMContentLoaded and a configurable interval, and analyses the resulting DOM. Images, fonts, media, POST requests, service workers, WebSockets and additional navigations are excluded from rendering. Login flows, Search Console/Analytics integrations, Lighthouse scores, scheduled runs and visual crawl diagrams are outside the current feature set.

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
