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
import logging
import sqlite3
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta

# All persona data lives under this directory
DATA_DIR = Path(__file__).parent / "data"
logger = logging.getLogger(__name__)

AGENT_OPPORTUNITY_STATUSES = {
    "candidate",
    "queued",
    "scheduled",
    "delivered",
    "accepted",
    "completed",
    "rejected",
    "dismissed",
    "expired",
    "blocked",
}

AGENT_OPPORTUNITY_RISK_LEVELS = {"low", "medium", "high"}

AGENT_OPPORTUNITY_SUPPRESSION_STATUSES = {
    "rejected",
    "dismissed",
    "expired",
    "blocked",
}

AGENT_INBOX_STATUSES = {
    "unread",
    "acted",
    "dismissed",
    "snoozed",
    "expired",
}

AGENT_INBOX_SURFACES = {
    "dashboard",
    "chat",
    "mobile_push",
    "silent",
}

MEMORY_ITEM_KINDS = {
    "fact",
    "preference",
    "commitment",
    "project",
    "constraint",
    "question",
}

MEMORY_ITEM_STATUSES = {
    "active",
    "superseded",
    "rejected",
    "expired",
}

REFLECTION_TRIGGER_DELAY_MINUTES = 5

REFLECTION_EVENT_TYPES = {
    "conversation_message",
    "step_accepted",
    "step_rejected",
    "step_completed",
    "step_abandoned",
    "inbox_item_dismissed",
    "inbox_item_snoozed",
    "inbox_item_acted",
}

SENSITIVE_MEMORY_TERMS = {
    "addiction",
    "diagnosis",
    "medical",
    "medication",
    "politics",
    "political",
    "religion",
    "religious",
    "sexuality",
}


def _truncate_log_value(value: str | None, limit: int = 240) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit] + "..."


def _load_trigger_context_json(context: str | None) -> dict | None:
    """Return a trigger's JSON context dict, or None for legacy text."""
    if not context:
        return {}
    try:
        decoded = json.loads(context)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def trigger_context_job_type(context: str | None) -> str | None:
    """
    Resolve a trigger context to a job type.

    New triggers carry explicit job_type. Older JSON triggers are interpreted
    compatibly: an empty tools list is a planning job, otherwise targeted.
    Plain-text legacy triggers return None so maintenance code does not treat
    them as planning cycles.
    """
    ctx = _load_trigger_context_json(context)
    if ctx is None:
        return None
    explicit = ctx.get("job_type")
    if explicit:
        return str(explicit)
    tools = ctx.get("tools", [])
    if isinstance(tools, list) and len(tools) == 0:
        return "planning"
    return "targeted_action"


def is_planning_trigger_context(context: str | None) -> bool:
    """Return whether a trigger context represents a planning job."""
    return trigger_context_job_type(context) == "planning"


