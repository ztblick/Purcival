import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.motivation import MOTIVATIONAL_TITLES, title_for_date
from goals import SharedGoalStore
from scripts.seed_dev_data import seed_mockup_data

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_renders_seeded_goals(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert any(title in response.text for title in MOTIVATIONAL_TITLES)
    assert "data-title-rotator" not in response.text
    assert "Learn more about AI safety" in response.text
    assert "steps in progress" not in response.text
    assert "suggested</span>" not in response.text
    assert "dashboard_seed" not in response.text
    assert "Open suggestions" not in response.text
    assert "Accepted" not in response.text
    assert "Reason optional" not in response.text
    assert "Thumbs up" not in response.text
    assert "Steps" in response.text
    assert "Jo" in response.text


def test_dashboard_partials_render(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))

    client = TestClient(app)

    goals_response = client.get("/partials/goals")
    suggestions_response = client.get("/partials/suggestions")
    chat_response = client.get("/partials/chat")

    assert goals_response.status_code == 200
    assert suggestions_response.status_code == 200
    assert chat_response.status_code == 200
    assert "Stay active &amp; healthy" in goals_response.text
    assert "category-health" in goals_response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" not in goals_response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" in suggestions_response.text
    assert "category-home" in suggestions_response.text
    assert "Focused Chat" in chat_response.text


def test_step_accept_and_reject_routes(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))

    client = TestClient(app)
    yoga_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    lucid_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Continue learning about LucidAI and their tech"
    )

    accept_response = client.post(f"/steps/{yoga_step['id']}/accept")
    assert accept_response.status_code == 200
    assert "step-card--accepted" in accept_response.text
    assert SharedGoalStore(db_path).get_step(yoga_step["id"])["status"] == "accepted"

    reject_response = client.post(f"/steps/{lucid_step['id']}/reject")
    assert reject_response.status_code == 200

    refreshed_store = SharedGoalStore(db_path)
    rejected = refreshed_store.get_step(lucid_step["id"])
    assert rejected["status"] == "rejected"
    assert refreshed_store.list_step_feedback(lucid_step["id"]) == []


def test_title_for_date_is_stable_for_same_day():
    title = title_for_date()

    assert title == title_for_date()
    assert title in MOTIVATIONAL_TITLES


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


def test_playwright_accept_reject_flow(tmp_path):
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    yoga_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    lucid_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Continue learning about LucidAI and their tech"
    )

    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"
    env = os.environ.copy()
    env["PURCIVAL_GOALS_DB"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT)
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
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception:
                try:
                    browser = playwright.chromium.launch(channel="msedge")
                except Exception:
                    pytest.skip("No Playwright Chromium-compatible browser is installed")

            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle")

                page.locator(
                    f'[data-step-id="{yoga_step["id"]}"] .decision-button--accept'
                ).click()
                accepted_card = page.locator(
                    f'.step-card--accepted[data-step-id="{yoga_step["id"]}"]'
                )
                expect(accepted_card).to_contain_text("Go to Yoga6 in Palo Alto at 12pm")

                reject_card = page.locator(f'[data-step-id="{lucid_step["id"]}"]')
                reject_card.locator(".decision-button--reject").click()
                expect(page.locator(f'[data-step-id="{lucid_step["id"]}"]')).to_have_count(0)
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    refreshed_store = SharedGoalStore(db_path)
    assert refreshed_store.get_step(yoga_step["id"])["status"] == "accepted"
    assert refreshed_store.get_step(lucid_step["id"])["status"] == "rejected"
    assert refreshed_store.list_step_feedback(lucid_step["id"]) == []
