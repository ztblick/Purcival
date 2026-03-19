"""
Memory — persistent conversation storage and retrieval.

Each persona gets its own SQLite database at data/<persona>/memory.db.
This module handles:
    - Storing every message (user and assistant) permanently
    - Storing conversation summaries with vector embeddings
    - Retrieving recent messages for the context window
    - Retrieving relevant summaries via cosine similarity search
    - Storing proactive schedule configuration
    - Managing proactive triggers
    - Tool state persistence (Stage 5)
    - Agent action logging (Stage 5)
    - Agent narrative state (Stage 5)
    - Reasoning log (Stage 5)

The rest of the app interacts with memory through a PersonaMemory instance.
One instance per persona, created at startup.

Database schema:
    messages        — full verbatim record of every message, never deleted
    summaries       — compressed conversation chunks with embeddings
    triggers        — scheduled wake-ups for proactive messaging / agent cycles
    schedule_config — schedule settings (single row per persona)
    tool_state      — key-value store for per-tool persistent state
    agent_actions   — audit trail of every action the agent takes or proposes
    agent_narrative — rolling prose state written by the LLM (single row)
    reasoning_log   — full reasoning traces for debugging

Design notes:
    - WAL mode is enabled for concurrent reads (multiple personas can
      read while one writes, though each persona has its own DB)
    - Embeddings are stored as binary blobs (numpy arrays serialized
      with tobytes/frombuffer)
    - Cosine similarity search is done in Python, not SQL. At the scale
      of a personal assistant (hundreds to low thousands of summaries),
      this is fast enough and avoids adding a vector DB dependency.
"""

import json
import sqlite3
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

