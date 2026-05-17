"""Capture Phase 3 dashboard screenshots with Playwright."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_OUTPUT_DIR = ROOT / "dashboard" / "screenshots"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "mobile": {"width": 390, "height": 900},
}


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(url: str, process: subprocess.Popen, timeout_seconds: float = 15.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Dashboard server exited before it became ready.")
        try:
            response = requests.get(url, timeout=0.5)
            if response.status_code == 200:
                return
        except requests.RequestException:
            time.sleep(0.2)
    raise TimeoutError(f"Dashboard server did not become ready at {url}")


def capture(url: str, output: Path, viewport: dict[str, int]):
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception:
            browser = playwright.chromium.launch(channel="msedge")
        page = browser.new_page(viewport=viewport, device_scale_factor=1)
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(output), full_page=True)
        browser.close()


def prepare_phase3_demo_data(db_path: Path):
    from goals import SharedGoalStore

    store = SharedGoalStore(db_path)
    steps = store.list_steps(status="suggested")
    yoga_step = next(
        (
            step for step in steps
            if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
        ),
        None,
    )
    lucid_step = next(
        (
            step for step in steps
            if step["title"] == "Continue learning about LucidAI and their tech"
        ),
        None,
    )
    if yoga_step:
        store.accept_step(yoga_step["id"])
    if lucid_step:
        store.record_step_feedback(lucid_step["id"], "thumbs_down")


def main():
    parser = argparse.ArgumentParser(description="Capture Phase 3 dashboard screenshots.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional single desktop output path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for default desktop and mobile screenshots.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="purcival-dashboard-") as temp_dir:
        db_path = Path(temp_dir) / "user.db"
        env = os.environ.copy()
        env["PURCIVAL_GOALS_DB"] = str(db_path)
        env["PYTHONPATH"] = str(ROOT)

        seed_result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "seed_dev_data.py"), "--db", str(db_path)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(seed_result.stdout)
        prepare_phase3_demo_data(db_path)

        port = find_free_port()
        url = f"http://127.0.0.1:{port}/"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "dashboard.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_server(url, server)
            if args.output is not None:
                capture(url, args.output, VIEWPORTS["desktop"])
                print(f"Captured dashboard screenshot: {args.output}")
            else:
                for name, viewport in VIEWPORTS.items():
                    output = args.output_dir / f"phase3-dashboard-{name}.png"
                    capture(url, output, viewport)
                    print(f"Captured dashboard screenshot: {output}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
