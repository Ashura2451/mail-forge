"""Real-browser acceptance for the packaged local operator console."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def _chromium_executable() -> str | None:
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    if shutil.which("chromium"):
        return shutil.which("chromium")
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    matches = sorted(cache.glob("chromium-*/**/Google Chrome for Testing"))
    return str(matches[-1]) if matches else None


def _wait_for_server(port: int) -> None:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/session", timeout=0.25)
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("local Mailforge server did not start")


def test_local_operator_console_desktop_and_narrow(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "browser.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = (
        "import uvicorn; "
        f"from mailforge.interfaces.web import create_app; "
        f"uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), "
        f"host='127.0.0.1', port={port}, log_level='error')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    try:
        _wait_for_server(port)
        with playwright.sync_playwright() as sync:
            browser = sync.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            assert page.get_by_role("heading", name="Dashboard").is_visible()

            page.goto(f"http://127.0.0.1:{port}/campaigns", wait_until="networkidle")
            assert page.get_by_role("heading", name="Campaigns").is_visible()
            page.get_by_role("button", name="Reports & Insights").click()
            assert page.url.endswith("/reports")
            page.go_back(wait_until="networkidle")
            assert page.get_by_role("heading", name="Campaigns").is_visible()
            page.go_forward(wait_until="networkidle")
            assert page.locator("h1").filter(has_text="Reports & Insights").is_visible()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

            page.get_by_role("button", name="Message Studio").click()
            page.get_by_role("button", name="+ New message").click()
            page.get_by_role("heading", name="Compose message").wait_for()
            page.get_by_label("Subject").fill("Browser studio subject")
            page.get_by_label("Paragraph").fill("Hello {{ name }}, this is a browser studio draft.")
            page.get_by_role("button", name="Preview canonical output").click()
            page.get_by_text("Recipient preview rendered").wait_for()
            assert page.get_by_title("Isolated message preview").is_visible()
            page.get_by_role("button", name="Save draft").click()
            page.get_by_text("Message draft saved").wait_for()
            page.get_by_role("button", name="Publish revision").click()
            page.get_by_text("Message revision published").wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.get_by_role("heading", name="Compose message").is_visible()
            page.set_viewport_size({"width": 1440, "height": 1000})

            page.get_by_role("button", name="Contacts").click()
            page.get_by_label("List name").fill("Browser acceptance audience")
            page.get_by_role("button", name="+ New list").click()
            page.get_by_text("Browser acceptance audience").wait_for()
            page.locator("input[type=file]").set_input_files(
                {
                    "name": "audience.txt",
                    "mimeType": "text/plain",
                    "buffer": (
                        b"alice@example.com\nbob@example.com\nalice@example.com\nnot-an-email"
                    ),
                }
            )
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("2 accepted").wait_for()
            page.get_by_role("button", name="Browser acceptance audience").click()
            page.get_by_role("button", name="Import to selected list").click()
            page.get_by_text("Contact records").wait_for()

            page.get_by_role("button", name="Campaigns").click()
            page.get_by_label("Audience list").select_option(label="Browser acceptance audience")
            page.get_by_label("Campaign name").fill("Browser synthetic campaign")
            page.get_by_role("button", name="Create campaign").click()
            page.get_by_role("button", name="Prepare").click()
            page.get_by_text("Campaign prepared").wait_for()
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("Campaign preview loaded").wait_for()
            page.get_by_role("button", name="Authorize").click()
            page.get_by_text("Simulation authorized").wait_for()
            page.get_by_role("button", name="Run synthetic").click()
            page.get_by_text("Synthetic worker started").wait_for()
            page.wait_for_timeout(500)
            assert page.get_by_text("Browser synthetic campaign").is_visible()

            for label in ("Inbound", "Providers", "Activity"):
                page.get_by_role("button", name=label).click()
                assert page.get_by_role("heading", name=label).is_visible()

            page.get_by_role("button", name="Reports & Insights").click()
            page.get_by_role("heading", name="Reports & Insights", exact=True).first.wait_for()
            page.get_by_text("Evidence boundary:").wait_for()
            page.get_by_text("Submission attempts").wait_for()
            page.get_by_role("button", name="Campaign outcomes").click()
            page.get_by_role("link", name="Export CSV").wait_for()
            page.get_by_text("Browser synthetic campaign").wait_for()

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            assert page.locator(".rail").evaluate("e => e.getBoundingClientRect().right <= 0")
            menu = page.get_by_role("button", name="Open navigation")
            menu.click()
            page.locator(".nav-scrim").wait_for()
            maintenance = page.get_by_role("button", name="Maintenance")
            assert page.locator(".rail").evaluate("e => e.scrollHeight > e.clientHeight")
            maintenance.scroll_into_view_if_needed()
            assert maintenance.is_visible()
            assert page.locator(".rail").evaluate("e => e.scrollTop > 0")
            page.keyboard.press("Escape")
            assert menu.get_attribute("aria-expanded") == "false"
            assert page.evaluate(
                "document.activeElement === document.querySelector('.mobile-menu-button')"
            )
            assert page.locator(".rail").get_attribute("aria-hidden") == "true"
            assert (
                page.locator(".rail .nav-item", has_text="Maintenance").get_attribute("tabindex")
                == "-1"
            )
            page.get_by_role("button", name="Open navigation").click()
            page.get_by_role("button", name="Campaigns").click()
            page.get_by_label("Communication purpose").select_option("marketing")
            page.get_by_label("Audience list").select_option(label="Browser acceptance audience")
            page.get_by_label("Campaign name").fill("Browser mobile marketing campaign")
            page.get_by_role("button", name="Create draft").click()
            page.get_by_text("Campaign draft created").wait_for()
            campaign = page.evaluate(
                """async () => {
                    const response = await fetch('/api/v1/campaigns');
                    const campaigns = await response.json();
                    const item = campaigns.items.find(
                        entry => entry.name === 'Browser mobile marketing campaign'
                    );
                    const workspace = await fetch(`/api/v1/campaigns/${item.id}/workspace`);
                    return await workspace.json();
                }"""
            )
            assert campaign["campaign"]["configuration"]["communication_purpose"] == "marketing"
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_data_studio_browser_recipe_preview(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "data-studio-browser.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = (
        "import uvicorn; "
        "from mailforge.interfaces.web import create_app; "
        f"uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), "
        f"host='127.0.0.1', port={port}, log_level='error')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    try:
        _wait_for_server(port)
        with playwright.sync_playwright() as sync:
            browser = sync.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="Data Studio", exact=True).click()
            page.locator("input[aria-label='Data Studio source file']").set_input_files(
                {
                    "name": "contacts.csv",
                    "mimeType": "text/csv",
                    "buffer": (
                        b"email,first,last\n"
                        b"Alice@example.com,Alice,One\n"
                        b"alice@example.com,Alice,One\n"
                    ),
                }
            )
            page.get_by_role("button", name="Save current recipe").click()
            page.get_by_text("Recipe saved").wait_for()
            page.get_by_role("button", name="Preview source").click()
            page.get_by_text("Preview only").wait_for()
            assert page.get_by_text("1 removed").is_visible()
            page.locator("input[aria-label='Data Studio source file']").set_input_files(
                [
                    {
                        "name": "people.csv",
                        "mimeType": "text/csv",
                        "buffer": b"email\nada@example.com\n",
                    },
                    {
                        "name": "more-people.csv",
                        "mimeType": "text/csv",
                        "buffer": b"email\nbob@example.com\n",
                    },
                ]
            )
            page.get_by_label("Data Studio combine operation").select_option("append")
            page.get_by_label("Data Studio export format").select_option("csv")
            page.get_by_role("button", name="Save current recipe").click()
            page.get_by_text("Recipe revision saved").wait_for()
            page.get_by_role("button", name="Start processing").click()
            page.get_by_text("Processing job completed").wait_for()
            page.get_by_role("heading", name="Completed output").wait_for()
            assert page.get_by_role("link", name="Download data-studio-output.csv").is_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.get_by_role("heading", name="Source and recipe").is_visible()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_browser_closure_and_inbound_review_survive_service_runtime(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "browser-closure.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = (
        "import uvicorn; "
        "from mailforge.interfaces.web import create_app; "
        f"uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), "
        f"host='127.0.0.1', port={port}, log_level='error')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    try:
        _wait_for_server(port)
        with playwright.sync_playwright() as sync:
            browser = sync.chromium.launch(headless=True, executable_path=executable)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            base = f"http://127.0.0.1:{port}/"
            page.goto(base, wait_until="networkidle")
            page.get_by_role("button", name="Contacts").click()
            page.get_by_label("List name").fill("Closure audience")
            page.get_by_role("button", name="+ New list").click()
            page.locator("input[type=file]").set_input_files(
                {
                    "name": "closure.txt",
                    "mimeType": "text/plain",
                    "buffer": b"\n".join(
                        f"person-{index}@example.com".encode() for index in range(8)
                    ),
                }
            )
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("8 accepted").wait_for()
            page.get_by_role("button", name="Closure audience").click()
            page.get_by_role("button", name="Import to selected list").click()
            page.get_by_role("button", name="Campaigns").click()
            page.get_by_label("Audience list").select_option(label="Closure audience")
            page.get_by_label("Campaign name").fill("Closure campaign")
            page.get_by_role("button", name="Create campaign").click()
            page.get_by_role("button", name="Prepare").click()
            page.get_by_text("Campaign prepared").wait_for()
            page.get_by_role("button", name="Preview").click()
            page.get_by_text("Campaign preview loaded").wait_for()
            preview = json.loads(page.locator(".preview-json").inner_text())
            message_id = preview["messages"][0]["message_id"]
            page.get_by_role("button", name="Authorize").click()
            page.get_by_role("button", name="Run synthetic").click()
            page.get_by_text("Synthetic worker started").wait_for()
            page.close()

            reopened = context.new_page()
            reopened.goto(base, wait_until="networkidle")
            reopened.get_by_role("button", name="Campaigns", exact=True).click()
            reopened.get_by_text("Closure campaign").wait_for()
            for _ in range(80):
                reopened.get_by_role("button", name="Refresh").click()
                if reopened.get_by_text("completed", exact=True).is_visible():
                    break
                reopened.wait_for_timeout(25)
            assert reopened.get_by_text("completed", exact=True).is_visible()

            reopened.get_by_role("button", name="Inbound").click()
            reopened.locator("input[type=file]").set_input_files(
                {
                    "name": "unsubscribe.eml",
                    "mimeType": "message/rfc822",
                    "buffer": (
                        "From: person-0@example.com\n"
                        "To: sender@example.com\n"
                        "Subject: unsubscribe\n"
                        f"In-Reply-To: {message_id}\n\nPlease unsubscribe me\n"
                    ).encode(),
                }
            )
            reopened.get_by_role("button", name="Import evidence for review").click()
            reopened.get_by_text("Inbound evidence acquired").wait_for()
            reopened.get_by_text("create_unsubscribe").wait_for()
            reopened.get_by_label("Search inbound messages").fill("unsubscribe")
            reopened.get_by_role("button", name="Search").click()
            message_row = reopened.locator('tr[tabindex="0"]').first
            message_row.click()
            reopened.get_by_text("Attachments: 0").wait_for()
            reopened.get_by_role("button", name="Prepare reply draft").click()
            reopened.get_by_text("Reply draft prepared in Message Studio").wait_for()
            reopened.get_by_role("button", name="Approve").click()
            reopened.get_by_text("Review approved").wait_for()
            assert reopened.get_by_text("No inbound actions need review.").is_visible()

            fresh_context = browser.new_context(viewport={"width": 390, "height": 844})
            fresh_page = fresh_context.new_page()
            fresh_page.goto(base, wait_until="networkidle")
            fresh_page.get_by_role("button", name="Open navigation").click()
            fresh_page.get_by_role("button", name="Inbound").click()
            assert fresh_page.get_by_text("No inbound actions need review.").is_visible()
            fresh_context.close()
            context.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_browser_visual_automation_revision_lifecycle(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "automation-browser.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = (
        "import uvicorn; "
        "from mailforge.interfaces.web import create_app; "
        f"uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), "
        f"host='127.0.0.1', port={port}, log_level='error')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    try:
        _wait_for_server(port)
        with playwright.sync_playwright() as sync:
            browser = sync.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="Automations", exact=True).click()
            page.get_by_label("Automation name").fill("Browser visual automation")
            page.get_by_role("button", name="+ New automation").click()
            page.get_by_role("heading", name="Browser visual automation").wait_for()
            page.get_by_role("button", name="Open →").click()
            page.get_by_role("button", name="Save draft").click()
            page.get_by_text("Draft saved").wait_for()
            page.get_by_role("button", name="Validate").click()
            page.get_by_text("Workflow validated").wait_for()
            page.get_by_role("button", name="Publish").click()
            page.get_by_text("Revision published").wait_for()
            page.get_by_role("button", name="Activate").click()
            page.get_by_text("Workflow activated").wait_for()
            assert page.get_by_text("active", exact=True).is_visible()
            page.get_by_role("button", name="Dashboard", exact=True).click()
            page.get_by_role("button", name="Automations", exact=True).click()
            page.get_by_text("Browser visual automation", exact=True).wait_for()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_browser_unified_workspace_operational_controls(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    received: list[tuple[dict[str, str], bytes]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(size)
            received.append(({key.lower(): value for key, value in self.headers.items()}, body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    database = tmp_path / "unified-workspace.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = (
        "import uvicorn; "
        "from mailforge.interfaces.web import create_app; "
        f"uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), "
        f"host='127.0.0.1', port={port}, log_level='error')"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    environment["MAILFORGE_ENABLE_LOCAL_FIXTURES"] = "1"
    environment["MAILFORGE_WEBHOOK_SECRET"] = "browser-qualification-secret"
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    try:
        _wait_for_server(port)
        with playwright.sync_playwright() as sync:
            browser = sync.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            base = f"http://127.0.0.1:{port}/"

            page.goto(base + "integrations", wait_until="networkidle")
            page.get_by_role("button", name="Issue scoped client").click()
            token = page.locator(".one-time-token code").inner_text()
            page.get_by_label("Event bearer token").fill(token)
            page.get_by_role("button", name="Send unauthorized event").click()
            alert = page.get_by_role("alert")
            alert.wait_for()
            assert "bearer" in alert.inner_text().lower()
            page.get_by_role("button", name="Dismiss error").click()
            page.get_by_role("button", name="Send authorized event").click()
            page.locator(".preview-box").filter(has_text="accepted").wait_for()
            page.get_by_role("button", name="Repeat event").click()
            page.locator(".preview-box").filter(has_text="duplicate").wait_for()

            endpoint = f"http://127.0.0.1:{receiver.server_port}/mailforge-hook"
            page.get_by_label("Webhook endpoint").fill(endpoint)
            page.get_by_role("button", name="Register subscription").click()
            page.get_by_text("Webhook subscription saved").wait_for()
            page.get_by_role("button", name="Preview payload").click()
            page.locator(".preview-box").filter(has_text="contact_imported").wait_for()
            page.get_by_role("button", name="Enable").click()
            page.get_by_text("Webhook enabled").wait_for()

            page.get_by_role("button", name="Revoke").click()
            page.get_by_text("Scoped client revoked").wait_for()
            page.get_by_role("button", name="Repeat event").click()
            alert = page.get_by_role("alert")
            alert.wait_for()
            assert "revoked" in alert.inner_text().lower()
            page.get_by_role("button", name="Dismiss error").click()

            page.get_by_role("button", name="Contacts").click()
            page.get_by_label("List name").fill("Integration trigger audience")
            page.get_by_role("button", name="+ New list").click()
            page.get_by_text("Integration trigger audience").wait_for()
            page.locator("input[type=file]").set_input_files(
                {
                    "name": "integration-trigger.txt",
                    "mimeType": "text/plain",
                    "buffer": b"integration@example.com\n",
                }
            )
            page.get_by_role("button", name="Integration trigger audience").click()
            page.get_by_role("button", name="Import to selected list").click()
            page.get_by_text("Contacts imported").wait_for()
            page.get_by_role("button", name="Integration Hub").click()
            page.get_by_role("button", name="Process due deliveries").click()
            page.get_by_text("Due webhook deliveries processed").wait_for()
            for _ in range(40):
                if received:
                    break
                page.wait_for_timeout(50)
            assert received, "the enabled loopback webhook did not receive a delivery"
            headers, body = received[0]
            timestamp = headers["x-mailforge-timestamp"]
            expected = hmac.new(
                b"browser-qualification-secret",
                f"{timestamp}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            assert headers["x-mailforge-signature"] == f"v1={expected}"
            assert headers["x-mailforge-event-id"].startswith("operational:")

            source = tmp_path / "disposable.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE contacts (email TEXT, plan TEXT)")
                connection.execute(
                    "INSERT INTO contacts VALUES (?, ?)",
                    ("alice@example.com", "pro"),
                )
            page.get_by_role("button", name="Data Connections", exact=True).click()
            page.get_by_label("Data source name").fill("Disposable browser source")
            page.get_by_label("Data source file").set_input_files(str(source))
            page.get_by_role("button", name="Save source").click()
            page.get_by_text("Data source saved").wait_for()
            page.get_by_role("button", name="Preview 50 rows").click()
            page.get_by_text("alice@example.com").wait_for()
            page.get_by_role("button", name="Create snapshot").click()
            page.get_by_text("Snapshot extraction started").wait_for()
            page.get_by_text("1 rows").wait_for()

            page.get_by_role("button", name="Data Studio", exact=True).click()
            page.get_by_role("button", name="Save current recipe").click()
            page.get_by_text("Recipe saved").wait_for()
            recipe_revision = page.evaluate(
                """async () => (await (await fetch('/api/v1/data-studio/recipes')).json())
                .items[0].current_revision_id"""
            )
            assert recipe_revision
            page.get_by_role("button", name="Data Connections", exact=True).click()
            page.get_by_role("button", name="Disposable browser source").click()
            page.get_by_label("Data Studio recipe revision").fill(recipe_revision)
            page.get_by_role("button", name="Open in Data Studio").click()
            page.get_by_text("Snapshot handed to Data Studio").wait_for()

            page.get_by_role("button", name="Email Standards").click()
            page.get_by_role("button", name="Save identity").click()
            page.get_by_text("Sender identity saved").wait_for()
            page.get_by_role("button", name="Inspect domain").click()
            page.get_by_text("Domain diagnostics recorded").wait_for()
            page.get_by_role("button", name="Validate message").click()
            page.get_by_text("Message validation completed").wait_for()
            page.get_by_role("button", name="Save endpoint").click()
            page.get_by_text("Unsubscribe endpoint recorded").wait_for()
            page.get_by_role("button", name="Check marketing readiness").click()
            page.get_by_text("Unsubscribe readiness evaluated").wait_for()
            page.get_by_text("one_click_not_fully_verified").wait_for()

            page.get_by_role("button", name="Maintenance").click()
            page.get_by_label("Application timezone").fill("America/New_York")
            page.get_by_role("button", name="Save application preferences").click()
            page.get_by_text("Application preferences saved").wait_for()
            page.get_by_role("button", name="Reports & Insights").click()
            page.get_by_role("button", name="Maintenance").click()
            timezone = page.get_by_label("Application timezone")
            for _ in range(40):
                if timezone.input_value() == "America/New_York":
                    break
                page.wait_for_timeout(50)
            assert timezone.input_value() == "America/New_York"
            page.reload(wait_until="networkidle")
            assert page.get_by_label("Application timezone").input_value() == "America/New_York"
            assert page.get_by_label("Keep synthetic simulation mode enabled").is_checked()
            assert not page.get_by_label("Allow sample data during onboarding").is_checked()
            page.get_by_role("button", name="Create backup").click()
            page.get_by_text("Application backup created").wait_for()
            backup = page.locator(".list-row").filter(has_text="verified").first
            backup.click()
            page.get_by_label("Restore destination").fill("browser-restore")
            page.get_by_role("button", name="Restore selected backup").click()
            page.get_by_text("Backup restored into a staged destination").wait_for()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=5)
