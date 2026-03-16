"""
Memory — persistent conversation storage and retrieval.

Each persona gets its own SQLite database at data/<persona>/memory.db.
This module handles:
    - Storing every message (user and assistant) permanently
    - Storing conversation summaries with vector embeddings
    - Retrieving recent messages for the context window
    - Retrieving relevant summaries via cosine similarity search

The rest of the app interacts with memory through a PersonaMemory instance.
One instance per persona, created at startup.

Database schema:
    messages   — full verbatim record of every message, never deleted
    summaries  — compressed conversation chunks with embeddings

Design notes:
    - WAL mode is enabled for concurrent reads (multiple personas can
      read while one writes, though each persona has its own DB)
    - Embeddings are stored as binary blobs (numpy arrays serialized
      with tobytes/frombuffer)
    - Cosine similarity search is done in Python, not SQL. At the scale
      of a personal assistant (hundreds to low thousands of summaries),
      this is fast enough and avoids adding a vector DB dependency.
"""

import sqlite3
import numpy as np
from pathlib import Path
from datetime import datetime

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

                CREATE INDEX IF NOT EXISTS idx_messages_created
                    ON messages(created_at);

                CREATE INDEX IF NOT EXISTS idx_summaries_range
                    ON summaries(message_start, message_end);

                CREATE INDEX IF NOT EXISTS idx_triggers_fire_at
                    ON triggers(fire_at);
            """)
            conn.commit()
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
            trigger_type: "reminder", "calendar", or "check_in"
            fire_at: When to fire, as "YYYY-MM-DD HH:MM:SS" local time.
            context: Human-readable description of why this trigger exists
                (e.g. "pick up Tessa from work", "morning check-in").
            recurring: Recurrence pattern string, or None for one-shot.
                e.g. "hourly_6_to_23" for the standard check-in schedule.

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
        Mark a one-shot trigger as fired so it won't fire again.
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

    def delete_trigger(self, trigger_id: int):
        """Remove a trigger entirely."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
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
        Delete all messages and summaries. Used by /clear command.

        This is destructive — there's no undo. The database file
        itself is preserved (with empty tables).
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM summaries")
            conn.execute("DELETE FROM triggers")
            conn.commit()
        finally:
            conn.close()