import json
import os
import re
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
from dashboard.app import create_app
from dashboard.auth import (
    create_signed_session,
    hash_dashboard_password,
    verify_dashboard_password,
    verify_signed_session,
)
from dashboard.config import DashboardConfigError, load_dashboard_config, validate_dashboard_config
from dashboard.motivation import MOTIVATIONAL_TITLES, title_for_date
from delivery import deliver_opportunity_to_inbox
from goals import SharedGoalStore
from memory import MessageScope, PersonaMemory
from scripts.seed_dev_data import seed_mockup_data

ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "correct horse battery staple"
TEST_SECRET = "0123456789abcdef0123456789abcdef"
TEST_SALT = b"0123456789abcdef"


def configure_dashboard_auth(monkeypatch, **overrides):
    monkeypatch.setenv(
        "PURCIVAL_DASHBOARD_PASSWORD_HASH",
        hash_dashboard_password(TEST_PASSWORD, iterations=1_000, salt=TEST_SALT),
    )
    monkeypatch.setenv("PURCIVAL_DASHBOARD_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv(
        "PURCIVAL_DASHBOARD_EXPOSURE",
        overrides.get("exposure", "local"),
    )
    monkeypatch.setenv(
        "PURCIVAL_DASHBOARD_HOST",
        overrides.get("host", "127.0.0.1"),
    )
    monkeypatch.setenv(
        "PURCIVAL_DASHBOARD_PORT",
        str(overrides.get("port", 8000)),
    )
    monkeypatch.setenv(
        "PURCIVAL_DASHBOARD_SESSION_DAYS",
        str(overrides.get("session_days", 30)),
    )
    public_base_url = overrides.get("public_base_url")
    if public_base_url is None:
        monkeypatch.delenv("PURCIVAL_DASHBOARD_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("PURCIVAL_DASHBOARD_PUBLIC_BASE_URL", public_base_url)


def configure_dashboard_env(monkeypatch, tmp_path, db_path):
    configure_dashboard_auth(monkeypatch)
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))
    monkeypatch.setenv("PURCIVAL_MEMORY_DATA_DIR", str(tmp_path / "persona_data"))
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path / "persona_data")


def make_client(monkeypatch, tmp_path, db_path):
    configure_dashboard_env(monkeypatch, tmp_path, db_path)
    return TestClient(create_app())


