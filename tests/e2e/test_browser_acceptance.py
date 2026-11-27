"""Rendered acceptance for the focused Sort, Compare, and Send workflows."""

from __future__ import annotations

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


@pytest.fixture()
def local_page(tmp_path: Path):
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "browser.sqlite3"
    frontend = repo / "src" / "mailforge" / "web_assets"
    code = f"import uvicorn; from mailforge.interfaces.web import create_app; uvicorn.run(create_app(r'{database}', frontend_dir=r'{frontend}'), host='127.0.0.1', port={port}, log_level='error')"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "src")
    server = subprocess.Popen([sys.executable, "-c", code], env=environment)
    _wait_for_server(port)
    with playwright.sync_playwright() as sync:
        browser = sync.chromium.launch(headless=True, executable_path=executable)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        yield page, f"http://127.0.0.1:{port}"
        browser.close()
    server.terminate()
    server.wait(timeout=5)


def test_sort_compare_and_history_workflows(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/", wait_until="networkidle")
    assert page.get_by_role("heading", name="Home").is_visible()
    for label in ("Sort email", "Compare lists", "Send", "History"):
        assert page.get_by_role("button", name=label, exact=True).is_visible()

    page.get_by_role("button", name="Sort email", exact=True).click()
    page.get_by_label("Email sorter input file").set_input_files(
        {
            "name": "sort.txt",
            "mimeType": "text/plain",
            "buffer": b"ALICE@example.com\nalice@example.com\ninvalid\nbob@example.com",
        }
    )
    page.get_by_role("button", name="Preview", exact=True).click()
    page.get_by_text("Accepted").wait_for()
    assert page.get_by_text("Duplicates", exact=True).is_visible()

    page.get_by_role("navigation", name="Main navigation").get_by_role(
        "button", name="Compare lists", exact=True
    ).click()
    page.get_by_label("Main comparison list").set_input_files(
        {
            "name": "main.txt",
            "mimeType": "text/plain",
            "buffer": b"alice@example.com\nbob@example.com",
        }
    )
    page.get_by_label("Reference comparison lists").set_input_files(
        {"name": "old.txt", "mimeType": "text/plain", "buffer": b"bob@example.com"}
    )
    page.get_by_role("main").get_by_role("button", name="Compare lists", exact=True).click()
    page.get_by_role("heading", name="Comparison result").wait_for()
    assert page.get_by_text("alice@example.com").is_visible()

    page.get_by_role("button", name="History", exact=True).click()
    assert page.get_by_role("heading", name="History").is_visible()


def test_direct_send_accepts_pasted_addresses_and_html(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/send", wait_until="networkidle")
    page.get_by_label("Paste addresses").fill("alice@example.com\nbob@example.com")
    page.get_by_label("Sender email").fill("sender@example.com")
    page.get_by_label("Subject").fill("Direct HTML test")
    page.get_by_label("Plain-text message").fill("Hello {{ email }}")
    page.get_by_label("HTML message").fill("<p>Hello {{ email }}</p>")
    page.get_by_role("button", name="Review this send").click()
    page.get_by_text("Recipient set and message prepared").wait_for()
    page.get_by_role("button", name="Prepare exact scope").click()
    page.get_by_text("Send prepared").wait_for()


def test_narrow_navigation_keeps_labels_and_focus(local_page) -> None:
    page, base = local_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/", wait_until="networkidle")
    menu = page.get_by_role("button", name="Open navigation")
    menu.click()
    assert page.get_by_role("button", name="Compare lists", exact=True).is_visible()
    page.get_by_role("button", name="Compare lists", exact=True).click()
    assert page.get_by_role("heading", name="Compare lists").is_visible()
    page.get_by_role("button", name="Open navigation").click()
    page.keyboard.press("Escape")
    assert (
        page.get_by_role("button", name="Open navigation").get_attribute("aria-expanded") == "false"
    )
