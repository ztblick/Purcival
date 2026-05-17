"""
Seed development data for the Goals dashboard.

By default this is idempotent: it creates the mockup goals and example steps
only when they are missing. Pass --reset to clear the target database first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goals import SharedGoalStore


SEED_GOALS = [
    ("career", "Learn more about AI safety"),
    ("health", "Stay active & healthy"),
    ("home", "Be a good husband and father"),
    ("money", "Make some extra money"),
]

SEED_STEPS = {
    "Learn more about AI safety": [
        (
            "Continue learning about LucidAI and their tech",
            "Review recent notes and write down three technical questions.",
        ),
    ],
    "Stay active & healthy": [
        (
            "Go to Yoga6 in Palo Alto at 12pm",
            "Try one class and capture whether the schedule works.",
        ),
    ],
    "Be a good husband and father": [
        (
            "Put up flyers for private tutoring",
            "Choose one local place and post the tutoring flyer.",
        ),
    ],
}


def seed_mockup_data(store: SharedGoalStore, reset: bool = False):
    """Load Zach's mockup goals and a few suggested steps."""
    if reset:
        store.clear_all()

    goal_ids = {}
    existing_goals = {goal["title"]: goal for goal in store.list_goals()}

    for category, title in SEED_GOALS:
        goal = existing_goals.get(title)
        if goal is None:
            goal_id = store.create_goal(
                category=category,
                title=title,
                source="user",
            )
        else:
            goal_id = goal["id"]
        goal_ids[title] = goal_id

    existing_steps = {
        (step["goal_id"], step["title"])
        for step in store.list_steps()
    }
    for goal_title, steps in SEED_STEPS.items():
        goal_id = goal_ids[goal_title]
        for step_title, rationale in steps:
            if (goal_id, step_title) in existing_steps:
                continue
            store.create_step(
                goal_id=goal_id,
                title=step_title,
                rationale=rationale,
                status="suggested",
                source="dashboard_seed",
                created_by_persona="jo",
            )


def main():
    parser = argparse.ArgumentParser(description="Seed Goals dashboard dev data.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Target SQLite database path. Defaults to data/user.db.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear goals, steps, and feedback before seeding.",
    )
    args = parser.parse_args()

    store = SharedGoalStore(args.db)
    seed_mockup_data(store, reset=args.reset)
    print(f"Seeded {len(SEED_GOALS)} goals into {store.db_path}")


if __name__ == "__main__":
    main()
