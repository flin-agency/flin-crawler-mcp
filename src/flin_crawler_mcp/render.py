"""Render public pages in an isolated browser, supplying GET responses via the crawl client."""

import os
import shutil
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright

from .network import USER_AGENT, url_for


def installed_browser():
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        str(
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Google/Chrome/Application/chrome.exe"
        ),
        str(
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe"
        ),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


class Renderer:
    def __init__(self, client):
        self.client = client
        self.stack = ExitStack()

    def __enter__(self):
        try:
            driver = self.stack.enter_context(sync_playwright())
            self.browser = driver.chromium.launch(
                executable_path=installed_browser(),
                headless=True,
                chromium_sandbox=True,
                args=[
                    "--proxy-server=http://127.0.0.1:9",
                    "--proxy-bypass-list=<-loopback>",
                    "--disable-background-networking",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            self.stack.callback(self.browser.close)
            return self
        except Exception as exc:
            self.stack.close()
            raise RuntimeError(
                f"Cannot start Chrome/Chromium. Install Google Chrome or run python -m playwright install chromium. {str(exc)[:400]}"
            ) from exc

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def html(self, url, document, wait_ms):
        context = self.browser.new_context(
            service_workers="block", accept_downloads=False, user_agent=USER_AGENT
        )
        try:
            page = context.new_page()
            page.set_default_timeout(15000)
            context.route_web_socket("**/*", lambda ws: ws.close())
            remaining = 100
            failed = 0
            used_document = False

            def intercept(route):
                nonlocal remaining, failed, used_document
                req = route.request
                target = url_for(req.url)
                if (
                    req.method != "GET"
                    or not target
                    or remaining <= 0
                    or req.resource_type in ("font", "media", "image")
                ):
                    route.abort()
                    return
                remaining -= 1
                try:
                    if req.is_navigation_request():
                        if (
                            used_document
                            or target != url
                            or req.frame != page.main_frame
                        ):
                            route.abort()
                            return
                        used_document = True
                        reply = document
                    else:
                        rules, _ = self.client.policy(target)
                        if not rules.allows(target):
                            route.abort()
                            return
                        reply = self.client.get(target)
                    # Follow subresource redirects only through another validated GET.
                    for _ in range(5):
                        if reply.status not in (301, 302, 303, 307, 308):
                            break
                        location = reply.headers.get("location")
                        target = url_for(location, target) if location else None
                        if not target or not self.client.policy(target)[0].allows(
                            target
                        ):
                            raise ValueError("Unavailable subresource redirect")
                        reply = self.client.get(target)
                    if 300 <= reply.status < 400 or reply.truncated:
                        raise ValueError("Incomplete subresource")
                    headers = {
                        k: v
                        for k, v in reply.headers.items()
                        if k
                        not in {
                            "content-length",
                            "content-encoding",
                            "transfer-encoding",
                            "connection",
                            "set-cookie",
                            "location",
                            "content-disposition",
                        }
                    }
                    route.fulfill(status=reply.status, headers=headers, body=reply.body)
                except Exception:
                    failed += 1
                    route.abort()

            context.route("**/*", intercept)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            if len(html.encode()) > 5 * 1024 * 1024:
                raise ValueError("Rendered DOM exceeds 5 MiB")
            return html, {
                "failed_resources": failed,
                "resource_limit_reached": remaining == 0,
            }
        finally:
            context.close()
