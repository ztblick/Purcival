"""
Shared goal and step storage for the Goals dashboard.

This module owns user-level goal state in data/user.db. Persona memory remains
per-persona; this store contains structured goals, steps, and feedback that all
personas may read.
"""

import sqlite3
from pathlib import Path
from datetime import datetime


DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "user.db"

GOAL_STATUSES = {"active", "paused", "completed", "abandoned", "archived"}
GOAL_SOURCES = {"user", "import", "agent"}

STEP_STATUSES = {"suggested", "accepted", "rejected", "completed", "abandoned"}
STEP_SOURCES = {"user", "agent_planning", "dashboard_seed"}

FEEDBACK_KINDS = {
    "thumbs_up",
    "thumbs_down",
    "rejection_reason",
    "completion_note",
    "abandon_reason",
    "freeform_note",
}


class SharedGoalStore:
    """SQLite-backed CRUD helper for goals, steps, and step feedback."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS goals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    category    TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    description TEXT,
                    status      TEXT NOT NULL DEFAULT 'active',
                    priority    INTEGER NOT NULL DEFAULT 0,
                    source      TEXT NOT NULL DEFAULT 'user',
                    created_at  TIMESTAMP NOT NULL,
                    updated_at  TIMESTAMP NOT NULL,
                    archived_at TIMESTAMP,

                    CHECK (status IN (
                        'active', 'paused', 'completed', 'abandoned', 'archived'
                    )),
                    CHECK (source IN ('user', 'import', 'agent'))
                );

                CREATE INDEX IF NOT EXISTS idx_goals_status_category
                    ON goals(status, category);

                CREATE TABLE IF NOT EXISTS steps (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id            INTEGER NOT NULL,
                    title              TEXT NOT NULL,
                    description        TEXT,
                    rationale          TEXT,
                    status             TEXT NOT NULL DEFAULT 'suggested',
                    source             TEXT NOT NULL DEFAULT 'user',
                    created_by_persona TEXT,
                    due_at             TIMESTAMP,
                    accepted_at        TIMESTAMP,
                    rejected_at        TIMESTAMP,
                    completed_at       TIMESTAMP,
                    abandoned_at       TIMESTAMP,
                    last_touched_at    TIMESTAMP,
                    created_at         TIMESTAMP NOT NULL,
                    updated_at         TIMESTAMP NOT NULL,

                    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE,
                    CHECK (status IN (
                        'suggested', 'accepted', 'rejected',
                        'completed', 'abandoned'
                    )),
                    CHECK (source IN ('user', 'agent_planning', 'dashboard_seed'))
                );

                CREATE INDEX IF NOT EXISTS idx_steps_goal_status
                    ON steps(goal_id, status);

                CREATE INDEX IF NOT EXISTS idx_steps_status_updated
                    ON steps(status, updated_at);

                CREATE TABLE IF NOT EXISTS step_feedback (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id     INTEGER NOT NULL,
                    kind        TEXT NOT NULL,
                    value       TEXT,
                    created_at  TIMESTAMP NOT NULL,

                    FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE,
                    CHECK (kind IN (
                        'thumbs_up',
                        'thumbs_down',
                        'rejection_reason',
                        'completion_note',
                        'abandon_reason',
                        'freeform_note'
                    ))
                );

                CREATE INDEX IF NOT EXISTS idx_step_feedback_step_created
                    ON step_feedback(step_id, created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def create_goal(
        self,
        category: str,
        title: str,
        description: str | None = None,
        status: str = "active",
        priority: int = 0,
        source: str = "user",
    ) -> int:
        """Create a goal and return its row id."""
        category = self._require_text(category, "category")
        title = self._require_text(title, "title")
        self._require_choice(status, GOAL_STATUSES, "status")
        self._require_choice(source, GOAL_SOURCES, "source")

        now = self._now()
        archived_at = now if status == "archived" else None
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO goals
                    (category, title, description, status, priority, source,
                     created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    category,
                    title,
                    description,
                    status,
                    priority,
                    source,
                    now,
                    now,
                    archived_at,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_goal(self, goal_id: int) -> dict | None:
        """Return one goal, or None if it does not exist."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM goals WHERE id = ?",
                (goal_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_goals(
        self,
        status: str | None = None,
        category: str | None = None,
    ) -> list[dict]:
        """List goals, optionally filtered by status and category."""
        clauses = []
        params = []
        if status is not None:
            self._require_choice(status, GOAL_STATUSES, "status")
            clauses.append("status = ?")
            params.append(status)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM goals
                {where}
                ORDER BY category ASC, priority DESC, id ASC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_goal_status(self, goal_id: int, status: str) -> bool:
        """Update a goal's status. Returns True when a row changed."""
        self._require_choice(status, GOAL_STATUSES, "status")
        now = self._now()
        archived_at = now if status == "archived" else None
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE goals
                SET status = ?, updated_at = ?, archived_at = ?
                WHERE id = ?
                """,
                (status, now, archived_at, goal_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def create_step(
        self,
        goal_id: int,
        title: str,
        description: str | None = None,
        rationale: str | None = None,
        status: str = "suggested",
        source: str = "user",
        created_by_persona: str | None = None,
        due_at: str | None = None,
    ) -> int:
        """Create a step under an existing goal and return its row id."""
        if self.get_goal(goal_id) is None:
            raise ValueError(f"Goal {goal_id} does not exist")
        title = self._require_text(title, "title")
        self._require_choice(status, STEP_STATUSES, "status")
        self._require_choice(source, STEP_SOURCES, "source")

        now = self._now()
        status_times = self._status_timestamps(status, now)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO steps
                    (goal_id, title, description, rationale, status, source,
                     created_by_persona, due_at, accepted_at, rejected_at,
                     completed_at, abandoned_at, last_touched_at, created_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    title,
                    description,
                    rationale,
                    status,
                    source,
                    created_by_persona,
                    due_at,
                    status_times["accepted_at"],
                    status_times["rejected_at"],
                    status_times["completed_at"],
                    status_times["abandoned_at"],
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_step(self, step_id: int) -> dict | None:
        """Return one step, or None if it does not exist."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM steps WHERE id = ?",
                (step_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_steps(
        self,
        goal_id: int | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List steps, optionally filtered by goal and status."""
        clauses = []
        params = []
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if status is not None:
            self._require_choice(status, STEP_STATUSES, "status")
            clauses.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM steps
                {where}
                ORDER BY updated_at DESC, id ASC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_step_status(self, step_id: int, status: str) -> bool:
        """Update a step status and stamp the matching transition time."""
        self._require_choice(status, STEP_STATUSES, "status")
        now = self._now()
        status_times = self._status_timestamps(status, now)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE steps
                SET status = ?,
                    accepted_at = COALESCE(accepted_at, ?),
                    rejected_at = COALESCE(rejected_at, ?),
                    completed_at = COALESCE(completed_at, ?),
                    abandoned_at = COALESCE(abandoned_at, ?),
                    last_touched_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    status_times["accepted_at"],
                    status_times["rejected_at"],
                    status_times["completed_at"],
                    status_times["abandoned_at"],
                    now,
                    now,
                    step_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def accept_step(self, step_id: int) -> bool:
        """Mark a suggested step accepted."""
        step = self.get_step(step_id)
        if step is None:
            return False
        if step["status"] == "accepted":
            return True
        if step["status"] != "suggested":
            raise ValueError("Only suggested steps can be accepted")
        return self.update_step_status(step_id, "accepted")

    def reject_step(self, step_id: int, reason: str | None = None) -> bool:
        """Mark a suggested step rejected and optionally capture the reason."""
        step = self.get_step(step_id)
        if step is None:
            return False
        if step["status"] not in {"suggested", "rejected"}:
            raise ValueError("Only suggested steps can be rejected")
        if step["status"] == "rejected":
            if reason and reason.strip():
                self.add_step_feedback(step_id, "rejection_reason", reason.strip())
            return True
        updated = self.update_step_status(step_id, "rejected")
        if updated and reason and reason.strip():
            self.add_step_feedback(step_id, "rejection_reason", reason.strip())
        return updated

    def record_step_feedback(
        self,
        step_id: int,
        kind: str,
        value: str | None = None,
    ) -> int:
        """Record UI feedback for a step."""
        cleaned_value = value.strip() if value and value.strip() else None
        return self.add_step_feedback(step_id, kind, cleaned_value)

    def add_step_feedback(
        self,
        step_id: int,
        kind: str,
        value: str | None = None,
    ) -> int:
        """Add sparse feedback to a step and return the feedback row id."""
        if self.get_step(step_id) is None:
            raise ValueError(f"Step {step_id} does not exist")
        self._require_choice(kind, FEEDBACK_KINDS, "kind")

        now = self._now()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO step_feedback (step_id, kind, value, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (step_id, kind, value, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def list_step_feedback(self, step_id: int) -> list[dict]:
        """Return feedback rows for a step, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM step_feedback
                WHERE step_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (step_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def delete_goal(self, goal_id: int) -> bool:
        """Delete a goal and cascade its steps and feedback."""
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def clear_all(self):
        """Delete all shared goal data from this database."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM step_feedback")
            conn.execute("DELETE FROM steps")
            conn.execute("DELETE FROM goals")
            conn.commit()
        finally:
            conn.close()

    def _status_timestamps(self, status: str, timestamp: str) -> dict[str, str | None]:
        return {
            "accepted_at": timestamp if status == "accepted" else None,
            "rejected_at": timestamp if status == "rejected" else None,
            "completed_at": timestamp if status == "completed" else None,
            "abandoned_at": timestamp if status == "abandoned" else None,
        }

    def _require_choice(self, value: str, allowed: set[str], label: str):
        if value not in allowed:
            choices = ", ".join(sorted(allowed))
            raise ValueError(f"Invalid {label} '{value}'. Expected one of: {choices}")

    def _require_text(self, value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{label} cannot be empty")
        return cleaned

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
