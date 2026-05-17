# Purcival Design Document

## Purpose

This document captures the current architecture and direction of Purcival so a
new coding session can resume without rebuilding context from scratch.

Operational instructions live in `AGENTS.md`. Rolling project state lives in
`PROGRESS.md`.

---

## About Zach

Zach Blickensderfer is a 32-year-old teacher in Menlo Park, California. He
studied computer science at Yale, earned a Master's in Education from Penn, and
is transitioning into software engineering.

He wants direct, technically serious collaboration. Challenge weak reasoning,
surface design tradeoffs, and avoid sycophancy. Purcival is both a practical
assistant and a learning project.

---

## What Purcival Is

Purcival is a self-hosted personal AI assistant running on Zach's custom
Windows PC with an NVIDIA RTX 3060.

The active assistant persona is Jo. The codebase still supports multiple
personas, but Jo is the only persona currently in active use.

Purcival can route LLM calls through:

- Claude via Anthropic API.
- ChatGPT via OpenAI API.
- Ollama for local models.

It owns its own memory and context:

- Every message is stored in SQLite.
- Older conversations are summarized.
- Summaries are embedded with Ollama and retrieved by semantic similarity.
- Tool state and agent state are persisted locally.

Telegram support exists in code but is not currently operable in Zach's Windows
setup. The active interface is the local terminal, and the next major interface
is the Goals dashboard.

---

## Environment

- Machine: custom Windows 11 PC.
- GPU: NVIDIA RTX 3060, 12GB VRAM.
- Python: 3.11+ target.
- Local embeddings: `nomic-embed-text` via Ollama.
- Active persona: Jo.
- Current mobile messaging: inactive.
- Background service story on Windows: TBD.

---

## High-Level Architecture

```text
Terminal / future dashboard
  |
  v
main.py / dashboard app
  |
  v
context.py
  |-- personas/jo.md
  |-- data/user_context.md
  |-- data/jo/memory.db
  |-- cached tool context
  |
  v
brain.ask(task=...)
  |-- Claude
  |-- ChatGPT
  |-- Ollama

agent.py
  |
  | perceive -> reason -> validate -> act -> update state
  v
tools/
  |-- schedule_tool.py
  |-- google_calendar.py
  |-- gmail.py
  |-- telegram_tool.py (implemented, inactive)
```

The model is stateless. Purcival supplies the system prompt, message history,
retrieved summaries, scheduled plan, tool context, and user context on each
call.

---

## Key Modules

| File | Role |
| --- | --- |
| `main.py` | Terminal UI, provider switching, `/schedule`, debug mode |
| `brain.py` | LLM gateway for Claude, ChatGPT, and Ollama |
| `context.py` | Prompt assembly from persona, user context, memory, schedule, tools |
| `memory.py` | SQLite persistence for messages, summaries, triggers, agent state |
| `summarizer.py` | Compresses older messages into embedded summaries |
| `embeddings.py` | Ollama embedding calls |
| `agent.py` | Agent cycle: perceive, reason, validate, act, update narrative |
| `proactive.py` | Scheduler bootstrap and trigger polling |
| `tools/base.py` | Tool and ToolMethod interface |
| `tools/schedule_tool.py` | Agent wake-up schedule management |
| `tools/google_calendar.py` | Read-only calendar context |
| `tools/gmail.py` | Read-only Gmail context with filtering |
| `tools/telegram_tool.py` | Telegram message tool, currently inactive |
| `google_auth.py` | OAuth flow for Google Calendar and Gmail |
| `config.py` | `.env` configuration |

---

## Data Layout

```text
data/
  user_context.md
  jo/
    memory.db
    google_credentials.json
```

`data/jo/memory.db` stores:

- `messages`
- `summaries`
- `triggers`
- `schedule_config`
- `tool_state`
- `agent_actions`
- `agent_narrative`
- `reasoning_log`

The Goals dashboard will add shared user-level state in `data/user.db`.

---

## Memory Model

Purcival's memory has three main layers:

- Shared user context in `data/user_context.md`.
- Summaries of older conversation chunks in SQLite, with embeddings.
- Recent verbatim messages from SQLite.

Retrieval is intentionally simple:

- Generate an embedding for the current query.
- Compare against stored summary embeddings with cosine similarity.
- Include relevant summaries in the system prompt.
- Include recent verbatim messages directly in the message array.

This avoids a vector database dependency and is sufficient for a personal
assistant scale.

---

## Agent Loop

The agent loop is the completed self-scheduling architecture documented in
`Design/agent_loop_design.md`.

Each cycle:

1. Loads trigger context and narrative state.
2. Runs selected tools for perception.
3. Builds a reasoning prompt.
4. Calls `brain.ask(..., task="reasoning")`.
5. Parses JSON actions.
6. Validates tool, method, tier, budget, and tool-specific rules.
7. Executes allowed actions.
8. Writes narrative state and reasoning logs.
9. Ensures a future planning cycle exists.

Tool actions use JSON inside `<actions>` tags. Narrative state remains prose.

---

## Tool System

Every tool implements:

```python
get_context() -> str | None
get_methods() -> list[ToolMethod]
execute(method_name: str, **kwargs) -> str
```

Permission tiers:

- `observe`: read external data or update internal state.
- `message`: send a user-visible message.
- `draft`: prepare something for review.
- `execute`: act externally as Zach, requiring explicit approval.

The generic agent gate validates tool existence, enabled status, method
existence, tier, and daily budget. Each tool validates its own business rules.

---

## Provider Architecture

`brain.ask()` accepts:

```python
task="chat" | "summary" | "reasoning"
```

Each provider family has per-task model settings:

- Claude chat / summary / reasoning.
- ChatGPT chat / summary / reasoning.
- Ollama chat / summary / reasoning.

`DEFAULT_PROVIDER` selects the provider family. If a cloud provider is
unconfigured, the code falls back to Ollama where possible.

OpenAI integration is documented in `Design/openai_integration_design.md`.

---

## Current Active Project

The active project is the Goals dashboard, documented in
`Design/dashboard_goals_design.md`.

The dashboard will:

- Store shared goals, steps, and feedback in `data/user.db`.
- Add scoped goal/step chats using Jo's existing `messages` infrastructure.
- Let Jo propose concrete next steps during planning cycles.
- Let Zach accept, reject, complete, or abandon steps.
- Surface accepted steps for accountability.

The current implementation stage is Phase 5: agent loop suggestion generation.
Phase 1 data storage and scoped memory are implemented; Phase 2 added the
dashboard skeleton and visual identity; Phase 3 added real goal/step rendering
and accept/reject flows; Phase 4 added scoped Jo chats for goals and steps.

---

## Known Issues

- Telegram is not currently operable.
- `telegram_bot.py` has provider-model display drift in `/status`.
- Some older tests still encode legacy assumptions.
- `pytest` is documented but may need environment cleanup in the current venv.
- The trigger-deletion bug remains a known issue and should not be tackled as
  a side effect of dashboard work.

---

## Roadmap

Near term:

- Register goal and suggestion tools with the agent loop.
- Update planning prompts so Jo proposes 1-3 concrete suggestions tied to active goals.
- Surface generated suggestions on the dashboard.

Deferred:

- Reactivate or replace mobile messaging.
- Execute-tier approval flow.
- Web search tool.
- AI-proposed goals.
- Recurring steps.
- Memory architecture evolution beyond summary-based retrieval.
