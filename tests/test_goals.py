from pathlib import Path

import pytest

from goals import SharedGoalStore
from scripts.seed_dev_data import seed_mockup_data


def make_store(tmp_path: Path) -> SharedGoalStore:
    return SharedGoalStore(tmp_path / "user.db")


def test_goal_crud_and_filters(tmp_path):
    store = make_store(tmp_path)

    career_id = store.create_goal(
        category="career",
        title="Learn more about AI safety",
        description="Explore AI safety organizations and technical work.",
        priority=10,
    )
    health_id = store.create_goal(
        category="health",
        title="Stay active & healthy",
    )

    career_goals = store.list_goals(category="career")
    assert [goal["id"] for goal in career_goals] == [career_id]
    assert career_goals[0]["status"] == "active"

    assert store.update_goal_status(health_id, "paused") is True
    active_goals = store.list_goals(status="active")
    assert [goal["id"] for goal in active_goals] == [career_id]

    paused = store.get_goal(health_id)
    assert paused["status"] == "paused"
    assert paused["updated_at"] is not None


def test_step_crud_status_timestamps_and_feedback(tmp_path):
    store = make_store(tmp_path)
    goal_id = store.create_goal("health", "Stay active & healthy")

    step_id = store.create_step(
        goal_id=goal_id,
        title="Go to Yoga6 in Palo Alto at 12pm",
        rationale="A concrete class is easier to accept or reject.",
        source="dashboard_seed",
        created_by_persona="jo",
    )

    step = store.get_step(step_id)
    assert step["status"] == "suggested"
    assert step["accepted_at"] is None

    assert store.update_step_status(step_id, "accepted") is True
    accepted = store.get_step(step_id)
    assert accepted["status"] == "accepted"
    assert accepted["accepted_at"] is not None
    assert accepted["last_touched_at"] is not None

    feedback_id = store.add_step_feedback(step_id, "thumbs_up")
    reason_id = store.add_step_feedback(
        step_id,
        "freeform_note",
        "Good because it has a concrete time.",
    )

    feedback = store.list_step_feedback(step_id)
    assert [row["id"] for row in feedback] == [feedback_id, reason_id]
    assert feedback[1]["value"] == "Good because it has a concrete time."


def test_accept_reject_and_feedback_helpers(tmp_path):
    store = make_store(tmp_path)
    goal_id = store.create_goal("career", "Learn more about AI safety")
    accepted_id = store.create_step(goal_id, "Read one AI safety paper")
    rejected_id = store.create_step(goal_id, "Spend all weekend on AI safety")

    assert store.accept_step(accepted_id) is True
    assert store.get_step(accepted_id)["status"] == "accepted"
    with pytest.raises(ValueError):
        store.reject_step(accepted_id, "Changed my mind too late.")

    feedback_id = store.record_step_feedback(accepted_id, "thumbs_up", "  ")
    assert store.list_step_feedback(accepted_id)[0]["id"] == feedback_id
    assert store.list_step_feedback(accepted_id)[0]["value"] is None

    assert store.reject_step(rejected_id, "Too vague for this week.") is True
    rejected = store.get_step(rejected_id)
    feedback = store.list_step_feedback(rejected_id)

    assert rejected["status"] == "rejected"
    assert rejected["rejected_at"] is not None
    assert feedback[0]["kind"] == "rejection_reason"
    assert feedback[0]["value"] == "Too vague for this week."


def test_goal_delete_cascades_steps_and_feedback(tmp_path):
    store = make_store(tmp_path)
    goal_id = store.create_goal("career", "Learn more about AI safety")
    step_id = store.create_step(goal_id, "Continue learning about LucidAI")
    store.add_step_feedback(step_id, "thumbs_down")

    assert store.delete_goal(goal_id) is True
    assert store.get_step(step_id) is None
    assert store.list_step_feedback(step_id) == []


def test_validation_rejects_invalid_values(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.create_goal("", "No category")
    with pytest.raises(ValueError):
        store.create_goal("career", "Bad status", status="open")

    goal_id = store.create_goal("career", "Learn more about AI safety")
    with pytest.raises(ValueError):
        store.create_step(goal_id, "", status="suggested")
    with pytest.raises(ValueError):
        store.create_step(goal_id, "Bad source", source="agent")
    with pytest.raises(ValueError):
        store.add_step_feedback(999, "thumbs_up")


def test_seed_mockup_data_is_idempotent(tmp_path):
    store = make_store(tmp_path)

    seed_mockup_data(store)
    seed_mockup_data(store)

    goals = store.list_goals()
    steps = store.list_steps()

    assert len(goals) == 4
    assert {goal["title"] for goal in goals} == {
        "Learn more about AI safety",
        "Stay active & healthy",
        "Be a good husband and father",
        "Make some extra money",
    }
    assert len(steps) == 3
    assert all(step["status"] == "suggested" for step in steps)
