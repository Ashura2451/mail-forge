"""Packaged qualification for the rationalized operator journeys."""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tests.e2e.test_browser_acceptance import _chromium_executable, _wait_for_server
from tests.test_provider_interoperability import _certificate, _ServerContext, _SMTPServer

playwright = pytest.importorskip("playwright.sync_api")


def test_packaged_send_uses_controlled_smtp_and_does_not_duplicate_after_restart(
    tmp_path: Path,
) -> None:
    executable = _chromium_executable()
    packaged_launcher = os.environ.get("MAILFORGE_PACKAGED_LAUNCHER")
    if executable is None or not packaged_launcher:
        pytest.skip("requires Chromium and MAILFORGE_PACKAGED_LAUNCHER")

    cert, key = _certificate(tmp_path, common_name="127.0.0.1", san="IP:127.0.0.1")
    smtp = _SMTPServer(cert, key, implicit_tls=True)
    data_dir = tmp_path / "packaged-operator-data"
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

    def register_capture(page, base: str) -> None:
        payload = {
            "ref": "capture",
            "display_name": "Local capture fixture",
            "sender_email": "sender@sandbox.example.com",
            "kind": "smtp",
            "host": "127.0.0.1",
            "port": smtp.server_address[1],
            "username": "sender@sandbox.example.com",
            "credential_ref": "env:MAILFORGE_CAPTURE_PASSWORD",
            "tls_mode": "implicit_tls",
            "auth_method": "password",
            "ca_file": str(cert),
            "pacing_per_minute": 60,
            "max_concurrency": 1,
            "sandbox_key": "local_capture_only",
        }
        response = page.evaluate(
            """async ({path, payload}) => {
                const csrf = document.cookie.split('; ').find(item => item.startsWith('mailforge_csrf='))?.split('=')[1] || '';
                const response = await fetch(path, {method: 'POST', headers: {'content-type': 'application/json', 'x-mailforge-csrf': decodeURIComponent(csrf)}, body: JSON.stringify(payload)});
                return {status: response.status, body: await response.json()};
            }""",
            {"path": f"{base}/api/v1/providers", "payload": payload},
        )
        assert response["status"] == 200, response

    with _ServerContext(smtp):
        start()
        try:
            with playwright.sync_playwright() as sync:
                browser = sync.chromium.launch(headless=True, executable_path=executable)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                base = f"http://127.0.0.1:{port}"
                page.goto(f"{base}/send", wait_until="networkidle")
                register_capture(page, base)
                page.reload(wait_until="networkidle")
                page.get_by_label("Paste addresses", exact=False).fill(
                    "audience@sandbox.example.com"
                )
                page.get_by_label("Sender email").fill("sender@sandbox.example.com")
                page.get_by_label("Subject").fill("Packaged qualification")
                html_file = tmp_path / "complete-message.html"
                html_file.write_text(
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<meta http-equiv='refresh' content='0;url=https://unsafe.invalid'>"
                    "<style>.hero{color:#123456}</style></head><body>"
                    "<div class='hero'><p>Hello {{ email }}</p>"
                    "<img src='logo.png' alt='Logo'></div></body></html>",
                    encoding="utf-8",
                )
                asset_zip = tmp_path / "message-assets.zip"
                with ZipFile(asset_zip, "w", ZIP_DEFLATED) as archive:
                    archive.writestr("logo.png", b"\x89PNG\r\n\x1a\nfixture")
                page.get_by_label("HTML file").set_input_files(str(html_file))
                page.get_by_label("HTML asset bundle").set_input_files(str(asset_zip))
                page.get_by_label("Email account").select_option("capture")
                page.get_by_role("button", name="Review exact send").click()
                page.get_by_text("1 recipients").wait_for()
                page.get_by_text("HTML prepared safely").wait_for()
                page.get_by_label("Test recipient").fill("test@sandbox.example.com")
                page.get_by_role("button", name="Send test email").click()
                page.get_by_text("Result: provider_accepted").wait_for(timeout=15_000)
                assert len(smtp.received_messages) == 1
                page.get_by_role("button", name="Prepare exact scope").click()
                page.get_by_role("button", name="Authorize simulation").click()
                page.get_by_role("button", name="Start local simulation").click()
                page.get_by_text("completed", exact=True).wait_for(timeout=15_000)
                assert len(smtp.received_messages) == 2
                record = page.evaluate(
                    """async name => {
                        const campaigns = await (await fetch('/api/v1/campaigns')).json();
                        const item = campaigns.items.find(candidate => candidate.name.includes(name));
                        return item ? await (await fetch(`/api/v1/campaigns/${item.id}/workspace`)).json() : null;
                    }""",
                    "Packaged qualification",
                )
                assert record["campaign"]["status"] == "completed"
                assert record["progress"]["counts"]["accepted"] == 1
                assert record["preview"][0]["latest_outcome"] == "accepted"
                assert "test@sandbox.example.com" in smtp.received_messages[0].decode(
                    "utf-8", errors="replace"
                )
                assert "audience@sandbox.example.com" in smtp.received_messages[1].decode(
                    "utf-8", errors="replace"
                )
                browser.close()
        finally:
            stop()

        start()
        try:
            with playwright.sync_playwright() as sync:
                browser = sync.chromium.launch(headless=True, executable_path=executable)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                page.goto(f"http://127.0.0.1:{port}/history", wait_until="networkidle")
                page.get_by_role(
                    "row",
                    name=re.compile(
                        r"^Send · Packaged qualification Local record Send completed\b"
                    ),
                ).wait_for()
                assert len(smtp.received_messages) == 2
                browser.close()
        finally:
            stop()