@dataclass(frozen=True)
class MessageScope:
    """
    Identifies which dashboard conversation a message belongs to.

    The default scope is the normal persona chat. Goal and step scopes are
    focused dashboard threads whose rows live in the same persona database.
    """

    scope_type: str = "default"
    scope_id: int | None = None

    def __post_init__(self):
        if self.scope_type not in ("default", "goal", "step"):
            raise ValueError(
                "scope_type must be one of 'default', 'goal', or 'step'"
            )
        if self.scope_type == "default" and self.scope_id is not None:
            raise ValueError("default scope must not have a scope_id")
        if self.scope_type in ("goal", "step"):
            if self.scope_id is None:
                raise ValueError(f"{self.scope_type} scope requires a scope_id")
            if self.scope_id < 1:
                raise ValueError("scope_id must be a positive integer")

    @classmethod
    def default(cls) -> "MessageScope":
        return cls()

    @classmethod
    def goal(cls, goal_id: int) -> "MessageScope":
        return cls("goal", goal_id)

    @classmethod
    def step(cls, step_id: int) -> "MessageScope":
        return cls("step", step_id)

    @property
    def label(self) -> str:
        if self.scope_type == "default":
            return "default"
        return f"{self.scope_type}:{self.scope_id}"


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
                    scope_type  TEXT NOT NULL DEFAULT 'default',
                    scope_id    INTEGER,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary         TEXT NOT NULL,
                    message_start   INTEGER NOT NULL,
                    message_end     INTEGER NOT NULL,
                    embedding       BLOB,
                    scope_type      TEXT NOT NULL DEFAULT 'default',
                    scope_id        INTEGER,
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

                CREATE TABLE IF NOT EXISTS agent_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type      TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    source_id       TEXT,
                    persona         TEXT,
                    payload_json    TEXT NOT NULL,
                    occurred_at     TIMESTAMP NOT NULL,
                    recorded_at     TIMESTAMP NOT NULL,
                    trust_level     TEXT NOT NULL DEFAULT 'local',
                    processed_at    TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_id          INTEGER UNIQUE,
                    job_type            TEXT NOT NULL,
                    purpose             TEXT NOT NULL,
                    tool_names_json     TEXT NOT NULL DEFAULT '[]',
                    opportunity_id      INTEGER,
                    delivery_policy     TEXT,
                    status              TEXT NOT NULL DEFAULT 'pending',
                    lease_owner         TEXT,
                    lease_expires_at    TIMESTAMP,
                    attempt_count       INTEGER NOT NULL DEFAULT 0,
                    max_attempts        INTEGER NOT NULL DEFAULT 1,
                    last_error          TEXT,
                    created_at          TIMESTAMP NOT NULL,
                    updated_at          TIMESTAMP NOT NULL,
                    started_at          TIMESTAMP,
                    completed_at        TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agent_opportunities (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind                 TEXT NOT NULL,
                    title                TEXT NOT NULL,
                    rationale            TEXT NOT NULL,
                    evidence_event_ids   TEXT NOT NULL DEFAULT '[]',
                    goal_id              INTEGER,
                    step_id              INTEGER,
                    status               TEXT NOT NULL,
                    urgency              INTEGER NOT NULL,
                    impact               INTEGER NOT NULL,
                    confidence           INTEGER NOT NULL,
                    attention_cost       INTEGER NOT NULL,
                    risk_level           TEXT NOT NULL,
                    proposed_action_json TEXT,
                    duplicate_key        TEXT NOT NULL,
                    deliver_after        TIMESTAMP,
                    expires_at           TIMESTAMP,
                    created_at           TIMESTAMP NOT NULL,
                    updated_at           TIMESTAMP NOT NULL,

                    CHECK (status IN (
                        'candidate', 'queued', 'scheduled', 'delivered',
                        'accepted', 'completed', 'rejected', 'dismissed',
                        'expired', 'blocked'
                    )),
                    CHECK (risk_level IN ('low', 'medium', 'high'))
                );

                CREATE TABLE IF NOT EXISTS agent_inbox_items (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id  INTEGER,
                    priority        INTEGER NOT NULL,
                    surface         TEXT NOT NULL,
                    title           TEXT NOT NULL,
                    body            TEXT NOT NULL,
                    actions_json    TEXT NOT NULL DEFAULT '[]',
                    status          TEXT NOT NULL DEFAULT 'unread',
                    duplicate_key   TEXT NOT NULL,
                    snoozed_until   TIMESTAMP,
                    expires_at      TIMESTAMP,
                    created_at      TIMESTAMP NOT NULL,
                    updated_at      TIMESTAMP NOT NULL,

                    CHECK (status IN (
                        'unread', 'acted', 'dismissed', 'snoozed', 'expired'
                    )),
                    CHECK (surface IN (
                        'dashboard', 'chat', 'mobile_push', 'silent'
                    ))
                );

                CREATE TABLE IF NOT EXISTS memory_items (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind                TEXT NOT NULL,
                    subject             TEXT NOT NULL,
                    content             TEXT NOT NULL,
                    confidence          INTEGER NOT NULL,
                    evidence_event_ids  TEXT NOT NULL DEFAULT '[]',
                    status              TEXT NOT NULL DEFAULT 'active',
                    source              TEXT NOT NULL DEFAULT 'reflection',
                    expires_at          TIMESTAMP,
                    created_at          TIMESTAMP NOT NULL,
                    updated_at          TIMESTAMP NOT NULL,

                    CHECK (kind IN (
                        'fact', 'preference', 'commitment', 'project',
                        'constraint', 'question'
                    )),
                    CHECK (status IN (
                        'active', 'superseded', 'rejected', 'expired'
                    )),
                    CHECK (confidence BETWEEN 1 AND 5)
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

                CREATE INDEX IF NOT EXISTS idx_agent_events_type_recorded
                    ON agent_events(event_type, recorded_at);

                CREATE INDEX IF NOT EXISTS idx_agent_events_source
                    ON agent_events(source, source_id);

                CREATE INDEX IF NOT EXISTS idx_agent_jobs_status
                    ON agent_jobs(status, lease_expires_at);

                CREATE INDEX IF NOT EXISTS idx_agent_jobs_trigger
                    ON agent_jobs(trigger_id);

                CREATE INDEX IF NOT EXISTS idx_agent_opportunities_status
                    ON agent_opportunities(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_agent_opportunities_duplicate
                    ON agent_opportunities(duplicate_key, status);

                CREATE INDEX IF NOT EXISTS idx_agent_opportunities_goal
                    ON agent_opportunities(goal_id, status);

                CREATE INDEX IF NOT EXISTS idx_agent_inbox_items_status
                    ON agent_inbox_items(surface, status, priority, updated_at);

                CREATE INDEX IF NOT EXISTS idx_agent_inbox_items_opportunity
                    ON agent_inbox_items(opportunity_id, status);

                CREATE INDEX IF NOT EXISTS idx_agent_inbox_items_duplicate
                    ON agent_inbox_items(duplicate_key, status);

                CREATE INDEX IF NOT EXISTS idx_memory_items_status_kind
                    ON memory_items(status, kind, updated_at);

                CREATE INDEX IF NOT EXISTS idx_memory_items_subject
                    ON memory_items(subject, status, updated_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_active_subject
                    ON memory_items(kind, subject)
                    WHERE status = 'active';
            """)
            conn.commit()
        finally:
            conn.close()

        # Migrate existing databases that lack the max_actions_per_day column.
        # ALTER TABLE ... ADD COLUMN is safe if the column doesn't exist yet,
        # but SQLite throws an error if it does. We catch and ignore that.
        self._migrate_schedule_config()
        self._migrate_agent_narrative()
        self._migrate_message_scopes()

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

    def _migrate_message_scopes(self):
        """Add dashboard scope columns to existing persona memory tables."""
        conn = self._connect()
        try:
            self._add_column_if_missing(
                conn,
                "messages",
                "scope_type",
                "TEXT NOT NULL DEFAULT 'default'",
            )
            self._add_column_if_missing(conn, "messages", "scope_id", "INTEGER")
            self._add_column_if_missing(
                conn,
                "summaries",
                "scope_type",
                "TEXT NOT NULL DEFAULT 'default'",
            )
            self._add_column_if_missing(conn, "summaries", "scope_id", "INTEGER")
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_messages_scope_id
                    ON messages(scope_type, scope_id, id);

                CREATE INDEX IF NOT EXISTS idx_summaries_scope_range
                    ON summaries(scope_type, scope_id, message_start, message_end);
            """)
            conn.commit()
        finally:
            conn.close()

    def _add_column_if_missing(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_sql: str,
    ):
        """Add one column to a SQLite table when an older DB lacks it."""
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            )

    # --- Message Operations ---

    def _normalize_scope(self, scope: MessageScope | None = None) -> MessageScope:
        return scope if scope is not None else MessageScope.default()

    def add_message(
        self,
        role: str,
        content: str,
        scope: MessageScope | None = None,
    ) -> int:
        """
        Store a message and return its ID.

        Every user message and assistant response gets stored here.
        Messages are never deleted — older ones get summarized and
        drop out of the active context window, but the full record
        stays in the database.

        Args:
            role: "user" or "assistant"
            content: The message text
            scope: Dashboard chat scope. Defaults to the normal persona chat.

        Returns:
            The database ID of the stored message.
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"Invalid role '{role}'. Must be 'user' or 'assistant'.")

        resolved_scope = self._normalize_scope(scope)
        message_id = None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (role, content, scope_type, scope_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    role,
                    content,
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    now,
                ),
            )
            conn.commit()
            message_id = cursor.lastrowid
        finally:
            conn.close()

        assert message_id is not None
        self.add_agent_event(
            event_type="conversation_message",
            source="conversation",
            source_id=str(message_id),
            payload={
                "message_id": message_id,
                "role": role,
                "content": content,
                "scope_type": resolved_scope.scope_type,
                "scope_id": resolved_scope.scope_id,
            },
            occurred_at=now,
            schedule_reflection=(role == "user"),
        )
        return message_id

    def get_recent_messages(
        self,
        limit: int = 20,
        scope: MessageScope | None = None,
    ) -> list[dict]:
        """
        Get the most recent messages, ordered oldest-first.

        These are the verbatim messages that get included in the
        API call's messages array. The limit controls how far back
        we go — older messages are covered by summaries instead.

        Args:
            limit: Maximum number of messages to return.
            scope: Dashboard chat scope. Defaults to the normal persona chat.

        Returns:
            List of dicts with 'id', 'role', 'content', 'created_at'.
            Ordered chronologically (oldest first), which is what the
            LLM expects.
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, scope_type, scope_id, created_at
                FROM messages
                WHERE scope_type = ?
                  AND (
                      (? IS NULL AND scope_id IS NULL)
                      OR scope_id = ?
                  )
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    resolved_scope.scope_id,
                    limit,
                ),
            ).fetchall()
            # Reverse so oldest is first (LLM expects chronological order)
            return [dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def get_messages_since(
        self,
        after_id: int,
        scope: MessageScope | None = None,
    ) -> list[dict]:
        """
        Get all messages with ID greater than after_id.

        Used by the summarization system to find unsummarized messages.

        Args:
            after_id: Return messages with ID strictly greater than this.
            scope: Dashboard chat scope. Defaults to the normal persona chat.

        Returns:
            List of dicts, ordered chronologically.
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, scope_type, scope_id, created_at
                FROM messages
                WHERE id > ?
                  AND scope_type = ?
                  AND (
                      (? IS NULL AND scope_id IS NULL)
                      OR scope_id = ?
                  )
                ORDER BY id ASC
                """,
                (
                    after_id,
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    resolved_scope.scope_id,
                ),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_messages_before(
        self,
        before_id: int,
        limit: int = 20,
        scope: MessageScope | None = None,
    ) -> list[dict]:
        """
        Get messages before a known message id, ordered oldest-first.

        Dashboard chat uses this to load older history when Zach scrolls up.
        The scope filter matches get_recent_messages so scoped threads remain
        isolated.
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, role, content, scope_type, scope_id, created_at
                FROM messages
                WHERE id < ?
                  AND scope_type = ?
                  AND (
                      (? IS NULL AND scope_id IS NULL)
                      OR scope_id = ?
                  )
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    before_id,
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    resolved_scope.scope_id,
                    limit,
                ),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def get_message_count(self, scope: MessageScope | None = None) -> int:
        """Return total number of messages stored for a scope."""
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as count
                FROM messages
                WHERE scope_type = ?
                  AND (
                      (? IS NULL AND scope_id IS NULL)
                      OR scope_id = ?
                  )
                """,
                (
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    resolved_scope.scope_id,
                ),
            ).fetchone()
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
        scope: MessageScope | None = None,
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
            scope: Dashboard chat scope. Defaults to the normal persona chat.

        Returns:
            The database ID of the stored summary.
        """
        resolved_scope = self._normalize_scope(scope)
        embedding_blob = embedding.tobytes() if embedding is not None else None

        conn = self._connect()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                """
                INSERT INTO summaries
                    (summary, message_start, message_end, embedding,
                     scope_type, scope_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary,
                    message_start,
                    message_end,
                    embedding_blob,
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    now,
                ),
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
        scope: MessageScope | None = None,
        include_default: bool = False,
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
            scope: Dashboard chat scope. Defaults to the normal persona chat.
            include_default: For goal/step scopes, also search default-scope
                summaries as lower-priority background.

        Returns:
            List of dicts with 'id', 'summary', 'message_start',
            'message_end', 'created_at', 'similarity'. Ordered by
            similarity descending (most relevant first).
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            if include_default and resolved_scope.scope_type != "default":
                rows = conn.execute(
                    """
                    SELECT id, summary, message_start, message_end, embedding,
                           scope_type, scope_id, created_at
                    FROM summaries
                    WHERE embedding IS NOT NULL
                      AND (
                          (
                              scope_type = ?
                              AND (
                                  (? IS NULL AND scope_id IS NULL)
                                  OR scope_id = ?
                              )
                          )
                          OR (scope_type = 'default' AND scope_id IS NULL)
                      )
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, summary, message_start, message_end, embedding,
                           scope_type, scope_id, created_at
                    FROM summaries
                    WHERE embedding IS NOT NULL
                      AND scope_type = ?
                      AND (
                          (? IS NULL AND scope_id IS NULL)
                          OR scope_id = ?
                      )
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
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
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "created_at": row["created_at"],
                "similarity": similarity,
            })

        # Sort by similarity (highest first) and return top-k
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    def get_all_summaries(
        self,
        scope: MessageScope | None = None,
        include_default: bool = False,
    ) -> list[dict]:
        """
        Get all summaries, ordered chronologically.

        Useful for debugging and for the /status command to show
        memory state.
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            if include_default and resolved_scope.scope_type != "default":
                rows = conn.execute(
                    """
                    SELECT id, summary, message_start, message_end,
                           scope_type, scope_id, created_at
                    FROM summaries
                    WHERE (
                        (
                            scope_type = ?
                            AND (
                                (? IS NULL AND scope_id IS NULL)
                                OR scope_id = ?
                            )
                        )
                        OR (scope_type = 'default' AND scope_id IS NULL)
                    )
                    ORDER BY message_start ASC
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, summary, message_start, message_end,
                           scope_type, scope_id, created_at
                    FROM summaries
                    WHERE scope_type = ?
                      AND (
                          (? IS NULL AND scope_id IS NULL)
                          OR scope_id = ?
                      )
                    ORDER BY message_start ASC
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_last_summarized_id(self, scope: MessageScope | None = None) -> int:
        """
        Find the highest message ID that has been summarized.

        Used to determine which messages still need summarization.
        Derived from the data — no separate cursor table needed.

        Returns:
            The highest message_end across all summaries, or 0
            if no summaries exist yet.
        """
        resolved_scope = self._normalize_scope(scope)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT MAX(message_end) as last_id
                FROM summaries
                WHERE scope_type = ?
                  AND (
                      (? IS NULL AND scope_id IS NULL)
                      OR scope_id = ?
                  )
                """,
                (
                    resolved_scope.scope_type,
                    resolved_scope.scope_id,
                    resolved_scope.scope_id,
                ),
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
            logger.info(
                "schedule_config_updated persona=%s start=%s end=%s "
                "interval_minutes=%s max_actions_per_day=%s",
                self.persona_name,
                start_time,
                end_time,
                interval_minutes,
                max_actions_per_day,
            )
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
            rows = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring
                FROM triggers
                WHERE fired = FALSE
                  AND recurring IS NOT NULL
                """
            ).fetchall()
            conn.execute(
                """
                DELETE FROM triggers
                WHERE fired = FALSE
                  AND recurring IS NOT NULL
                """
            )
            conn.commit()
            if rows:
                logger.info(
                    "recurring_triggers_cleared persona=%s trigger_ids=%s",
                    self.persona_name,
                    [row["id"] for row in rows],
                )
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
            rows = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring
                FROM triggers
                WHERE fired = FALSE
                  AND (recurring IS NOT NULL OR type = 'agent_cycle')
                """
            ).fetchall()
            conn.execute(
                """
                DELETE FROM triggers
                WHERE fired = FALSE
                  AND (recurring IS NOT NULL OR type = 'agent_cycle')
                """
            )
            conn.commit()
            if rows:
                logger.info(
                    "agent_triggers_cleared persona=%s trigger_ids=%s",
                    self.persona_name,
                    [row["id"] for row in rows],
                )
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
                SELECT id, fire_at, context
                FROM triggers
                WHERE fired = FALSE AND type = 'agent_cycle'
                """
            ).fetchall()

            ids_to_delete = []
            for row in rows:
                try:
                    if is_planning_trigger_context(row["context"]):
                        ids_to_delete.append(row["id"])
                except (json.JSONDecodeError, TypeError):
                    # Legacy trigger or unparseable — leave it alone
                    continue

            for trigger_id in ids_to_delete:
                conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))

            conn.commit()
            if ids_to_delete:
                logger.info(
                    "planning_triggers_rescheduled persona=%s deleted_ids=%s",
                    self.persona_name,
                    ids_to_delete,
                )
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
            trigger_id = cursor.lastrowid
            logger.info(
                "trigger_added persona=%s id=%s type=%s fire_at=%s "
                "recurring=%s context=%s",
                self.persona_name,
                trigger_id,
                trigger_type,
                fire_at,
                recurring,
                _truncate_log_value(context),
            )
            return trigger_id
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
            trigger = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, fired
                FROM triggers
                WHERE id = ?
                """,
                (trigger_id,),
            ).fetchone()
            cursor = conn.execute(
                "UPDATE triggers SET fired = TRUE WHERE id = ?",
                (trigger_id,),
            )
            conn.commit()
            logger.info(
                "trigger_marked_fired persona=%s id=%s updated=%s "
                "fire_at=%s context=%s",
                self.persona_name,
                trigger_id,
                cursor.rowcount,
                trigger["fire_at"] if trigger else None,
                _truncate_log_value(trigger["context"] if trigger else None),
            )
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
            trigger = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, fired
                FROM triggers
                WHERE id = ?
                """,
                (trigger_id,),
            ).fetchone()
            cursor = conn.execute(
                "UPDATE triggers SET fire_at = ? WHERE id = ?",
                (next_fire_at, trigger_id),
            )
            conn.commit()
            logger.info(
                "recurring_trigger_advanced persona=%s id=%s updated=%s "
                "old_fire_at=%s new_fire_at=%s context=%s",
                self.persona_name,
                trigger_id,
                cursor.rowcount,
                trigger["fire_at"] if trigger else None,
                next_fire_at,
                _truncate_log_value(trigger["context"] if trigger else None),
            )
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
                if is_planning_trigger_context(trigger["context"]):
                    return True  # Found a future planning cycle
            except (json.JSONDecodeError, TypeError):
                continue

        return False

    def has_future_job_type(self, job_type: str) -> bool:
        """Return whether any active future trigger already carries this job type."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for trigger in self.get_active_triggers():
            if trigger["fire_at"] <= now_str:
                continue
            if trigger_context_job_type(trigger.get("context")) == job_type:
                return True
        return False

    def ensure_reflection_trigger(
        self,
        delay_minutes: int = REFLECTION_TRIGGER_DELAY_MINUTES,
    ) -> int | None:
        """
        Ensure a future reflection trigger exists for recent feedback processing.

        Reflection is silent background work, so it is scheduled outside the
        user's normal planning cadence whenever new reflectable evidence arrives.
        """
        if self.has_future_job_type("reflection"):
            return None
        fire_at = (
            datetime.now() + timedelta(minutes=max(1, int(delay_minutes)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        context = json.dumps(
            {
                "purpose": "Reflection cycle - process recent events into memory",
                "job_type": "reflection",
                "tools": [],
            }
        )
        return self.add_trigger("agent_cycle", fire_at, context=context, recurring=None)

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
            trigger = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, fired
                FROM triggers
                WHERE id = ?
                """,
                (trigger_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE triggers
                SET fire_at = ?, context = ?
                WHERE id = ? AND fired = FALSE
                """,
                (fire_at, context, trigger_id),
            )
            conn.commit()
            logger.info(
                "trigger_updated persona=%s id=%s updated=%s "
                "old_fire_at=%s new_fire_at=%s old_context=%s new_context=%s",
                self.persona_name,
                trigger_id,
                cursor.rowcount,
                trigger["fire_at"] if trigger else None,
                fire_at,
                _truncate_log_value(trigger["context"] if trigger else None),
                _truncate_log_value(context),
            )
        finally:
            conn.close()

    def delete_trigger(self, trigger_id: int):
        """Remove a trigger entirely."""
        conn = self._connect()
        try:
            trigger = conn.execute(
                """
                SELECT id, type, fire_at, context, recurring, fired
                FROM triggers
                WHERE id = ?
                """,
                (trigger_id,),
            ).fetchone()
            cursor = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
            conn.commit()
            logger.info(
                "trigger_deleted persona=%s id=%s deleted=%s type=%s "
                "fire_at=%s recurring=%s fired=%s context=%s",
                self.persona_name,
                trigger_id,
                cursor.rowcount,
                trigger["type"] if trigger else None,
                trigger["fire_at"] if trigger else None,
                trigger["recurring"] if trigger else None,
                trigger["fired"] if trigger else None,
                _truncate_log_value(trigger["context"] if trigger else None),
            )
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

    # --- Agent Events ---

    def add_agent_event(
        self,
        event_type: str,
        source: str,
        payload: dict | list | str | int | float | bool | None,
        source_id: str | None = None,
        persona: str | None = None,
        occurred_at: str | None = None,
        trust_level: str = "local",
        processed_at: str | None = None,
        schedule_reflection: bool | None = None,
    ) -> int:
        """
        Append one durable event to the agent event log.

        The event log is intentionally append-only. Later phases can mark rows
        processed, but should not rewrite the original observation payload.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_events
                    (event_type, source, source_id, persona, payload_json,
                     occurred_at, recorded_at, trust_level, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    source,
                    source_id,
                    persona or self.persona_name,
                    payload_json,
                    occurred_at or now,
                    now,
                    trust_level,
                    processed_at,
                ),
            )
            conn.commit()
            event_id = cursor.lastrowid
        finally:
            conn.close()

        if schedule_reflection is None:
            schedule_reflection = event_type in REFLECTION_EVENT_TYPES
        if schedule_reflection:
            self.ensure_reflection_trigger()
        return event_id

    def get_agent_events(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent agent events, newest first."""
        clauses = []
        params: list = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT id, event_type, source, source_id, persona, payload_json,
                       occurred_at, recorded_at, trust_level, processed_at
                FROM agent_events
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_unprocessed_agent_events(
        self,
        event_types: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return unprocessed agent events, oldest first."""
        clauses = ["processed_at IS NULL"]
        params: list = []
        if event_types:
            placeholders = ", ".join("?" for _ in sorted(event_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(event_types))
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT id, event_type, source, source_id, persona, payload_json,
                       occurred_at, recorded_at, trust_level, processed_at
                FROM agent_events
                {where}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_agent_events_processed(
        self,
        event_ids: list[int],
        processed_at: str | None = None,
    ) -> int:
        """Mark a batch of agent events processed."""
        cleaned = [int(event_id) for event_id in event_ids if int(event_id) > 0]
        if not cleaned:
            return 0
        timestamp = processed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ", ".join("?" for _ in cleaned)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"""
                UPDATE agent_events
                SET processed_at = ?
                WHERE id IN ({placeholders})
                """,
                [timestamp, *cleaned],
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # --- Agent Jobs ---

    def add_agent_job(
        self,
        job_type: str,
        purpose: str,
        tool_names: list[str] | None = None,
        trigger_id: int | None = None,
        opportunity_id: int | None = None,
        delivery_policy: str | None = None,
        max_attempts: int = 1,
    ) -> int:
        """Create explicit job metadata for an agent wake-up."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_jobs
                    (trigger_id, job_type, purpose, tool_names_json,
                     opportunity_id, delivery_policy, status, attempt_count,
                     max_attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    trigger_id,
                    job_type,
                    purpose,
                    json.dumps(tool_names or [], ensure_ascii=True),
                    opportunity_id,
                    delivery_policy,
                    max(1, int(max_attempts)),
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_agent_job(self, job_id: int) -> dict | None:
        """Return one agent job by id."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, trigger_id, job_type, purpose, tool_names_json,
                       opportunity_id, delivery_policy, status, lease_owner,
                       lease_expires_at, attempt_count, max_attempts,
                       last_error, created_at, updated_at, started_at,
                       completed_at
                FROM agent_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_agent_job_for_trigger(self, trigger_id: int) -> dict | None:
        """Return the job metadata for a trigger, if it exists."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, trigger_id, job_type, purpose, tool_names_json,
                       opportunity_id, delivery_policy, status, lease_owner,
                       lease_expires_at, attempt_count, max_attempts,
                       last_error, created_at, updated_at, started_at,
                       completed_at
                FROM agent_jobs
                WHERE trigger_id = ?
                """,
                (trigger_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ensure_agent_job_for_trigger(
        self,
        trigger: dict,
        job_type: str,
        purpose: str,
        tool_names: list[str],
        max_attempts: int = 1,
    ) -> dict:
        """Create or return the explicit job record for a scheduler trigger."""
        trigger_id = trigger.get("id")
        if trigger_id is None:
            job_id = self.add_agent_job(
                job_type=job_type,
                purpose=purpose,
                tool_names=tool_names,
                max_attempts=max_attempts,
            )
            job = self.get_agent_job(job_id)
            assert job is not None
            return job

        existing = self.get_agent_job_for_trigger(int(trigger_id))
        if existing:
            return existing

        job_id = self.add_agent_job(
            job_type=job_type,
            purpose=purpose,
            tool_names=tool_names,
            trigger_id=int(trigger_id),
            max_attempts=max_attempts,
        )
        job = self.get_agent_job(job_id)
        assert job is not None
        return job

    def acquire_agent_job(
        self,
        job_id: int,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> bool:
        """
        Lease a pending/retryable job.

        A previously leased job can be acquired again after its lease expires,
        which gives the scheduler a crash-safe recovery path.
        """
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE agent_jobs
                SET status = 'leased',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ?
                  AND (
                      status IN ('pending', 'failed_retryable')
                      OR (status = 'leased' AND lease_expires_at < ?)
                  )
                """,
                (lease_owner, lease_until, now, now, job_id, now),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def complete_agent_job(self, job_id: int, receipt: dict | None = None):
        """Mark a leased job completed and write a completion event."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_jobs
                SET status = 'completed',
                    completed_at = ?,
                    updated_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

        if receipt is not None:
            self.add_agent_event(
                event_type="job_completed",
                source="agent_job",
                source_id=str(job_id),
                payload=receipt,
            )

    def fail_agent_job(
        self,
        job_id: int,
        error: str,
        retryable: bool = True,
    ) -> str:
        """Mark a job failed and return its resulting status."""
        job = self.get_agent_job(job_id)
        if job is None:
            raise ValueError(f"Agent job #{job_id} not found")

        if retryable and job["attempt_count"] < job["max_attempts"]:
            status = "failed_retryable"
        else:
            status = "failed_terminal"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_jobs
                SET status = ?,
                    last_error = ?,
                    updated_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE id = ?
                """,
                (status, error, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.add_agent_event(
            event_type="job_failed",
            source="agent_job",
            source_id=str(job_id),
            payload={"error": error, "status": status, "retryable": retryable},
        )
        return status

    # --- Agent Opportunities ---

    def add_agent_opportunity(
        self,
        kind: str,
        title: str,
        rationale: str,
        evidence_event_ids: list[int] | None = None,
        goal_id: int | None = None,
        step_id: int | None = None,
        status: str = "candidate",
        urgency: int = 3,
        impact: int = 3,
        confidence: int = 3,
        attention_cost: int = 2,
        risk_level: str = "low",
        proposed_action: dict | list | None = None,
        duplicate_key: str | None = None,
        deliver_after: str | None = None,
        expires_at: str | None = None,
    ) -> int:
        """Create a durable candidate opportunity for later policy/delivery."""
        self._validate_opportunity_status(status)
        self._validate_opportunity_risk(risk_level)
        scores = {
            "urgency": self._validate_opportunity_score(urgency, "urgency"),
            "impact": self._validate_opportunity_score(impact, "impact"),
            "confidence": self._validate_opportunity_score(confidence, "confidence"),
            "attention_cost": self._validate_opportunity_score(
                attention_cost,
                "attention_cost",
            ),
        }

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duplicate = duplicate_key or self._default_opportunity_duplicate_key(
            kind,
            title,
            goal_id,
            step_id,
        )
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_opportunities
                    (kind, title, rationale, evidence_event_ids, goal_id,
                     step_id, status, urgency, impact, confidence,
                     attention_cost, risk_level, proposed_action_json,
                     duplicate_key, deliver_after, expires_at, created_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    title,
                    rationale,
                    json.dumps(evidence_event_ids or [], ensure_ascii=True),
                    goal_id,
                    step_id,
                    status,
                    scores["urgency"],
                    scores["impact"],
                    scores["confidence"],
                    scores["attention_cost"],
                    risk_level,
                    json.dumps(proposed_action, ensure_ascii=True, sort_keys=True)
                    if proposed_action is not None
                    else None,
                    duplicate,
                    deliver_after,
                    expires_at,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_agent_opportunity(self, opportunity_id: int) -> dict | None:
        """Return one opportunity by id."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM agent_opportunities
                WHERE id = ?
                """,
                (opportunity_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_agent_opportunity_by_duplicate_key(
        self,
        duplicate_key: str,
    ) -> dict | None:
        """Return the newest opportunity with the same duplicate key."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM agent_opportunities
                WHERE duplicate_key = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (duplicate_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_agent_opportunities(
        self,
        status: str | None = None,
        kind: str | None = None,
        goal_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent opportunities, newest first."""
        clauses = []
        params: list = []
        if status is not None:
            self._validate_opportunity_status(status)
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM agent_opportunities
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_agent_opportunity(
        self,
        opportunity_id: int,
        status: str | None = None,
        step_id: int | None = None,
        rationale: str | None = None,
        urgency: int | None = None,
        impact: int | None = None,
        confidence: int | None = None,
        attention_cost: int | None = None,
        risk_level: str | None = None,
        proposed_action: dict | list | None = None,
    ) -> bool:
        """Update mutable policy/delivery fields on an opportunity."""
        existing = self.get_agent_opportunity(opportunity_id)
        if existing is None:
            return False

        updates = []
        params = []
        if status is not None:
            self._validate_opportunity_status(status)
            updates.append("status = ?")
            params.append(status)
        if step_id is not None:
            updates.append("step_id = ?")
            params.append(step_id)
        if rationale is not None:
            updates.append("rationale = ?")
            params.append(rationale)
        for column, value in (
            ("urgency", urgency),
            ("impact", impact),
            ("confidence", confidence),
            ("attention_cost", attention_cost),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(self._validate_opportunity_score(value, column))
        if risk_level is not None:
            self._validate_opportunity_risk(risk_level)
            updates.append("risk_level = ?")
            params.append(risk_level)
        if proposed_action is not None:
            updates.append("proposed_action_json = ?")
            params.append(
                json.dumps(proposed_action, ensure_ascii=True, sort_keys=True)
            )
        if not updates:
            return True

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now)
        params.append(opportunity_id)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"""
                UPDATE agent_opportunities
                SET {", ".join(updates)}
                WHERE id = ?
                """,
                params,
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def _validate_opportunity_status(self, status: str):
        if status not in AGENT_OPPORTUNITY_STATUSES:
            choices = ", ".join(sorted(AGENT_OPPORTUNITY_STATUSES))
            raise ValueError(
                f"Invalid opportunity status '{status}'. Expected one of: {choices}"
            )

    def _validate_opportunity_risk(self, risk_level: str):
        if risk_level not in AGENT_OPPORTUNITY_RISK_LEVELS:
            choices = ", ".join(sorted(AGENT_OPPORTUNITY_RISK_LEVELS))
            raise ValueError(
                f"Invalid opportunity risk_level '{risk_level}'. "
                f"Expected one of: {choices}"
            )

    def _validate_opportunity_score(self, value: int, label: str) -> int:
        score = int(value)
        if score < 0 or score > 5:
            raise ValueError(f"{label} must be between 0 and 5")
        return score

    def _default_opportunity_duplicate_key(
        self,
        kind: str,
        title: str,
        goal_id: int | None,
        step_id: int | None,
    ) -> str:
        normalized_title = " ".join(title.casefold().split())
        return f"{kind}:goal={goal_id or ''}:step={step_id or ''}:{normalized_title}"

    # --- Agent Inbox Items ---

    def add_agent_inbox_item(
        self,
        title: str,
        body: str,
        actions: list[dict] | None = None,
        opportunity_id: int | None = None,
        priority: int = 3,
        surface: str = "dashboard",
        status: str = "unread",
        duplicate_key: str | None = None,
        snoozed_until: str | None = None,
        expires_at: str | None = None,
    ) -> int:
        """Create a dashboard delivery item and return its row id."""
        self._validate_inbox_status(status)
        self._validate_inbox_surface(surface)
        priority = self._validate_opportunity_score(priority, "priority")
        title = title.strip()
        body = body.strip()
        if not title:
            raise ValueError("title cannot be empty")
        if not body:
            raise ValueError("body cannot be empty")
        duplicate = duplicate_key or self._default_inbox_duplicate_key(
            opportunity_id,
            title,
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_inbox_items
                    (opportunity_id, priority, surface, title, body,
                     actions_json, status, duplicate_key, snoozed_until,
                     expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    priority,
                    surface,
                    title,
                    body,
                    json.dumps(actions or [], ensure_ascii=True, sort_keys=True),
                    status,
                    duplicate,
                    snoozed_until,
                    expires_at,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_agent_inbox_item(self, item_id: int) -> dict | None:
        """Return one inbox item by id."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM agent_inbox_items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_agent_inbox_item_by_duplicate_key(
        self,
        duplicate_key: str,
    ) -> dict | None:
        """Return the newest inbox item with this duplicate key."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM agent_inbox_items
                WHERE duplicate_key = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (duplicate_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_agent_inbox_items(
        self,
        status: str | None = "unread",
        surface: str | None = "dashboard",
        limit: int = 20,
    ) -> list[dict]:
        """Return inbox items, highest priority first."""
        clauses = []
        params: list = []
        if status is not None:
            self._validate_inbox_status(status)
            if status == "unread":
                now_for_status = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                clauses.append(
                    "(status = ? OR (status = 'snoozed' AND snoozed_until <= ?))"
                )
                params.extend([status, now_for_status])
            else:
                clauses.append("status = ?")
                params.append(status)
        if surface is not None:
            self._validate_inbox_surface(surface)
            clauses.append("surface = ?")
            params.append(surface)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clauses.append("(snoozed_until IS NULL OR snoozed_until <= ?)")
        params.append(now)
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM agent_inbox_items
                {where}
                ORDER BY priority DESC, updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_agent_inbox_item(
        self,
        item_id: int,
        status: str | None = None,
        priority: int | None = None,
        title: str | None = None,
        body: str | None = None,
        actions: list[dict] | None = None,
        snoozed_until: str | None = None,
        expires_at: str | None = None,
    ) -> bool:
        """Update mutable delivery fields on an inbox item."""
        existing = self.get_agent_inbox_item(item_id)
        if existing is None:
            return False

        updates = []
        params = []
        if status is not None:
            self._validate_inbox_status(status)
            updates.append("status = ?")
            params.append(status)
        if priority is not None:
            updates.append("priority = ?")
            params.append(self._validate_opportunity_score(priority, "priority"))
        if title is not None:
            updates.append("title = ?")
            params.append(title.strip())
        if body is not None:
            updates.append("body = ?")
            params.append(body.strip())
        if actions is not None:
            updates.append("actions_json = ?")
            params.append(json.dumps(actions, ensure_ascii=True, sort_keys=True))
        if snoozed_until is not None:
            updates.append("snoozed_until = ?")
            params.append(snoozed_until)
        if expires_at is not None:
            updates.append("expires_at = ?")
            params.append(expires_at)
        if not updates:
            return True

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now)
        params.append(item_id)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"""
                UPDATE agent_inbox_items
                SET {", ".join(updates)}
                WHERE id = ?
                """,
                params,
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def _validate_inbox_status(self, status: str):
        if status not in AGENT_INBOX_STATUSES:
            choices = ", ".join(sorted(AGENT_INBOX_STATUSES))
            raise ValueError(
                f"Invalid inbox status '{status}'. Expected one of: {choices}"
            )

    def _validate_inbox_surface(self, surface: str):
        if surface not in AGENT_INBOX_SURFACES:
            choices = ", ".join(sorted(AGENT_INBOX_SURFACES))
            raise ValueError(
                f"Invalid inbox surface '{surface}'. Expected one of: {choices}"
            )

    def _default_inbox_duplicate_key(
        self,
        opportunity_id: int | None,
        title: str,
    ) -> str:
        if opportunity_id is not None:
            return f"inbox:opportunity={opportunity_id}"
        normalized_title = " ".join(title.casefold().split())
        return f"inbox:title={normalized_title}"

    # --- Structured Memory ---

    def record_memory_item(
        self,
        kind: str,
        subject: str,
        content: str,
        confidence: int,
        evidence_event_ids: list[int],
        status: str = "active",
        expires_at: str | None = None,
        source: str = "reflection",
        supersede_existing: bool = False,
    ) -> int:
        """
        Create or refresh a typed memory record.

        Active items are unique by (kind, subject). Matching active rows merge
        evidence when their content matches. Callers may opt in to superseding
        an existing active row before recording a replacement.
        """
        self.expire_memory_items()
        validated = self._validate_memory_item_fields(
            kind=kind,
            subject=subject,
            content=content,
            confidence=confidence,
            evidence_event_ids=evidence_event_ids,
            status=status,
        )
        existing = None
        if validated["status"] == "active":
            existing = self.get_active_memory_item(
                validated["kind"],
                validated["subject"],
            )
        if existing is not None:
            existing_evidence = self._decode_json_list(existing["evidence_event_ids"])
            merged_evidence = sorted(
                {int(event_id) for event_id in existing_evidence + validated["evidence_event_ids"]}
            )
            if existing["content"] == validated["content"]:
                merged_confidence = max(int(existing["confidence"]), validated["confidence"])
                self.update_memory_item(
                    int(existing["id"]),
                    content=validated["content"],
                    confidence=merged_confidence,
                    evidence_event_ids=merged_evidence,
                    expires_at=expires_at,
                )
                return int(existing["id"])
            if not supersede_existing:
                raise ValueError(
                    f"Active memory item already exists for {validated['kind']} / "
                    f"{validated['subject']}"
                )
            self.update_memory_item_status(
                int(existing["id"]),
                "superseded",
                evidence_event_ids=validated["evidence_event_ids"],
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO memory_items
                    (kind, subject, content, confidence, evidence_event_ids,
                     status, source, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated["kind"],
                    validated["subject"],
                    validated["content"],
                    validated["confidence"],
                    json.dumps(validated["evidence_event_ids"], ensure_ascii=True),
                    validated["status"],
                    source,
                    expires_at,
                    now,
                    now,
                ),
            )
            conn.commit()
            item_id = cursor.lastrowid
        finally:
            conn.close()

        self.add_agent_event(
            event_type="memory_item_recorded",
            source=source,
            source_id=str(item_id),
            payload={
                "memory_item_id": item_id,
                "kind": validated["kind"],
                "subject": validated["subject"],
                "status": validated["status"],
                "confidence": validated["confidence"],
                "evidence_event_ids": validated["evidence_event_ids"],
            },
            schedule_reflection=False,
        )
        return item_id

    def get_memory_item(self, item_id: int) -> dict | None:
        """Return one memory item by id."""
        self.expire_memory_items()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM memory_items
                WHERE id = ?
                """,
                (int(item_id),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_active_memory_item(self, kind: str, subject: str) -> dict | None:
        """Return the active memory item for this kind/subject pair, if present."""
        self.expire_memory_items()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM memory_items
                WHERE kind = ?
                  AND subject = ?
                  AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (kind, subject),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_memory_items(
        self,
        status: str | None = "active",
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent memory items, newest first."""
        self.expire_memory_items()
        clauses = []
        params: list = []
        if status is not None:
            self._validate_memory_status(status)
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            self._validate_memory_kind(kind)
            clauses.append("kind = ?")
            params.append(kind)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)

        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM memory_items
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_memory_item(
        self,
        item_id: int,
        content: str | None = None,
        confidence: int | None = None,
        evidence_event_ids: list[int] | None = None,
        expires_at: str | None = None,
    ) -> bool:
        """Update mutable fields on one memory item."""
        existing = self.get_memory_item(item_id)
        if existing is None:
            return False

        updates = []
        params = []
        if content is not None:
            cleaned = content.strip()
            if not cleaned:
                raise ValueError("content cannot be empty")
            updates.append("content = ?")
            params.append(cleaned)
        if confidence is not None:
            updates.append("confidence = ?")
            params.append(self._validate_memory_confidence(confidence))
        if evidence_event_ids is not None:
            updates.append("evidence_event_ids = ?")
            params.append(
                json.dumps(
                    self._validate_evidence_event_ids(evidence_event_ids),
                    ensure_ascii=True,
                )
            )
        if expires_at is not None:
            updates.append("expires_at = ?")
            params.append(expires_at)
        if not updates:
            return True

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now)
        params.append(int(item_id))
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"""
                UPDATE memory_items
                SET {", ".join(updates)}
                WHERE id = ?
                """,
                params,
            )
            conn.commit()
        finally:
            conn.close()

        if cursor.rowcount:
            self.add_agent_event(
                event_type="memory_item_updated",
                source=existing.get("source") or "memory",
                source_id=str(item_id),
                payload={"memory_item_id": int(item_id)},
                schedule_reflection=False,
            )
        return cursor.rowcount > 0

    def update_memory_item_status(
        self,
        item_id: int,
        status: str,
        evidence_event_ids: list[int] | None = None,
    ) -> bool:
        """Move a memory item through its bounded lifecycle."""
        existing = self.get_memory_item(item_id)
        if existing is None:
            return False
        self._validate_memory_transition(existing["status"], status)

        merged_evidence = self._decode_json_list(existing["evidence_event_ids"])
        if evidence_event_ids:
            merged_evidence = sorted(
                {int(event_id) for event_id in merged_evidence + evidence_event_ids}
            )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE memory_items
                SET status = ?,
                    evidence_event_ids = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(merged_evidence, ensure_ascii=True),
                    now,
                    int(item_id),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        if cursor.rowcount:
            self.add_agent_event(
                event_type="memory_item_status_changed",
                source=existing.get("source") or "memory",
                source_id=str(item_id),
                payload={
                    "memory_item_id": int(item_id),
                    "previous_status": existing["status"],
                    "status": status,
                },
                schedule_reflection=False,
            )
        return cursor.rowcount > 0

    def expire_memory_items(self) -> int:
        """Mark active items expired once their expiry timestamp passes."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                UPDATE memory_items
                SET status = 'expired',
                    updated_at = ?
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (now, now),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def _validate_memory_item_fields(
        self,
        kind: str,
        subject: str,
        content: str,
        confidence: int,
        evidence_event_ids: list[int],
        status: str,
    ) -> dict[str, object]:
        self._validate_memory_kind(kind)
        self._validate_memory_status(status)
        cleaned_subject = subject.strip()
        cleaned_content = content.strip()
        if not cleaned_subject:
            raise ValueError("subject cannot be empty")
        if not cleaned_content:
            raise ValueError("content cannot be empty")
        normalized_confidence = self._validate_memory_confidence(confidence)
        normalized_evidence = self._validate_evidence_event_ids(evidence_event_ids)
        if not normalized_evidence:
            raise ValueError("memory items require at least one evidence_event_id")
        lowered = f"{cleaned_subject} {cleaned_content}".casefold()
        if normalized_confidence > 3 and any(term in lowered for term in SENSITIVE_MEMORY_TERMS):
            raise ValueError(
                "Sensitive inferred memories must stay low or medium confidence"
            )
        return {
            "kind": kind,
            "subject": cleaned_subject,
            "content": cleaned_content,
            "confidence": normalized_confidence,
            "evidence_event_ids": normalized_evidence,
            "status": status,
        }

    def _validate_memory_kind(self, kind: str):
        if kind not in MEMORY_ITEM_KINDS:
            choices = ", ".join(sorted(MEMORY_ITEM_KINDS))
            raise ValueError(
                f"Invalid memory kind '{kind}'. Expected one of: {choices}"
            )

    def _validate_memory_status(self, status: str):
        if status not in MEMORY_ITEM_STATUSES:
            choices = ", ".join(sorted(MEMORY_ITEM_STATUSES))
            raise ValueError(
                f"Invalid memory status '{status}'. Expected one of: {choices}"
            )

    def _validate_memory_confidence(self, value: int) -> int:
        confidence = int(value)
        if confidence < 1 or confidence > 5:
            raise ValueError("confidence must be between 1 and 5")
        return confidence

    def _validate_evidence_event_ids(self, evidence_event_ids: list[int]) -> list[int]:
        cleaned = sorted({int(event_id) for event_id in evidence_event_ids if int(event_id) > 0})
        return cleaned

    def _validate_memory_transition(self, current_status: str, new_status: str):
        self._validate_memory_status(new_status)
        allowed = {
            "active": {"active", "superseded", "rejected", "expired"},
            "superseded": {"superseded"},
            "rejected": {"rejected", "superseded"},
            "expired": {"expired", "superseded"},
        }
        if new_status not in allowed.get(current_status, set()):
            raise ValueError(
                f"Invalid memory status transition {current_status!r} -> {new_status!r}"
            )

    def _decode_json_list(self, raw: str | None) -> list[int]:
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, list):
            return []
        cleaned = []
        for value in decoded:
            try:
                numeric = int(value)
            except (TypeError, ValueError):
                continue
            if numeric > 0:
                cleaned.append(numeric)
        return cleaned

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

    def get_unsummarized_messages(
        self,
        scope: MessageScope | None = None,
    ) -> list[dict]:
        """
        Get all messages that haven't been covered by any summary.

        This is the primary input for the summarization check:
        if len(memory.get_unsummarized_messages()) exceeds the
        token threshold, it's time to summarize.
        """
        resolved_scope = self._normalize_scope(scope)
        last_id = self.get_last_summarized_id(resolved_scope)
        return self.get_messages_since(last_id, resolved_scope)

    def clear_history(self, scope: MessageScope | None = None):
        """
        Delete all messages, summaries, and triggers. Used by /clear.

        This is destructive — there's no undo. The database file
        itself is preserved (with empty tables). Schedule config and
        agent narrative are intentionally preserved — they're settings
        and state, not conversation data.
        """
        conn = self._connect()
        try:
            if scope is None:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM summaries")
                conn.execute("DELETE FROM triggers")
            else:
                resolved_scope = self._normalize_scope(scope)
                conn.execute(
                    """
                    DELETE FROM messages
                    WHERE scope_type = ?
                      AND (
                          (? IS NULL AND scope_id IS NULL)
                          OR scope_id = ?
                      )
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM summaries
                    WHERE scope_type = ?
                      AND (
                          (? IS NULL AND scope_id IS NULL)
                          OR scope_id = ?
                      )
                    """,
                    (
                        resolved_scope.scope_type,
                        resolved_scope.scope_id,
                        resolved_scope.scope_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
