"""Real-browser acceptance for the packaged local operator console."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            assert page.locator(".rail").evaluate("e => e.getBoundingClientRect().width") == 68
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
            reopened.get_by_role("button", name="Approve").click()
            reopened.get_by_text("Review approved").wait_for()
            assert reopened.get_by_text("No inbound actions need review.").is_visible()

            fresh_context = browser.new_context(viewport={"width": 390, "height": 844})
            fresh_page = fresh_context.new_page()
            fresh_page.goto(base, wait_until="networkidle")
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
