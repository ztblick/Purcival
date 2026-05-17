from fastapi.testclient import TestClient

from dashboard.app import app
from goals import SharedGoalStore
from scripts.seed_dev_data import seed_mockup_data


def test_dashboard_renders_seeded_goals(tmp_path, monkeypatch):
    db_path = tmp_path / "user.db"
    seed_mockup_data(SharedGoalStore(db_path))
    monkeypatch.setenv("PURCIVAL_GOALS_DB", str(db_path))

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert "One step at a time" in response.text
    assert "Keep at it, Zach" in response.text
    assert "Learn more about AI safety" in response.text
    assert "steps in progress" in response.text
    assert "suggested</span>" not in response.text
    assert "Suggested Steps" in response.text
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
    assert "Go to Yoga6 in Palo Alto at 12pm" in suggestions_response.text
    assert "category-home" in suggestions_response.text
    assert "Focused Chat" in chat_response.text
