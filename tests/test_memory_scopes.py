import sqlite3
from pathlib import Path

import numpy as np
import pytest

import context
import memory
import summarizer
from memory import MessageScope


@pytest.fixture()
def scoped_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path)
    return memory.PersonaMemory("jo")


def test_message_scope_validation():
    assert MessageScope.default().label == "default"
    assert MessageScope.goal(2).label == "goal:2"
    assert MessageScope.step(3).label == "step:3"

    with pytest.raises(ValueError):
        MessageScope("default", 1)
    with pytest.raises(ValueError):
        MessageScope("step", None)
    with pytest.raises(ValueError):
        MessageScope("project", 1)


def test_scoped_messages_do_not_leak_into_default(scoped_memory):
    default_id = scoped_memory.add_message("user", "Normal Jo chat")
    step_scope = MessageScope.step(7)
    step_id = scoped_memory.add_message("user", "Step-specific chat", step_scope)

    default_messages = scoped_memory.get_recent_messages(limit=10)
    step_messages = scoped_memory.get_recent_messages(limit=10, scope=step_scope)

    assert [row["id"] for row in default_messages] == [default_id]
    assert [row["id"] for row in step_messages] == [step_id]
    assert scoped_memory.get_message_count() == 1
    assert scoped_memory.get_message_count(step_scope) == 1


def test_get_messages_before_respects_scope(scoped_memory):
    step_scope = MessageScope.step(7)
    for index in range(5):
        scoped_memory.add_message("user", f"Default {index}")
        scoped_memory.add_message("user", f"Scoped {index}", step_scope)

    recent = scoped_memory.get_recent_messages(limit=3, scope=step_scope)
    older = scoped_memory.get_messages_before(
        before_id=recent[0]["id"],
        limit=2,
        scope=step_scope,
    )

    assert [row["content"] for row in recent] == [
        "Scoped 2",
        "Scoped 3",
        "Scoped 4",
    ]
    assert [row["content"] for row in older] == ["Scoped 0", "Scoped 1"]


def test_scoped_summarization_cursor_is_independent(scoped_memory):
    step_scope = MessageScope.step(4)

    scoped_memory.add_message("user", "Default one")
    scoped_memory.add_message("assistant", "Default two")
    scoped_memory.add_message("user", "Step one", step_scope)
    scoped_memory.add_message("assistant", "Step two", step_scope)

    scoped_memory.add_summary("Default summary", 1, 2, None)
    scoped_memory.add_summary("Step summary", 3, 4, None, scope=step_scope)

    assert scoped_memory.get_last_summarized_id() == 2
    assert scoped_memory.get_last_summarized_id(step_scope) == 4
    assert scoped_memory.get_unsummarized_messages() == []
    assert scoped_memory.get_unsummarized_messages(step_scope) == []


def test_search_summaries_can_include_default_background(scoped_memory):
    step_scope = MessageScope.step(11)
    default_embedding = np.zeros(768, dtype=np.float32)
    default_embedding[0] = 1.0
    step_embedding = np.zeros(768, dtype=np.float32)
    step_embedding[1] = 1.0
    query = np.zeros(768, dtype=np.float32)
    query[0] = 0.5
    query[1] = 1.0

    scoped_memory.add_summary(
        "Default background about Zach's career.",
        1,
        2,
        default_embedding,
    )
    scoped_memory.add_summary(
        "Step thread about researching LucidAI.",
        3,
        4,
        step_embedding,
        scope=step_scope,
    )

    scoped_only = scoped_memory.search_summaries(
        query,
        top_k=5,
        scope=step_scope,
    )
    with_background = scoped_memory.search_summaries(
        query,
        top_k=5,
        scope=step_scope,
        include_default=True,
    )

    assert [row["summary"] for row in scoped_only] == [
        "Step thread about researching LucidAI."
    ]
    assert {row["summary"] for row in with_background} == {
        "Default background about Zach's career.",
        "Step thread about researching LucidAI.",
    }


def test_context_assembly_uses_active_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "USER_CONTEXT_PATH", tmp_path / "user_context.md")

    mem = memory.PersonaMemory("jo")
    step_scope = MessageScope.step(9)
    mem.add_message("user", "Default conversation")
    mem.add_message("user", "Scoped conversation", scope=step_scope)

    system, messages = context.assemble_context(
        "You are Jo.",
        mem,
        scope=step_scope,
        entity_context="Goal: Stay active & healthy\nStep: Try one class",
    )

    assert "ACTIVE DASHBOARD CONTEXT" in system
    assert "Try one class" in system
    assert len(messages) == 1
    assert "Scoped conversation" in messages[0]["content"]


def test_summarizer_accepts_scope(scoped_memory, monkeypatch):
    step_scope = MessageScope.step(5)
    scoped_memory.add_message("user", "Step one", scope=step_scope)
    scoped_memory.add_message("assistant", "Step two", scope=step_scope)

    monkeypatch.setattr(summarizer, "_select_batch", lambda messages, max_tokens: messages)
    monkeypatch.setattr(summarizer, "_generate_summary", lambda messages: "Scoped summary")
    monkeypatch.setattr(summarizer, "get_embedding", lambda text: None)

    assert summarizer._summarize_one_batch(
        scoped_memory,
        scoped_memory.get_unsummarized_messages(step_scope),
        scope=step_scope,
    ) is True

    summaries = scoped_memory.get_all_summaries(scope=step_scope)
    assert len(summaries) == 1
    assert summaries[0]["scope_type"] == "step"
    assert scoped_memory.get_all_summaries() == []


def test_existing_memory_database_migrates_to_default_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DATA_DIR", tmp_path)
    db_dir = tmp_path / "jo"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "memory.db"

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE summaries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                summary         TEXT NOT NULL,
                message_start   INTEGER NOT NULL,
                message_end     INTEGER NOT NULL,
                embedding       BLOB,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO messages (role, content)
            VALUES ('user', 'Legacy default chat');
            INSERT INTO summaries (summary, message_start, message_end)
            VALUES ('Legacy summary', 1, 1);
        """)
        conn.commit()
    finally:
        conn.close()

    mem = memory.PersonaMemory("jo")

    messages = mem.get_recent_messages()
    summaries = mem.get_all_summaries()
    assert messages[0]["scope_type"] == "default"
    assert messages[0]["scope_id"] is None
    assert summaries[0]["scope_type"] == "default"
    assert summaries[0]["scope_id"] is None
