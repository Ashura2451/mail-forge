"""Rendered acceptance for the direct-manipulation Mailforge workflows."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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


def test_direct_address_workspace_preserves_provider_and_row_selection(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/sort", wait_until="networkidle")
    page.get_by_label("Address file").set_input_files(
        {
            "name": "addresses.txt",
            "mimeType": "text/plain",
            "buffer": b"alice@gmail.com\nbob@example.com\ncarol@proton.me\nhello@notgmail.example\nalice@gmail.com",
        }
    )
    page.get_by_role("button", name="Sort file", exact=True).click()
    page.get_by_text("Completed", exact=True).wait_for(timeout=10_000)
    assert page.get_by_text("gmail", exact=True).first.is_visible()
    assert page.get_by_text("proton", exact=True).first.is_visible()
    assert page.get_by_text("Unknown host is not invalid").is_visible()

    page.get_by_role("button", name="None", exact=True).click()
    assert page.get_by_text("0 addresses", exact=True).is_visible()
    page.get_by_role("button", name="Choose rows", exact=True).click()
    page.get_by_role("checkbox", name="Select bob@example.com").check()
    assert page.get_by_text("1 address", exact=True).is_visible()
    page.get_by_role("button", name="Save exact selection", exact=True).click()
    page.get_by_role("textbox", name="List name").fill("One address")
    page.get_by_role("button", name="Save list", exact=True).click()
    page.get_by_role("button", name="More tools", exact=True).click()
    page.get_by_role("button", name="Saved lists", exact=True).click()
    page.get_by_text("One address", exact=True).wait_for()
    assert page.get_by_role("cell", name="1", exact=True).is_visible()


def test_compare_clears_stale_results_when_operation_changes(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/compare", wait_until="networkidle")
    page.get_by_label("Main comparison file").set_input_files(
        {"name": "main.txt", "mimeType": "text/plain", "buffer": b"alice@example.com\nbob@example.com"}
    )
    page.get_by_label("Reference comparison files").set_input_files(
        {"name": "reference.txt", "mimeType": "text/plain", "buffer": b"bob@example.com"}
    )
    page.get_by_role("button", name="Calculate result", exact=True).click()
    page.get_by_role("heading", name="Only in main", exact=True).wait_for()
    assert page.get_by_text("alice@example.com", exact=True).is_visible()
    page.get_by_role("combobox", name="Show").select_option("intersection")
    assert page.get_by_text("No result is active", exact=True).is_visible()
    assert not page.get_by_role("heading", name="Only in main", exact=True).is_visible()


def test_compose_is_independent_and_shows_exact_pasted_scope(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/send", wait_until="networkidle")
    page.get_by_label(re.compile("Paste addresses")).fill("alice@example.com\nbob@example.com")
    page.get_by_label("Subject").fill("Direct HTML test")
    page.get_by_label(re.compile("HTML message")).fill("<p>Hello {{ email }}</p>")
    page.get_by_role("button", name="Review exact send", exact=True).click()
    page.get_by_text("2 recipients", exact=True).wait_for()
    assert page.get_by_text("Direct HTML test", exact=True).is_visible()
    assert page.get_by_role("button", name="Send 2 emails", exact=True).is_disabled()
    assert not page.get_by_text("Operation controls", exact=True).is_visible()
    assert page.get_by_title("Rendered email preview").is_visible()


def test_compose_accepts_complete_html_document_metadata_and_assets(local_page) -> None:
    page, base = local_page
    asset_zip = BytesIO()
    with ZipFile(asset_zip, "w", ZIP_DEFLATED) as archive:
        archive.writestr("logo.png", b"\x89PNG\r\n\x1a\nfixture")
    page.goto(f"{base}/send", wait_until="networkidle")
    page.get_by_label(re.compile("Paste addresses")).fill("alice@example.com")
    page.get_by_label("Subject").fill("Complete HTML document")
    page.get_by_label("HTML file").set_input_files(
        {
            "name": "newsletter.html",
            "mimeType": "text/html",
            "buffer": (
                b"<!doctype html><html><head><meta charset='utf-8'>"
                b"<meta name='viewport' content='width=device-width, initial-scale=1'>"
                b"<meta http-equiv='refresh' content='0;url=https://unsafe.invalid'>"
                b"<style>.hero{color:#123456}</style></head><body>"
                b"<div class='hero'><h1>Hello {{ name }}</h1>"
                b"<img src='logo.png' alt='Logo'></div></body></html>"
            ),
        }
    )
    page.get_by_label("HTML asset bundle").set_input_files(
        {
            "name": "assets.zip",
            "mimeType": "application/zip",
            "buffer": asset_zip.getvalue(),
        }
    )
    page.get_by_role("button", name="Review exact send", exact=True).click()
    page.get_by_text("1 recipient", exact=True).wait_for()
    page.get_by_text("Message adjusted for email compatibility", exact=True).click()
    assert page.get_by_text(re.compile("UTF-8 document metadata was normalized")).is_visible()
    assert page.get_by_title("Rendered email preview").is_visible()


def test_sorted_result_handoff_selects_the_transferred_list_in_compare(local_page) -> None:
    page, base = local_page
    page.goto(f"{base}/sort", wait_until="networkidle")
    page.get_by_label("Address file").set_input_files(
        {"name": "handoff.txt", "mimeType": "text/plain", "buffer": b"alice@example.com\nbob@example.com"}
    )
    page.get_by_role("button", name="Sort file", exact=True).click()
    page.get_by_text("Completed", exact=True).wait_for(timeout=10_000)
    page.get_by_role("button", name="Compare", exact=True).last.click()
    page.wait_for_url("**/compare?list_id=*")
    page.get_by_role("combobox", name="Saved list").wait_for()
    assert page.get_by_role("combobox", name="Saved list").input_value()
    page.get_by_role("button", name="Calculate result", exact=True).click()
    page.get_by_role("heading", name="Only in main", exact=True).wait_for(timeout=5_000)
    assert page.get_by_text("alice@example.com", exact=True).is_visible()


def test_narrow_navigation_has_keyboard_dismissal(local_page) -> None:
    page, base = local_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/", wait_until="networkidle")
    menu = page.get_by_role("button", name="Open navigation")
    menu.click()
    assert page.get_by_role("button", name="Compare", exact=True).is_visible()
    page.keyboard.press("Escape")
    assert page.get_by_role("button", name="Open navigation").get_attribute("aria-expanded") == "false"
