# Purcival

Purcival is Zach's self-hosted personal AI assistant, built as both a useful
local tool and a systems-learning project. It runs on Zach's Windows PC with an
RTX 3060, uses Jo as the active persona, persists long-term memory in SQLite,
and can route LLM calls to Claude, ChatGPT, or a local Ollama model.

The active development project is the **Goals dashboard**: a local web app for
tracking Zach's goals, suggesting concrete next steps, and opening focused Jo
chat threads about a goal or step.

## Current Status

Built and stable:

- Jo persona with persistent memory in `data/jo/memory.db`.
- Terminal chat through `main.py`.
- Self-scheduling agent loop with trigger-based wake-ups.
- Tool interface with permission tiers: observe, message, draft, execute.
- Schedule, Google Calendar, Gmail, and Telegram tool implementations.
- Per-task LLM model routing for chat, summary, and reasoning calls.
- Conversation summarization and semantic retrieval through Ollama embeddings.

Important current constraints:

- Jo is the only persona currently in active use.
- Telegram exists in code but is not currently operable.
- Windows is the primary environment.
- Background service setup on Windows is still TBD.
- Design docs live in `Design/`.

## Architecture

```text
Windows PC
  |
  | Terminal / future dashboard
  v
main.py or dashboard app
  |
  | loads persona + context
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
  | perceive -> reason -> validate -> act -> update narrative
  v
tools/
  |-- schedule_tool.py
  |-- google_calendar.py
  |-- gmail.py
  |-- telegram_tool.py (inactive in current setup)
```

The model is stateless. Purcival owns persistence, prompt assembly, tool
state, summaries, and retrieval.

## Quick Start

PowerShell:

```powershell
git clone <repo-url> Purcival
cd Purcival

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
```

Install local models if using Ollama:

```powershell
ollama pull nomic-embed-text
```

The local chat model changes over time. Set the active Ollama chat, summary,
and reasoning models in `.env`.

## Running Jo

Terminal chat:

```powershell
python main.py --persona jo
python main.py --persona jo --provider chatgpt
python main.py --persona jo --provider claude
python main.py --persona jo --provider ollama
python main.py --persona jo --debug
```

Single message:

```powershell
python main.py --persona jo -m "hello"
```

Terminal commands:

- `/persona`
- `/claude`
- `/chatgpt`
- `/ollama`
- `/schedule`
- `/status`
- `/debug`
- `clear`
- `quit`

## Configuration

All local configuration lives in `.env`, which is never committed.

Core provider settings:

```env
DEFAULT_PROVIDER=ollama

ANTHROPIC_API_KEY=
OPENAI_API_KEY=

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_CHAT_MODEL=phi4
OLLAMA_SUMMARY_MODEL=phi4
OLLAMA_REASONING_MODEL=phi4
```

Per-task cloud model settings are available for Claude and ChatGPT:

```env
CLAUDE_CHAT_MODEL=
CLAUDE_SUMMARY_MODEL=
CLAUDE_REASONING_MODEL=

CHATGPT_CHAT_MODEL=
CHATGPT_SUMMARY_MODEL=
CHATGPT_REASONING_MODEL=
```

## Memory

Current data layout:

```text
data/
  user_context.md
  jo/
    memory.db
    google_credentials.json
```

Memory tiers:

- `data/user_context.md`: manually maintained shared user context.
- `messages`: verbatim conversation history.
- `summaries`: compressed older conversation chunks with embeddings.
- `tool_state`: durable state for tools.
- `agent_narrative`: prose continuity state for the agent.
- `agent_actions`: action audit trail.
- `reasoning_log`: debugging trace for agent cycles.

## Agent Loop

The agent loop is implemented in `agent.py` and scheduled through
`proactive.py`.

Each cycle:

1. Loads trigger purpose, narrative state, active plan, and pending proposals.
2. Runs relevant tools for perception.
3. Builds a reasoning prompt.
4. Calls `brain.ask(..., task="reasoning")`.
5. Parses JSON tool actions.
6. Validates actions through generic and tool-specific gates.
7. Executes allowed actions.
8. Updates narrative and reasoning logs.
9. Ensures a future planning cycle exists.

Actions use JSON inside `<actions>` tags. Narrative continuity remains prose.

## Tools

| Tool | Purpose | Current note |
| --- | --- | --- |
| `ScheduleTool` | Manage agent wake-ups | Active |
| `GoogleCalendarTool` | Read upcoming calendar events | Active when Google credentials exist |
| `GmailTool` | Read and filter inbox context | Active when Google credentials exist |
| `TelegramTool` | Send Telegram messages | Implemented but inactive in current setup |

Adding a tool means implementing the `Tool` interface in `tools/base.py` and
registering it in `tools/__init__.py`.

## Google API Setup

Calendar and Gmail use shared OAuth credentials.

```powershell
python -c "from google_auth import run_auth_flow; run_auth_flow('jo')"
```

Credentials are stored under `data/jo/google_credentials.json` and are ignored
by git.

## Goals Dashboard

Current phase: Phase 2 dashboard skeleton.

Canonical design doc:

```text
Design/dashboard_goals_design.md
```

Planned implementation phases:

- Phase 1: shared data layer and scoped memory.
- Phase 2: dashboard skeleton and visual identity.
- Phase 3: real goal/step rendering and accept/reject flows.
- Phase 4: scoped chat on goals and steps.
- Phase 5: agent-generated suggestions.
- Phase 6: accountability.
- Phase 7: feedback-loop polish.

Phase 1 data storage and scoped memory are implemented. No dashboard UI has
shipped yet; Phase 2 starts with the screenshot-driven dashboard skeleton.

## Project Structure

```text
Purcival/
  main.py
  agent.py
  brain.py
  config.py
  context.py
  embeddings.py
  google_auth.py
  memory.py
  proactive.py
  summarizer.py
  telegram_bot.py
  tools/
  personas/
  Design/
  tests/
  requirements.txt
  AGENTS.md
  PROGRESS.md
```

## Tests

The intended test runner is:

```powershell
pytest
```

Live OpenAI, Google Calendar, and Ollama summarization tests are skipped by
default during dashboard design work. To run them, set
`PURCIVAL_RUN_LIVE_TESTS=1` and make sure the relevant credentials or local
services are available.

## Roadmap

- Goals dashboard Phase 2 skeleton and local UI.
- Scoped Jo chats for goals and steps.
- Agent-generated suggestions tied to active goals.
- Accountability context for accepted steps.
- Feedback summaries to improve future suggestions.
- Future reactivation or replacement of Telegram/mobile messaging.
