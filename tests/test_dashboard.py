import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

import memory
from dashboard import routes
from dashboard.app import app
from dashboard.motivation import MOTIVATIONAL_TITLES, title_for_date
from goals import SharedGoalStore
from memory import MessageScope, PersonaMemory
from scripts.seed_dev_data import seed_mockup_data

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_renders_seeded_goals(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")

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
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")

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


def test_scoped_chat_panel_loads_step_history(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    scope = MessageScope.step(step["id"])
    mem = PersonaMemory("jo")
    mem.add_message("user", "Can we make this fit after school?", scope=scope)
    mem.add_message("assistant", "Yes. Let's narrow the time window.", scope=scope)

    client = TestClient(app)
    response = client.get(f"/chat/step/{step['id']}")

    assert response.status_code == 200
    assert f"step:{step['id']}" in response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" in response.text
    assert "Can we make this fit after school?" in response.text
    assert "Let&#39;s narrow the time window." in response.text


def test_scoped_chat_panel_loads_goal_history(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")

    goal = next(
        row for row in store.list_goals(status="active")
        if row["title"] == "Stay active & healthy"
    )
    scope = MessageScope.goal(goal["id"])
    mem = PersonaMemory("jo")
    mem.add_message("user", "Let's rethink this goal.", scope=scope)

    client = TestClient(app)
    response = client.get(f"/chat/goal/{goal['id']}")

    assert response.status_code == 200
    assert f"goal:{goal['id']}" in response.text
    assert "Stay active &amp; healthy" in response.text
    assert "Let&#39;s rethink this goal." in response.text


def test_scoped_chat_stream_persists_without_default_leak(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")
    monkeypatch.setattr(routes.personas, "load_persona", lambda name: "You are Jo.")
    monkeypatch.setattr(routes, "check_and_summarize", lambda *args, **kwargs: 0)

    captured = {}

    def fake_stream(messages, system, provider=None, max_tokens=2048, task="chat"):
        captured["messages"] = messages
        captured["system"] = system
        yield "Scoped answer."

    monkeypatch.setattr(routes.brain, "stream", fake_stream)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )

    client = TestClient(app)
    post_response = client.post(
        f"/chat/step/{step['id']}/messages",
        data={"message": "How should I think about this?"},
    )
    assert post_response.status_code == 200

    stream_id = post_response.json()["stream_id"]
    stream_response = client.get(f"/chat/streams/{stream_id}")

    assert stream_response.status_code == 200
    assert 'event: delta\ndata: {"text": "Scoped answer."}' in stream_response.text
    assert "event: done" in stream_response.text
    assert "ACTIVE DASHBOARD CONTEXT" in captured["system"]
    assert "Go to Yoga6 in Palo Alto at 12pm" in captured["system"]

    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    scoped_messages = mem.get_recent_messages(scope=scope)

    assert [row["role"] for row in scoped_messages] == ["user", "assistant"]
    assert scoped_messages[0]["content"] == "How should I think about this?"
    assert scoped_messages[1]["content"] == "Scoped answer."
    assert mem.get_message_count() == 0


def test_step_accept_and_reject_routes(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")

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
    env["PURCIVAL_MEMORY_DATA_DIR"] = str(tmp_path / "persona_data")
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


def test_playwright_scoped_chat_flow(tmp_path, monkeypatch):
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    db_path = tmp_path / "user.db"
    memory_dir = tmp_path / "persona_data"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )

    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"
    env = os.environ.copy()
    env["PURCIVAL_GOALS_DB"] = str(db_path)
    env["PURCIVAL_MEMORY_DATA_DIR"] = str(memory_dir)
    env["PURCIVAL_DASHBOARD_FAKE_RESPONSE"] = "Scoped Playwright response."
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
                page.locator(f'[data-step-id="{step["id"]}"]').click(position={"x": 20, "y": 20})
                expect(page.locator(".chat-panel")).to_have_attribute(
                    "data-chat-scope-id",
                    str(step["id"]),
                )
                textarea = page.locator('textarea[name="message"]')
                textarea.fill("Can")
                page.keyboard.press("Space")
                page.keyboard.type("you scope this?")
                expect(textarea).to_have_value("Can you scope this?")
                page.locator('form[data-chat-form] button[type="submit"]').click()

                expect(page.locator(".message-stack")).to_contain_text("Can you scope this?")
                expect(page.locator(".message-stack")).to_contain_text("Scoped Playwright response.")

                page.goto(url, wait_until="networkidle")
                page.locator(f'[data-step-id="{step["id"]}"]').click(position={"x": 20, "y": 20})
                expect(page.locator(".chat-panel")).to_have_attribute(
                    "data-chat-scope-id",
                    str(step["id"]),
                )
                expect(page.locator(".message-stack")).to_contain_text("Can you scope this?")
                expect(page.locator(".message-stack")).to_contain_text("Scoped Playwright response.")
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    monkeypatch.setattr(memory, "DATA_DIR", memory_dir)
    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    assert mem.get_message_count() == 0
    assert [row["role"] for row in mem.get_recent_messages(scope=scope)] == [
        "user",
        "assistant",
    ]
