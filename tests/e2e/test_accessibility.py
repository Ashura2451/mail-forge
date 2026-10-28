"""Automated WCAG A/AA smoke checks for the actual local console routes."""

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


def test_console_routes_have_no_axe_critical_or_serious_findings(tmp_path: Path) -> None:
    executable = _chromium_executable()
    if executable is None:
        pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to run E2E")
    repo = Path(__file__).parents[2]
    axe_source = repo / "web" / "node_modules" / "axe-core" / "axe.min.js"
    if not axe_source.is_file():
        pytest.skip("install web dependencies to run the axe audit")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "accessibility.sqlite3"
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
            axe = axe_source.read_text(encoding="utf-8")
            page.add_init_script(script=axe)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="Contacts", exact=True).focus()
            page.keyboard.press("Enter")
            assert page.get_by_role("heading", name="Contacts", exact=True).is_visible()
            page.get_by_label("List name").focus()
            page.keyboard.press("Tab")
            assert page.locator(":focus-visible").count() == 1
            page.get_by_role("button", name="Dashboard", exact=True).click()
            for label in (
                "Dashboard",
                "Contacts",
                "Message Studio",
                "Campaigns",
                "Automations",
                "Inbound",
                "Providers",
                "Activity",
            ):
                if label != "Dashboard":
                    page.get_by_role("button", name=label).click()
                result = page.evaluate(
                    """async () => await axe.run(document, {
                        runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}
                    })"""
                )
                serious = [
                    {
                        "id": item["id"],
                        "nodes": [node["html"] for node in item["nodes"]],
                    }
                    for item in result["violations"]
                    if item["impact"] in {"critical", "serious"}
                ]
                assert not serious, f"{label} has serious accessibility findings: {serious}"
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=5)