# All persona data lives under this directory
DATA_DIR = Path(__file__).parent / "data"


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute cosine similarity between two vectors.

    Returns a value between -1 and 1, where 1 means identical direction.
    Used to find summaries whose embeddings are most similar to the
    current message's embedding.
    """
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm == 0:
        return 0.0
    return float(dot / norm)


class PersonaMemory:
    """
    Persistent memory for a single persona.

    Each persona gets its own SQLite database. Messages are stored
    verbatim and never deleted. Summaries are generated from older
    messages and stored with vector embeddings for retrieval.

    Usage:
        memory = PersonaMemory("purcival")
        memory.add_message("user", "What should I work on today?")
        memory.add_message("assistant", "Let's review your priorities...")
        recent = memory.get_recent_messages(limit=20)
        relevant = memory.search_summaries(query_embedding, top_k=5)
    """

    def __init__(self, persona_name: str):
        self.persona_name = persona_name
        self.db_path = DATA_DIR / persona_name / "memory.db"

        # Create the directory structure if it doesn't exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database and schema
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """
        Create a database connection with our preferred settings.

        WAL mode allows concurrent reads while writing. Row factory
        gives us dict-like access to columns by name.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """
        Create tables if they don't exist.

        This runs on every startup, which is safe because CREATE TABLE
        IF NOT EXISTS is a no-op when the table already exists.
        """
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary         TEXT NOT NULL,
                    message_start   INTEGER NOT NULL,
                    message_end     INTEGER NOT NULL,
                    embedding       BLOB,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS triggers (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    type        TEXT NOT NULL,
                    fire_at     TIMESTAMP NOT NULL,
                    context     TEXT,
                    recurring   TEXT,
                    fired       BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS schedule_config (
                    id                  INTEGER PRIMARY KEY CHECK (id = 1),
                    start_time          TEXT NOT NULL,
                    end_time            TEXT NOT NULL,
                    interval_minutes    INTEGER NOT NULL,
                    max_actions_per_day INTEGER NOT NULL DEFAULT 25,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS tool_state (
                    tool_name   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tool_name, key)
                );

                CREATE TABLE IF NOT EXISTS agent_actions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id    TEXT NOT NULL,
                    tool_name   TEXT NOT NULL,
                    method_name TEXT NOT NULL,
                    tier        TEXT NOT NULL,
                    parameters  TEXT,
                    result      TEXT,
                    status      TEXT NOT NULL DEFAULT 'completed',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agent_narrative (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    narrative   TEXT NOT NULL,
                    cycle_id    TEXT NOT NULL,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS reasoning_log (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id         TEXT NOT NULL,
                    trigger_id       INTEGER,
                    trigger_purpose  TEXT,
                    tool_contexts    TEXT,
                    narrative_in     TEXT,
                    llm_response     TEXT,
                    actions_taken    TEXT,
                    schedule_changes TEXT,
                    narrative_out    TEXT,
                    skipped          BOOLEAN DEFAULT FALSE,
                    skip_reason      TEXT,
                    provider         TEXT,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_messages_created
                    ON messages(created_at);

                CREATE INDEX IF NOT EXISTS idx_summaries_range
                    ON summaries(message_start, message_end);

                CREATE INDEX IF NOT EXISTS idx_triggers_fire_at
                    ON triggers(fire_at);

                CREATE INDEX IF NOT EXISTS idx_agent_actions_cycle
                    ON agent_actions(cycle_id);

                CREATE INDEX IF NOT EXISTS idx_agent_actions_status
                    ON agent_actions(status);

                CREATE INDEX IF NOT EXISTS idx_reasoning_log_created
                    ON reasoning_log(created_at);
            """)
            conn.commit()
        finally:
            conn.close()

        # Migrate existing databases that lack the max_actions_per_day column.
        # ALTER TABLE ... ADD COLUMN is safe if the column doesn't exist yet,
        # but SQLite throws an error if it does. We catch and ignore that.
        self._migrate_schedule_config()
        self._migrate_agent_narrative()

    def _migrate_schedule_config(self):
        """Add max_actions_per_day to schedule_config if it's missing."""
        conn = self._connect()
        try:
            conn.execute(
                "ALTER TABLE schedule_config "
                "ADD COLUMN max_actions_per_day INTEGER NOT NULL DEFAULT 25"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists — nothing to do
        finally:
            conn.close()

    def _migrate_agent_narrative(self):
        """
        Migrate agent_narrative from single-row (CHECK id=1) to append-only.

        The old schema enforced a single row. The new schema uses
        AUTOINCREMENT and keeps all entries. We detect the old schema
        by trying an insert with id != 1 — if it fails with a CHECK
        constraint, we need to migrate.
        """
        conn = self._connect()
        try:
            # Check if the old CHECK constraint exists by looking at the
            # table's SQL definition
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='agent_narrative'"
            ).fetchone()
            if row and "CHECK" in (row["sql"] or ""):
                # Old schema — recreate without the constraint
                conn.executescript("""
                    ALTER TABLE agent_narrative RENAME TO agent_narrative_old;
                    CREATE TABLE agent_narrative (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        narrative   TEXT NOT NULL,
                        cycle_id    TEXT NOT NULL,
                        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO agent_narrative (narrative, cycle_id, updated_at)
                        SELECT narrative, cycle_id, updated_at
                        FROM agent_narrative_old;
                    DROP TABLE agent_narrative_old;
                """)
                conn.commit()
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet or already migrated
        finally:
            conn.close()

    # --- Message Operations ---

    def add_message(self, role: str, content: str) -> int:
        """
        Store a message and return its ID.

        Every user message and assistant response gets stored here.
        Messages are never deleted — older ones get summarized and
        drop out of the active context window, but the full record
        stays in the database.

        Args:
            role: "user" or "assistant"
            content: The message text

        Returns:
            The database ID of the stored message.
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"Invalid role '{role}'. Must be 'user' or 'assistant'.")

        conn = self._connect()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
                (role, content, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_recent_messages(self, limit: int = 20) -> list[dict]:
        """
        Get the most recent messages, ordered oldest-first.

        These are the verbatim messages that get included in the
        API call's messages array. The limit controls how far back
        we go — older messages are covered by summaries instead.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of dicts with 'id', 'role', 'content', 'created_at'.
            Ordered chronologically (oldest first), which is what the
            LLM expects.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            # Reverse so oldest is first (LLM expects chronological order)
            return [dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def get_messages_since(self, after_id: int) -> list[dict]:
        """
        Get all messages with ID greater than after_id.

        Used by the summarization system to find unsummarized messages.

        Args:
            after_id: Return messages with ID strictly greater than this.

        Returns:
            List of dicts, ordered chronologically.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE id > ?
                ORDER BY id ASC
                """,
                (after_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_message_count(self) -> int:
        """Return total number of messages stored."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) as count FROM messages").fetchone()
            return row["count"]
        finally:
            conn.close()

    # --- Summary Operations ---

    def add_summary(
        self,
        summary: str,
        message_start: int,
        message_end: int,
        embedding: np.ndarray | None = None,
    ) -> int:
        """
        Store a conversation summary with its embedding.

        Summaries are generated from batches of older messages. The
        message_start and message_end fields record which messages
        were condensed into this summary, creating a clear audit trail.

        Args:
            summary: The summary text.
            message_start: ID of the first message covered.
            message_end: ID of the last message covered.
            embedding: Vector embedding of the summary (numpy array).

        Returns:
            The database ID of the stored summary.
        """
        embedding_blob = embedding.tobytes() if embedding is not None else None

        conn = self._connect()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                """
                INSERT INTO summaries (summary, message_start, message_end, embedding, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (summary, message_start, message_end, embedding_blob, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def search_summaries(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        embedding_dim: int = 768,
    ) -> list[dict]:
        """
        Find the most relevant summaries using cosine similarity.

        Loads all summary embeddings, computes similarity against the
        query, and returns the top-k matches. This is a brute-force
        search — fine for hundreds or low thousands of summaries.
        If you ever have 10,000+ summaries, consider a vector index.

        Args:
            query_embedding: The embedding of the current message/query.
            top_k: Number of summaries to return.
            embedding_dim: Dimension of the embedding vectors (must match
                the embedding model's output size).

        Returns:
            List of dicts with 'id', 'summary', 'message_start',
            'message_end', 'created_at', 'similarity'. Ordered by
            similarity descending (most relevant first).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, summary, message_start, message_end, embedding, created_at
                FROM summaries
                WHERE embedding IS NOT NULL
                """
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        results = []
        for row in rows:
            stored_embedding = np.frombuffer(row["embedding"], dtype=np.float32)
            # Guard against dimension mismatches from model changes
            if len(stored_embedding) != embedding_dim:
                continue
            similarity = _cosine_similarity(query_embedding, stored_embedding)
            results.append({
                "id": row["id"],
                "summary": row["summary"],
                "message_start": row["message_start"],
                "message_end": row["message_end"],
                "created_at": row["created_at"],
                "similarity": similarity,
            })

        # Sort by similarity (highest first) and return top-k
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    def get_all_summaries(self) -> list[dict]:
        """
        Get all summaries, ordered chronologically.

        Useful for debugging and for the /status command to show
        memory state.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, summary, message_start, message_end, created_at
                FROM summaries
                ORDER BY message_start ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_last_summarized_id(self) -> int:
        """
        Find the highest message ID that has been summarized.

        Used to determine which messages still need summarization.
        Derived from the data — no separate cursor table needed.

        Returns:
            The highest message_end across all summaries, or 0
            if no summaries exist yet.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(message_end) as last_id FROM summaries"
            ).fetchone()
            return row["last_id"] or 0
        finally:
            conn.close()

    # --- Schedule Configuration ---

    def get_schedule_config(self) -> dict | None:
        """
        Get the proactive messaging schedule, or None if not configured.

        The schedule_config table holds at most one row (enforced by
        CHECK constraint on id=1). This is the single source of truth
        for when this persona should proactively reach out.

        Returns:
            Dict with 'start_time', 'end_time', 'interval_minutes',
            'max_actions_per_day', 'updated_at', or None if not set.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT start_time, end_time, interval_minutes, "
                "max_actions_per_day, updated_at "
                "FROM schedule_config WHERE id = 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_schedule_config(
        self,
        start_time: str,
        end_time: str,
        interval_minutes: int,
        max_actions_per_day: int = 25,
    ):
        """
        Set or update the proactive messaging schedule.

        Uses INSERT OR REPLACE to upsert the single config row.

        Args:
            start_time: First check-in time as "HH:MM" (24-hour).
            end_time: Last check-in time as "HH:MM" (24-hour).
            interval_minutes: Minutes between check-ins (preserved for
                backward compat; the self-scheduling agent manages its own).
            max_actions_per_day: Cap on message/draft/execute actions per day.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO schedule_config
                    (id, start_time, end_time, interval_minutes,
                     max_actions_per_day, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (start_time, end_time, interval_minutes,
                 max_actions_per_day, now),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_recurring_triggers(self):
        """
        Delete all unfired recurring triggers.

        Called when the schedule changes so the trigger pool can be
        re-seeded with the new config. One-shot triggers (reminders,
        calendar events) are preserved — only recurring check-ins
        are cleared.
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                DELETE FROM triggers
                WHERE fired = FALSE
                  AND recurring IS NOT NULL
                """
            )
            conn.commit()
        finally:
            conn.close()

    def clear_agent_triggers(self):
        """
        Delete all unfired agent_cycle triggers.

        WARNING: This is a sledgehammer — it removes the agent's entire
        plan including targeted wake-ups (pre-meeting reminders, user-
        requested reminders, etc.). Only use for a full reset.

        For schedule config changes, use reschedule_planning_cycles()
        instead, which preserves targeted wake-ups.
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                DELETE FROM triggers
                WHERE fired = FALSE
                  AND (recurring IS NOT NULL OR type = 'agent_cycle')
                """
            )
            conn.commit()
        finally:
            conn.close()

    def reschedule_planning_cycles(self) -> int:
        """
        Remove all planning-cycle triggers. Preserves all targeted
        wake-ups (the agent's reminders, meeting prep, etc.).

        Called when operating hours change via /schedule. The old
        planning cycles are stale — the agent will create new ones
        at the new start_time via ensure_agent_has_plan().

        A planning cycle is identified by having an empty tools list
        in its JSON context: {"tools": []}. Targeted wake-ups have
        specific tools listed: {"tools": ["telegram", "calendar"]}.

        Returns:
            Number of planning cycles removed.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, context
                FROM triggers
                WHERE fired = FALSE AND type = 'agent_cycle'
                """
            ).fetchall()

            ids_to_delete = []
            for row in rows:
                try:
                    ctx = json.loads(row["context"]) if row["context"] else {}
                    tools = ctx.get("tools", [])
                    if len(tools) == 0:
                        ids_to_delete.append(row["id"])
                except (json.JSONDecodeError, TypeError):
                    # Legacy trigger or unparseable — leave it alone
                    continue

            for trigger_id in ids_to_delete:
                conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))

            conn.commit()
            return len(ids_to_delete)
        finally:
            conn.close()

    # --- Trigger Operations ---

    def add_trigger(
        self,
        trigger_type: str,
        fire_at: str,
        context: str | None = None,
        recurring: str | None = None,
    ) -> int:
        """
        Schedule a trigger to fire at a specific time.

        Args:
            trigger_type: "reminder", "calendar", "check_in", or "agent_cycle"
            fire_at: When to fire, as "YYYY-MM-DD HH:MM:SS" local time.
            context: For agent_cycle triggers, a JSON string with purpose,
                tools, and planning_cycle fields. For legacy triggers,
                a human-readable description.
            recurring: Recurrence pattern string, or None for one-shot.

        Returns:
            The database ID of the stored trigger.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO triggers (type, fire_at, context, recurring, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trigger_type, fire_at, context, recurring, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_due_triggers(self) -> list[dict]:
        """
        Get all triggers that are due to fire (fire_at <= now, not yet fired).

        Returns:
            List of trigger dicts, ordered by fire_at ascending.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, created_at
                FROM triggers
                WHERE fire_at <= ? AND fired = FALSE
                ORDER BY fire_at ASC
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_trigger_fired(self, trigger_id: int):
        """
        Mark a trigger as fired so it won't fire again.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE triggers SET fired = TRUE WHERE id = ?",
                (trigger_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def advance_recurring_trigger(self, trigger_id: int, next_fire_at: str):
        """
        Advance a recurring trigger to its next fire time.

        Instead of marking it fired, we update fire_at to the next
        occurrence so it stays in the active trigger pool.

        Args:
            trigger_id: The trigger to advance.
            next_fire_at: The next fire time as "YYYY-MM-DD HH:MM:SS".
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE triggers SET fire_at = ? WHERE id = ?",
                (next_fire_at, trigger_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_active_triggers(self) -> list[dict]:
        """
        Get all triggers that haven't been permanently fired.
        Useful for /status and debugging.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, created_at
                FROM triggers
                WHERE fired = FALSE
                ORDER BY fire_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def has_future_planning_cycle(self) -> bool:
        """
        Check whether at least one future planning cycle exists.

        A planning cycle is an agent_cycle trigger with an empty tools
        list in its JSON context: {"tools": []}. Targeted wake-ups
        (with specific tools like ["telegram"]) do NOT count.

        Used by ensure_agent_has_plan and _ensure_future_plan to
        determine whether a planning cycle needs to be seeded. The
        agent must always have at least one future planning cycle —
        without it, the agent can't discover new calendar events,
        plan its day, or schedule further wake-ups.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        active = self.get_active_triggers()

        for trigger in active:
            if trigger["fire_at"] <= now_str:
                continue  # Not in the future

            try:
                ctx = json.loads(trigger["context"]) if trigger["context"] else {}
                tools = ctx.get("tools", [])
                if len(tools) == 0:
                    return True  # Found a future planning cycle
            except (json.JSONDecodeError, TypeError):
                continue

        return False

    def get_trigger(self, trigger_id: int) -> dict | None:
        """Get a single trigger by ID, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, fired, created_at
                FROM triggers
                WHERE id = ?
                """,
                (trigger_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_trigger(self, trigger_id: int, fire_at: str, context: str):
        """
        Update a trigger's fire time and context.

        Used by the agent to modify its own scheduled wake-ups.
        Only works on unfired triggers.

        Args:
            trigger_id: The trigger to update.
            fire_at: New fire time as "YYYY-MM-DD HH:MM:SS".
            context: New context (JSON string for agent_cycle triggers).
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE triggers
                SET fire_at = ?, context = ?
                WHERE id = ? AND fired = FALSE
                """,
                (fire_at, context, trigger_id),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_trigger(self, trigger_id: int):
        """Remove a trigger entirely."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
            conn.commit()
        finally:
            conn.close()

    # --- Tool State ---

    def get_tool_state(self, tool_name: str, key: str) -> str | None:
        """
        Read a tool's persisted state value.

        Returns the value as a string, or None if the key doesn't exist.
        Tools that store complex data serialize it as JSON.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM tool_state WHERE tool_name = ? AND key = ?",
                (tool_name, key),
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_tool_state(self, tool_name: str, key: str, value: str):
        """
        Write a tool's persisted state value.

        Uses INSERT OR REPLACE so it works for both new and existing keys.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_state (tool_name, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (tool_name, key, value, now),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Agent Actions ---

    def add_agent_action(
        self,
        cycle_id: str,
        tool_name: str,
        method_name: str,
        tier: str,
        parameters: str | None = None,
        result: str | None = None,
        status: str = "completed",
    ) -> int:
        """
        Log an action taken (or proposed) by the agent.

        Every action goes through here: successful executions, failures,
        pending proposals, approvals, rejections, and expirations.

        Args:
            cycle_id: Which agent cycle produced this action.
            tool_name: The tool used (e.g. "telegram", "gmail").
            method_name: The method called (e.g. "send_message").
            tier: "observe", "message", "draft", or "execute".
            parameters: JSON string of the method arguments.
            result: The tool's return value, or error message.
            status: One of 'completed', 'failed', 'pending_approval',
                    'approved', 'rejected', 'expired'.

        Returns:
            The database ID of the logged action.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_actions
                    (cycle_id, tool_name, method_name, tier,
                     parameters, result, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cycle_id, tool_name, method_name, tier,
                 parameters, result, status, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_today_action_count(self) -> int:
        """
        Count how many message/draft/execute actions were completed today.

        Used to enforce the daily action budget. Observe-tier actions
        and schedule management don't count against the limit.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as count FROM agent_actions
                WHERE created_at >= ? || ' 00:00:00'
                  AND created_at < ? || ' 23:59:59'
                  AND status = 'completed'
                  AND tier IN ('message', 'draft', 'execute')
                """,
                (today, today),
            ).fetchone()
            return row["count"]
        finally:
            conn.close()

    def get_pending_proposals(self) -> list[dict]:
        """
        Get all execute-tier actions awaiting user approval.

        Returns actions with status 'pending_approval', ordered by
        creation time (oldest first).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, cycle_id, tool_name, method_name, tier,
                       parameters, status, created_at
                FROM agent_actions
                WHERE status = 'pending_approval'
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_proposal_status(self, action_id: int, status: str):
        """
        Update the status of a pending proposal.

        Args:
            action_id: The agent_actions row ID.
            status: New status ('approved', 'rejected', 'expired').
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE agent_actions SET status = ? WHERE id = ?",
                (status, action_id),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Agent Narrative State ---

    def get_narrative(self) -> str | None:
        """
        Read the agent's most recent narrative state.

        Returns the prose state written by the LLM at the end of the
        last agent cycle, or None if no narrative exists yet (fresh start).
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT narrative FROM agent_narrative ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["narrative"] if row else None
        finally:
            conn.close()

    def set_narrative(self, narrative: str, cycle_id: str):
        """
        Append a new narrative state entry.

        Called at the end of each agent cycle with the LLM's updated
        understanding of the current situation. Previous narratives
        are preserved as a history log.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_narrative (narrative, cycle_id, updated_at)
                VALUES (?, ?, ?)
                """,
                (narrative, cycle_id, now),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Reasoning Log ---

    def add_reasoning_log(
        self,
        cycle_id: str,
        trigger_id: int | None = None,
        trigger_purpose: str | None = None,
        tool_contexts: str | None = None,
        narrative_in: str | None = None,
        llm_response: str | None = None,
        actions_taken: str | None = None,
        schedule_changes: str | None = None,
        narrative_out: str | None = None,
        skipped: bool = False,
        skip_reason: str | None = None,
        provider: str | None = None,
    ) -> int:
        """
        Log a full reasoning trace for one agent cycle.

        Called at the end of every cycle, whether reasoning happened
        or was skipped. This is the primary debugging tool for
        understanding agent behavior.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO reasoning_log
                    (cycle_id, trigger_id, trigger_purpose, tool_contexts,
                     narrative_in, llm_response, actions_taken,
                     schedule_changes, narrative_out, skipped,
                     skip_reason, provider, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cycle_id, trigger_id, trigger_purpose, tool_contexts,
                 narrative_in, llm_response, actions_taken,
                 schedule_changes, narrative_out, skipped,
                 skip_reason, provider, now),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def cleanup_old_data(self):
        """
        Enforce retention policies. Called at the start of each agent cycle.

        - reasoning_log: delete entries older than 7 days
        - agent_actions: delete entries older than 30 days
        - agent_narrative: delete entries older than 30 days
        """
        now = datetime.now()
        reasoning_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        actions_cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM reasoning_log WHERE created_at < ?",
                (reasoning_cutoff,),
            )
            conn.execute(
                "DELETE FROM agent_actions WHERE created_at < ?",
                (actions_cutoff,),
            )
            conn.execute(
                "DELETE FROM agent_narrative WHERE updated_at < ?",
                (actions_cutoff,),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Utility ---

    def get_unsummarized_messages(self) -> list[dict]:
        """
        Get all messages that haven't been covered by any summary.

        This is the primary input for the summarization check:
        if len(memory.get_unsummarized_messages()) exceeds the
        token threshold, it's time to summarize.
        """
        last_id = self.get_last_summarized_id()
        return self.get_messages_since(last_id)

    def clear_history(self):
        """
        Delete all messages, summaries, and triggers. Used by /clear.

        This is destructive — there's no undo. The database file
        itself is preserved (with empty tables). Schedule config and
        agent narrative are intentionally preserved — they're settings
        and state, not conversation data.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM summaries")
            conn.execute("DELETE FROM triggers")
            conn.commit()
        finally:
            conn.close()