def login(client: TestClient, next_path: str = "/"):
    response = client.post(
        "/login",
        data={"password": TEST_PASSWORD, "next": next_path},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def extract_csrf_token(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def csrf_headers(client: TestClient, path: str = "/") -> dict[str, str]:
    response = client.get(path)
    assert response.status_code == 200
    return {"X-CSRF-Token": extract_csrf_token(response.text)}


def dashboard_url(path: str = "/") -> str:
    return f"http://testserver{path}"


def test_password_hash_round_trip_and_malformed_failure():
    stored_hash = hash_dashboard_password(
        TEST_PASSWORD,
        iterations=1_000,
        salt=TEST_SALT,
    )

    assert verify_dashboard_password(TEST_PASSWORD, stored_hash) is True
    assert verify_dashboard_password("not-the-password", stored_hash) is False
    assert verify_dashboard_password(TEST_PASSWORD, "pbkdf2_sha256$bad") is False


def test_signed_sessions_reject_tampering_and_expiry():
    token = create_signed_session(TEST_SECRET, session_days=30, now=100)
    session = verify_signed_session(token, TEST_SECRET, now=200)
    assert session is not None

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert verify_signed_session(tampered, TEST_SECRET, now=200) is None
    assert verify_signed_session(token, TEST_SECRET, now=31 * 24 * 60 * 60 + 101) is None


def test_dashboard_config_guards_unsafe_startup(monkeypatch):
    configure_dashboard_auth(monkeypatch, exposure="local", host="0.0.0.0")
    with pytest.raises(DashboardConfigError):
        validate_dashboard_config(load_dashboard_config())

    configure_dashboard_auth(monkeypatch, exposure="tailscale", host="127.0.0.1")
    with pytest.raises(DashboardConfigError):
        validate_dashboard_config(load_dashboard_config())

    configure_dashboard_auth(
        monkeypatch,
        exposure="tailscale",
        host="127.0.0.1",
        public_base_url="https://purcival.tail123.ts.net",
    )
    validated = validate_dashboard_config(load_dashboard_config())
    assert validated.secure_cookies is True


def test_unauthenticated_routes_redirect_or_block(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    configure_dashboard_env(monkeypatch, tmp_path, db_path)
    client = TestClient(create_app())

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    post_response = client.post("/steps/1/accept", follow_redirects=False)
    assert post_response.status_code == 401

    stream_response = client.get("/chat/streams/missing", follow_redirects=False)
    assert stream_response.status_code == 401


def test_login_logout_and_cookie_flags(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    client = make_client(monkeypatch, tmp_path, db_path)

    login_response = login(client)
    cookie_header = login_response.headers.get("set-cookie", "")
    assert "purcival_dashboard_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "samesite=lax" in cookie_header.lower()

    logout_response = client.post(
        "/logout",
        headers=csrf_headers(client),
        data={"csrf_token": extract_csrf_token(client.get("/").text)},
        follow_redirects=False,
    )
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"
    redirected = client.get("/", follow_redirects=False)
    assert redirected.status_code == 303


def test_mutating_post_requires_csrf(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    yoga_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    response = client.post(f"/steps/{yoga_step['id']}/accept", follow_redirects=False)
    assert response.status_code == 403


def test_actor_spoofing_is_rejected_by_verified_session(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    yoga_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    headers = csrf_headers(client)
    response = client.post(
        f"/steps/{yoga_step['id']}/accept?actor=mallory",
        headers={**headers, "X-Remote-User": "mallory", "X-Forwarded-User": "mallory"},
    )
    assert response.status_code == 200

    event = PersonaMemory("jo").get_agent_events(event_type="step_accepted")[0]
    payload = json.loads(event["payload_json"])
    assert payload["actor"] == "zach_dashboard"
    assert payload["actor_metadata"]["client_host"] == "testclient"


def test_dashboard_renders_seeded_goals(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

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
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

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
    assert "health" in suggestions_response.text
    assert "Focused Chat" in chat_response.text


def test_dashboard_category_filter_renders_bubbles_and_filters_steps(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    response = client.get("/?category=career")

    assert response.status_code == 200
    assert 'data-category-filter=""' in response.text
    assert 'data-category-filter="career"' in response.text
    assert 'category-bubble--active' in response.text
    assert "Learn more about AI safety" in response.text
    assert "Continue learning about LucidAI and their tech" in response.text
    assert "Stay active &amp; healthy" not in response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" not in response.text
    assert "Be a good husband and father" not in response.text
    assert "Put up flyers for private tutoring" not in response.text


def test_dashboard_goal_filter_narrows_steps_and_ignores_mismatched_goal(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    career_goal = next(
        goal for goal in store.list_goals(status="active")
        if goal["category"] == "career"
    )
    health_goal = next(
        goal for goal in store.list_goals(status="active")
        if goal["category"] == "health"
    )
    store.create_step(
        goal_id=career_goal["id"],
        title="Draft one AI safety question",
        status="accepted",
    )

    goal_response = client.get(f"/partials/suggestions?goal_id={career_goal['id']}")
    mismatch_response = client.get(
        f"/partials/suggestions?category=career&goal_id={health_goal['id']}"
    )

    assert goal_response.status_code == 200
    assert "Continue learning about LucidAI and their tech" in goal_response.text
    assert "Draft one AI safety question" in goal_response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" not in goal_response.text
    assert "Put up flyers for private tutoring" not in goal_response.text
    assert f"/steps/" in goal_response.text
    assert "goal_id=" in goal_response.text

    assert mismatch_response.status_code == 200
    assert "Continue learning about LucidAI and their tech" in mismatch_response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" not in mismatch_response.text


def test_dashboard_category_filter_hides_nonmatching_step_inbox_cards(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    mem = PersonaMemory("jo")
    health_goal = next(
        goal for goal in store.list_goals(status="active")
        if goal["category"] == "health"
    )
    step_id = store.create_step(
        goal_id=health_goal["id"],
        title="Try the health inbox filter",
        status="suggested",
        source="agent_planning",
        created_by_persona="jo",
    )
    opportunity_id = mem.add_agent_opportunity(
        kind="suggest_goal_step",
        title="Try the health inbox filter",
        rationale="This should disappear outside health.",
        goal_id=health_goal["id"],
        step_id=step_id,
        status="delivered",
        urgency=3,
        impact=4,
        confidence=4,
        attention_cost=1,
        risk_level="low",
    )
    deliver_opportunity_to_inbox(mem, store, mem.get_agent_opportunity(opportunity_id))

    client = TestClient(create_app())
    login(client)
    career_response = client.get("/?category=career")
    health_response = client.get("/?category=health")

    assert career_response.status_code == 200
    assert "Suggested step: Try the health inbox filter" not in career_response.text
    assert health_response.status_code == 200
    assert "Suggested step: Try the health inbox filter" in health_response.text


def test_scoped_chat_panel_loads_step_history(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    scope = MessageScope.step(step["id"])
    mem = PersonaMemory("jo")
    mem.add_message("user", "Can we make this fit after school?", scope=scope)
    mem.add_message("assistant", "Yes. Let's narrow the time window.", scope=scope)

    client = TestClient(create_app())
    login(client)
    response = client.get(f"/chat/step/{step['id']}")

    assert response.status_code == 200
    assert f"step:{step['id']}" in response.text
    assert "Go to Yoga6 in Palo Alto at 12pm" in response.text
    assert "Can we make this fit after school?" in response.text
    assert "Let&#39;s narrow the time window." in response.text
    assert 'data-chat-history' in response.text
    assert 'data-message-id=' in response.text
    assert 'class="chat-dock"' in response.text
    assert 'class="composer__input-row"' in response.text
    assert response.text.index('class="message-stack"') < response.text.index('class="chat-dock"')


def test_scoped_chat_messages_can_page_older_history(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    scope = MessageScope.step(step["id"])
    mem = PersonaMemory("jo")
    for index in range(25):
        mem.add_message("user", f"Scoped message {index}", scope=scope)

    client = TestClient(create_app())
    login(client)
    recent_response = client.get(f"/chat/step/{step['id']}/messages?limit=20")
    assert recent_response.status_code == 200
    recent_payload = recent_response.json()
    assert len(recent_payload["messages"]) == 20
    assert recent_payload["has_more"] is True
    assert recent_payload["messages"][0]["content"] == "Scoped message 5"

    before_id = recent_payload["messages"][0]["id"]
    older_response = client.get(
        f"/chat/step/{step['id']}/messages?before_id={before_id}&limit=20"
    )
    assert older_response.status_code == 200
    older_payload = older_response.json()
    assert len(older_payload["messages"]) == 5
    assert older_payload["has_more"] is False
    assert older_payload["messages"][0]["content"] == "Scoped message 0"
    assert older_payload["messages"][-1]["content"] == "Scoped message 4"


def test_scoped_chat_panel_loads_goal_history(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    goal = next(
        row for row in store.list_goals(status="active")
        if row["title"] == "Stay active & healthy"
    )
    scope = MessageScope.goal(goal["id"])
    mem = PersonaMemory("jo")
    mem.add_message("user", "Let's rethink this goal.", scope=scope)

    client = TestClient(create_app())
    login(client)
    response = client.get(f"/chat/goal/{goal['id']}")

    assert response.status_code == 200
    assert f"goal:{goal['id']}" in response.text
    assert "Stay active &amp; healthy" in response.text
    assert "Let&#39;s rethink this goal." in response.text


def test_scoped_chat_stream_persists_without_default_leak(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)
    monkeypatch.setattr(routes.personas, "load_persona", lambda name: "You are Jo.")
    monkeypatch.setattr(routes, "check_and_summarize", lambda *args, **kwargs: 0)

    captured = {}

    def fake_stream(messages, system, provider=None, max_tokens=2048, task="chat"):
        captured["messages"] = messages
        captured["system"] = system
        yield "**Scoped"
        yield " answer**."

    monkeypatch.setattr(routes.brain, "stream", fake_stream)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )

    client = TestClient(create_app())
    login(client)
    post_response = client.post(
        f"/chat/step/{step['id']}/messages",
        headers=csrf_headers(client),
        data={"message": "How should I think about this?"},
    )
    assert post_response.status_code == 200

    stream_id = post_response.json()["stream_id"]
    stream_response = client.get(f"/chat/streams/{stream_id}")

    assert stream_response.status_code == 200
    assert 'event: delta\ndata: {"text": "**Scoped"}' in stream_response.text
    assert 'event: delta\ndata: {"text": " answer**."}' in stream_response.text
    assert "event: done" in stream_response.text
    assert "ACTIVE DASHBOARD CONTEXT" in captured["system"]
    assert "Go to Yoga6 in Palo Alto at 12pm" in captured["system"]

    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    scoped_messages = mem.get_recent_messages(scope=scope)

    assert [row["role"] for row in scoped_messages] == ["user", "assistant"]
    assert scoped_messages[0]["content"] == "How should I think about this?"
    assert scoped_messages[1]["content"] == "**Scoped answer**."
    assert mem.get_message_count() == 0


def test_scoped_chat_stream_suppresses_schedule_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)
    monkeypatch.setattr(routes.personas, "load_persona", lambda name: "You are Jo.")
    monkeypatch.setattr(routes, "check_and_summarize", lambda *args, **kwargs: 0)

    def fake_stream(messages, system, provider=None, max_tokens=2048, task="chat"):
        yield "Visible before. "
        yield "<schedule_"
        yield "updates>"
        yield '[{"tool": "schedule", "method": "cancel_wakeup", "parameters": {"id": 7}}]'
        yield "</schedule_"
        yield "updates>"
        yield " Visible after."

    monkeypatch.setattr(routes.brain, "stream", fake_stream)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )

    client = TestClient(create_app())
    login(client)
    post_response = client.post(
        f"/chat/step/{step['id']}/messages",
        headers=csrf_headers(client),
        data={"message": "Please adjust the plan."},
    )
    assert post_response.status_code == 200

    stream_response = client.get(f"/chat/streams/{post_response.json()['stream_id']}")

    assert stream_response.status_code == 200
    assert "Visible before." in stream_response.text
    assert "Visible after." in stream_response.text
    assert "schedule_updates" not in stream_response.text
    assert "cancel_wakeup" not in stream_response.text
    assert "event: done" in stream_response.text

    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    assistant_message = mem.get_recent_messages(scope=scope)[1]
    assert assistant_message["content"] == "Visible before.  Visible after."


def test_scoped_chat_stream_applies_internal_step_receipt(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)
    monkeypatch.setattr(routes.personas, "load_persona", lambda name: "You are Jo.")
    monkeypatch.setattr(routes, "check_and_summarize", lambda *args, **kwargs: 0)

    step = next(
        row for row in store.list_steps(status="suggested")
        if row["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    store.update_step_status(step["id"], "accepted")
    action_json = json.dumps([{
        "tool": "steps",
        "method": "abandon_step",
        "parameters": {
            "step_id": step["id"],
            "note": "Zach said the class no longer fits.",
        },
    }])

    def fake_stream(messages, system, provider=None, max_tokens=2048, task="chat"):
        yield "That step no longer fits. "
        yield "<internal_actions>"
        yield action_json
        yield "</internal_actions>"

    monkeypatch.setattr(routes.brain, "stream", fake_stream)

    client = TestClient(create_app())
    login(client)
    post_response = client.post(
        f"/chat/step/{step['id']}/messages",
        headers=csrf_headers(client),
        data={"message": "Actually, this class does not fit anymore."},
    )
    assert post_response.status_code == 200

    stream_response = client.get(f"/chat/streams/{post_response.json()['stream_id']}")
    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    scoped_messages = mem.get_recent_messages(scope=scope)
    event = mem.get_agent_events(event_type="step_abandoned")[0]
    payload = json.loads(event["payload_json"])

    assert stream_response.status_code == 200
    assert "internal_actions" not in stream_response.text
    assert "event: receipt" in stream_response.text
    assert "abandoned" in stream_response.text
    assert SharedGoalStore(db_path).get_step(step["id"])["status"] == "abandoned"
    assert scoped_messages[-1]["content"].startswith("Receipt: abandoned")
    assert payload["previous_status"] == "accepted"
    assert payload["related_message_ids"]
    assert payload["actor"] == "zach_dashboard"


def test_step_accept_and_reject_routes(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    yoga_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Go to Yoga6 in Palo Alto at 12pm"
    )
    lucid_step = next(
        step for step in store.list_steps(status="suggested")
        if step["title"] == "Continue learning about LucidAI and their tech"
    )

    headers = csrf_headers(client)
    accept_response = client.post(f"/steps/{yoga_step['id']}/accept", headers=headers)
    assert accept_response.status_code == 200
    assert "step-card--accepted" in accept_response.text
    assert "Receipt:" in accept_response.text
    assert SharedGoalStore(db_path).get_step(yoga_step["id"])["status"] == "accepted"

    reject_response = client.post(f"/steps/{lucid_step['id']}/reject", headers=headers)
    assert reject_response.status_code == 200

    refreshed_store = SharedGoalStore(db_path)
    rejected = refreshed_store.get_step(lucid_step["id"])
    assert rejected["status"] == "rejected"
    assert refreshed_store.list_step_feedback(lucid_step["id"]) == []
    mem = PersonaMemory("jo")
    assert len(mem.get_agent_events(event_type="step_accepted")) == 1
    assert len(mem.list_agent_opportunities(kind="accountability_check")) == 1
    assert len(mem.get_agent_events(event_type="step_rejected")) == 1


def test_step_complete_and_abandon_routes_write_receipts(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    client = make_client(monkeypatch, tmp_path, db_path)
    login(client)

    accepted_step_id = store.create_step(
        goal_id=store.list_goals(status="active")[0]["id"],
        title="Finish the accountability slice",
        status="accepted",
    )
    abandoned_step_id = store.create_step(
        goal_id=store.list_goals(status="active")[0]["id"],
        title="Try a stale idea",
        status="accepted",
    )

    headers = csrf_headers(client)
    complete_response = client.post(f"/steps/{accepted_step_id}/complete", headers=headers)
    abandon_response = client.post(f"/steps/{abandoned_step_id}/abandon", headers=headers)

    refreshed_store = SharedGoalStore(db_path)
    mem = PersonaMemory("jo")

    assert complete_response.status_code == 200
    assert "marked done" in complete_response.text
    assert abandon_response.status_code == 200
    assert "abandoned" in abandon_response.text
    assert refreshed_store.get_step(accepted_step_id)["status"] == "completed"
    assert refreshed_store.get_step(abandoned_step_id)["status"] == "abandoned"
    assert len(mem.get_agent_events(event_type="step_completed")) == 1
    assert len(mem.get_agent_events(event_type="step_abandoned")) == 1


def test_dashboard_renders_and_acts_on_inbox_card(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    mem = PersonaMemory("jo")
    goal = next(goal for goal in store.list_goals(status="active"))
    step_id = store.create_step(
        goal_id=goal["id"],
        title="Pick one inbox-tested step",
        status="suggested",
        source="agent_planning",
        created_by_persona="jo",
    )
    opportunity_id = mem.add_agent_opportunity(
        kind="suggest_goal_step",
        title="Pick one inbox-tested step",
        rationale="A card should make this suggestion visible.",
        goal_id=goal["id"],
        step_id=step_id,
        status="delivered",
        urgency=3,
        impact=4,
        confidence=4,
        attention_cost=1,
        risk_level="low",
    )
    opportunity = mem.get_agent_opportunity(opportunity_id)
    deliver_opportunity_to_inbox(mem, store, opportunity)

    client = TestClient(create_app())
    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "Inbox" in response.text
    assert "Suggested step: Pick one inbox-tested step" in response.text
    assert "Open chat" in response.text

    item = mem.list_agent_inbox_items()[0]
    accept_response = client.post(
        f"/inbox/{item['id']}/accept",
        headers=csrf_headers(client),
    )
    refreshed_mem = PersonaMemory("jo")

    assert accept_response.status_code == 200
    assert "accepted" in accept_response.text
    assert SharedGoalStore(db_path).get_step(step_id)["status"] == "accepted"
    assert refreshed_mem.get_agent_inbox_item(item["id"])["status"] == "acted"
    assert refreshed_mem.list_agent_inbox_items() == []


def test_dashboard_can_snooze_inbox_card(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    store = SharedGoalStore(db_path)
    seed_mockup_data(store)
    configure_dashboard_env(monkeypatch, tmp_path, db_path)

    mem = PersonaMemory("jo")
    item_id = mem.add_agent_inbox_item(
        priority=3,
        surface="dashboard",
        title="Check in",
        body="A proactive card is ready.",
        actions=[{"type": "snooze", "label": "Snooze"}],
    )

    client = TestClient(create_app())
    login(client)
    response = client.post(
        f"/inbox/{item_id}/snooze",
        headers=csrf_headers(client),
    )

    refreshed = PersonaMemory("jo")
    assert response.status_code == 200
    assert "snoozed" in response.text
    assert refreshed.get_agent_inbox_item(item_id)["status"] == "snoozed"
    assert refreshed.list_agent_inbox_items() == []


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
            response = requests.get(url, timeout=0.5, allow_redirects=False)
            if response.status_code in {200, 303}:
                return
        except requests.RequestException:
            time.sleep(0.2)
    raise TimeoutError(f"Dashboard server did not become ready at {url}")


def dashboard_subprocess_env(tmp_path, db_path, **extra):
    env = os.environ.copy()
    env["PURCIVAL_GOALS_DB"] = str(db_path)
    env["PURCIVAL_MEMORY_DATA_DIR"] = str(tmp_path / "persona_data")
    env["PURCIVAL_DASHBOARD_PASSWORD_HASH"] = hash_dashboard_password(
        TEST_PASSWORD,
        iterations=1_000,
        salt=TEST_SALT,
    )
    env["PURCIVAL_DASHBOARD_SECRET_KEY"] = TEST_SECRET
    env["PURCIVAL_DASHBOARD_EXPOSURE"] = "local"
    env["PURCIVAL_DASHBOARD_HOST"] = "127.0.0.1"
    env["PYTHONPATH"] = str(ROOT)
    for key, value in extra.items():
        env[key] = value
    return env


def playwright_login(page):
    page.get_by_label("Password").fill(TEST_PASSWORD)
    page.get_by_role("button", name="Unlock Dashboard").click()


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
    env = dashboard_subprocess_env(tmp_path, db_path)
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
                playwright_login(page)
                expect(page).to_have_url(re.compile(r"/$"))

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


def test_playwright_category_filter_flow(tmp_path):
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))

    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"
    env = dashboard_subprocess_env(tmp_path, db_path)
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
                playwright_login(page)
                expect(page).to_have_url(re.compile(r"/$"))

                page.locator('[data-category-filter="career"]').click()
                expect(page).to_have_url(re.compile(r"category=career"))
                expect(page.locator(".goal-grid")).to_contain_text("Learn more about AI safety")
                expect(page.locator(".goal-grid")).not_to_contain_text("Stay active & healthy")
                expect(page.locator("#steps-panel")).to_contain_text(
                    "Continue learning about LucidAI and their tech"
                )
                expect(page.locator("#steps-panel")).not_to_contain_text(
                    "Go to Yoga6 in Palo Alto at 12pm"
                )

                page.locator('[data-category-filter=""]').click()
                expect(page).to_have_url(re.compile(r"/$"))
                expect(page.locator(".goal-grid")).to_contain_text("Stay active & healthy")
                expect(page.locator("#steps-panel")).to_contain_text(
                    "Go to Yoga6 in Palo Alto at 12pm"
                )
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


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
    env = dashboard_subprocess_env(
        tmp_path,
        db_path,
        PURCIVAL_MEMORY_DATA_DIR=str(memory_dir),
        PURCIVAL_DASHBOARD_FAKE_RESPONSE="**Scoped** Playwright response.",
    )
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
                playwright_login(page)
                expect(page).to_have_url(re.compile(r"/$"))
                page.locator(f'[data-step-id="{step["id"]}"]').click(position={"x": 20, "y": 20})
                expect(page.locator(".chat-panel")).to_have_attribute(
                    "data-chat-scope-id",
                    str(step["id"]),
                )
                expect(page.locator(".message-stack")).to_have_css("overflow-y", "auto")
                expect(page.locator(".chat-dock")).to_have_css("display", "grid")
                expect(page.locator(".composer__input-row")).to_have_css("display", "grid")
                textarea = page.locator('textarea[name="message"]')
                textarea.fill("Can")
                page.keyboard.press("Space")
                page.keyboard.type("you scope this?")
                expect(textarea).to_have_value("Can you scope this?")
                page.locator('form[data-chat-form] button[type="submit"]').click()

                expect(page.locator(".message-stack")).to_contain_text("Can you scope this?")
                expect(page.locator(".message-stack")).to_contain_text("Scoped Playwright response.")
                expect(page.locator(".message-stack .chat-message--assistant strong")).to_contain_text("Scoped")

                page.goto(url, wait_until="networkidle")
                page.locator(f'[data-step-id="{step["id"]}"]').click(position={"x": 20, "y": 20})
                expect(page.locator(".chat-panel")).to_have_attribute(
                    "data-chat-scope-id",
                    str(step["id"]),
                )
                expect(page.locator(".message-stack")).to_contain_text("Can you scope this?")
                expect(page.locator(".message-stack")).to_contain_text("Scoped Playwright response.")
                expect(page.locator(".message-stack .chat-message--assistant strong")).to_contain_text("Scoped")
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


def test_playwright_streaming_keeps_manual_scroll_position(tmp_path, monkeypatch):
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

    monkeypatch.setattr(memory, "DATA_DIR", memory_dir)
    mem = PersonaMemory("jo")
    scope = MessageScope.step(step["id"])
    for index in range(60):
        role = "user" if index % 2 == 0 else "assistant"
        mem.add_message(role, f"Earlier scoped message {index}", scope=scope)

    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"
    env = dashboard_subprocess_env(
        tmp_path,
        db_path,
        PURCIVAL_MEMORY_DATA_DIR=str(memory_dir),
    )
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
                page.add_init_script(
                    """
                    (() => {
                      const originalFetch = window.fetch.bind(window);
                      window.__eventSources = [];

                      class FakeEventSource {
                        constructor(url) {
                          this.url = url;
                          this.listeners = {};
                          this.closed = false;
                          window.__eventSources.push(this);
                        }

                        addEventListener(type, listener) {
                          if (!this.listeners[type]) {
                            this.listeners[type] = [];
                          }
                          this.listeners[type].push(listener);
                        }

                        close() {
                          this.closed = true;
                        }

                        emit(type, payload) {
                          const event = { data: JSON.stringify(payload) };
                          for (const listener of this.listeners[type] || []) {
                            listener(event);
                          }
                        }
                      }

                      window.EventSource = FakeEventSource;
                      window.fetch = async (input, init = {}) => {
                        const url = typeof input === "string" ? input : input.url;
                        const method = (init.method || "GET").toUpperCase();
                        if (method === "POST" && /\\/chat\\/step\\/\\d+\\/messages$/.test(url)) {
                          return new Response(
                            JSON.stringify({ stream_id: "fake-stream", message_id: 12345 }),
                            {
                              status: 200,
                              headers: { "Content-Type": "application/json" },
                            },
                          );
                        }
                        return originalFetch(input, init);
                      };
                    })();
                    """
                )
                page.goto(url, wait_until="networkidle")
                playwright_login(page)
                expect(page).to_have_url(re.compile(r"/$"))
                page.locator(f'[data-step-id="{step["id"]}"]').click(position={"x": 20, "y": 20})
                expect(page.locator(".chat-panel")).to_have_attribute(
                    "data-chat-scope-id",
                    str(step["id"]),
                )
                page.wait_for_function(
                    """
                    () => {
                      const stack = document.querySelector(".message-stack");
                      return Boolean(stack && stack.scrollHeight > stack.clientHeight);
                    }
                    """
                )

                textarea = page.locator('textarea[name="message"]')
                textarea.fill("Keep streaming while I scroll up.")
                page.locator('form[data-chat-form] button[type="submit"]').click()
                page.wait_for_function(
                    "() => window.__eventSources && window.__eventSources.length === 1"
                )

                before_scroll_top = page.evaluate(
                    """
                    () => {
                      const stack = document.querySelector(".message-stack");
                      const target = Math.max(
                        64,
                        stack.scrollHeight - stack.clientHeight - 240,
                      );
                      stack.scrollTop = target;
                      return stack.scrollTop;
                    }
                    """
                )

                page.evaluate(
                    """
                    () => {
                      window.__eventSources[0].emit("delta", {
                        text: "First streamed chunk.",
                      });
                    }
                    """
                )
                expect(page.locator(".message-stack")).to_contain_text("First streamed chunk.")
                first_scroll_top = page.evaluate(
                    "() => document.querySelector('.message-stack').scrollTop"
                )
                assert abs(first_scroll_top - before_scroll_top) <= 1

                page.evaluate(
                    """
                    () => {
                      window.__eventSources[0].emit("delta", {
                        text: "\\n\\nSecond streamed chunk.",
                      });
                    }
                    """
                )
                expect(page.locator(".message-stack")).to_contain_text("Second streamed chunk.")
                second_scroll_top = page.evaluate(
                    "() => document.querySelector('.message-stack').scrollTop"
                )
                distance_from_bottom = page.evaluate(
                    """
                    () => {
                      const stack = document.querySelector(".message-stack");
                      return stack.scrollHeight - stack.clientHeight - stack.scrollTop;
                    }
                    """
                )

                assert abs(second_scroll_top - before_scroll_top) <= 1
                assert distance_from_bottom > 120

                page.evaluate(
                    """
                    () => {
                      window.__eventSources[0].emit("done", { message_id: 98765 });
                    }
                    """
                )
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
