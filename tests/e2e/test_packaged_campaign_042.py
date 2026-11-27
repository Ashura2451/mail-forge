from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from tests.e2e.test_browser_acceptance import (
    _ServerContext,
    _SMTPServer,
    _certificate,
    _chromium_executable,
    _wait_for_server,
)

playwright = pytest.importorskip("playwright.sync_api")


def test_packaged_operator_campaign_journey_survives_restart_without_duplicate_capture(
    tmp_path: Path,
) -> None:
    executable = _chromium_executable()
    packaged_launcher = os.environ.get("MAILFORGE_PACKAGED_LAUNCHER")
    if executable is None or not packaged_launcher:
        pytest.skip("requires Chromium and MAILFORGE_PACKAGED_LAUNCHER")

    repo = Path(__file__).parents[2]
    cert, key = _certificate(tmp_path, common_name="127.0.0.1", san="IP:127.0.0.1")
    smtp = _SMTPServer(cert, key, implicit_tls=True)
    data_dir = tmp_path / "packaged-campaign-data"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = os.environ.copy()
    environment.update(
        {
            "MAILFORGE_ENABLE_LOCAL_FIXTURES": "1",
            "MAILFORGE_LOCAL_CAPTURE_ENDPOINT": f"127.0.0.1:{smtp.server_address[1]}",
            "MAILFORGE_CAPTURE_PASSWORD": "synthetic-capture-password",
        }
    )
    launcher = Path(packaged_launcher).resolve()
    contents = launcher.parents[1]

    def start() -> None:
        result = subprocess.run(
            [str(launcher), "--data-dir", str(data_dir), "--port", str(port)],
            env={**environment, "BROWSER": "true"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        _wait_for_server(port)

    def stop() -> None:
        package_environment = {
            **environment,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHOME": str(contents / "Resources" / "python"),
            "PYTHONPATH": str(contents / "Resources" / "site-packages"),
        }
        result = subprocess.run(
            [
                str(contents / "Resources" / "python" / "bin" / "python3.15"),
                "-m",
                "mailforge",
                "app",
                "stop",
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
            ],
            env=package_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    with _ServerContext(smtp):
        start()
        try:
            with playwright.sync_playwright() as sync:
                browser = sync.chromium.launch(headless=True, executable_path=executable)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                base = f"http://127.0.0.1:{port}"
                page.goto(f"{base}/providers", wait_until="networkidle")
                page.get_by_label("Provider profile").select_option("local_capture_only_smtp")
                page.get_by_label("Account name").fill("campaign-capture")
                page.get_by_label("SMTP port").fill(str(smtp.server_address[1]))
                page.get_by_label("SMTP username").fill("sender@sandbox.example.com")
                page.get_by_label("Sender email").fill("sender@sandbox.example.com")
                page.get_by_text("Advanced storage settings").click()
                page.get_by_label("Credential reference").fill("env:MAILFORGE_CAPTURE_PASSWORD")
                page.get_by_label("CA file reference").fill(str(cert))
                page.get_by_label("Encryption").select_option("implicit_tls")
                page.get_by_role("button", name="Save account").click()
                page.get_by_text("Email account saved locally; no connection made").wait_for()

                page.get_by_role("button", name="Contacts").click()
                page.get_by_label("List name").fill("Packaged campaign audience")
                page.get_by_role("button", name="+ New list").click()
                page.locator("input[type=file]").set_input_files(
                    {
                        "name": "packaged-audience.txt",
                        "mimeType": "text/plain",
                        "buffer": b"one@sandbox.example.com\n",
                    }
                )
                page.get_by_role("button", name="Preview consequences").click()
                page.get_by_text("1 accepted").wait_for()
                page.get_by_role("button", name="Packaged campaign audience").click()
                page.get_by_role("button", name="Import to selected list").click()
                page.get_by_text("Contacts imported").wait_for()

                page.get_by_role("button", name="Send").click()
                page.get_by_label("Audience list").select_option(label="Packaged campaign audience")
                page.get_by_label("Provider account").select_option("campaign-capture")
                page.get_by_label("Campaign name").fill("Packaged operator campaign")
                page.get_by_role("button", name="Create draft").click()
                page.get_by_text("Campaign draft created").wait_for()
                page.get_by_role("button", name="Prepare snapshots").click()
                page.get_by_text("Campaign prepared").wait_for()
                page.get_by_role("button", name="Authorize synthetic").click()
                page.get_by_text("Synthetic execution authorized").wait_for()
                page.get_by_role("button", name="Run trusted loopback SMTP").click()
                page.get_by_text("Loopback campaign worker started.").wait_for()
                page.get_by_text("Completed successfully", exact=True).wait_for(timeout=10_000)
                assert len(smtp.received_messages) == 1
                page.get_by_role("button", name="Attempts").click()
                page.get_by_text("accepted", exact=True).wait_for()
                browser.close()
        finally:
            stop()

        start()
        try:
            with playwright.sync_playwright() as sync:
                browser = sync.chromium.launch(headless=True, executable_path=executable)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                page.goto(f"http://127.0.0.1:{port}/campaigns", wait_until="networkidle")
                page.get_by_text("Packaged operator campaign").wait_for()
                page.get_by_role("button", name="Packaged operator campaign").click()
                page.get_by_text("Completed successfully", exact=True).wait_for()
                page.get_by_role("button", name="Attempts").click()
                page.get_by_text("accepted", exact=True).wait_for()
                assert len(smtp.received_messages) == 1
                browser.close()
        finally:
            stop()
