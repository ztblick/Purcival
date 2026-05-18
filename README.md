# Purcival

Purcival is Zach's self-hosted personal AI assistant, built as both a useful
local tool and a systems-learning project. It runs on Zach's Windows PC with an
RTX 3060, uses Jo as the active persona, persists long-term memory in SQLite,
and can route LLM calls to Claude, ChatGPT, or a local Ollama model.

The active development project is the **core agent reliability redesign**. The
dashboard now acts as Purcival's secure local control plane for goals, inbox
cards, and focused Jo chats while the agent loop migrates to events, jobs,
opportunities, receipts, and safer delivery paths.

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
- Dashboard mobile access is private-only: keep Uvicorn on loopback and put
  Tailscale Serve plus dashboard auth in front of it.
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

## Dashboard

Active redesign docs:

```text
Design/core_agent_reliability_redesign.md
Design/dashboard_goals_design.md
```

Implemented dashboard slices:

- Shared goals/steps storage in `data/user.db`
- Scoped goal/step chats backed by Jo's normal memory
- Dashboard inbox cards for delivered suggestions and accountability checks
- Event-backed accept/reject/done/abandon receipts
- Signed-session dashboard auth with CSRF protection
- Loopback-first runtime config for local, tailscale, or LAN fallback modes

Generate a dashboard password hash once:

```powershell
.\venv\Scripts\python.exe scripts\hash_dashboard_password.py
```

Add the resulting hash and a random secret to `.env`:

```env
PURCIVAL_DASHBOARD_PASSWORD_HASH=pbkdf2_sha256$...
PURCIVAL_DASHBOARD_SECRET_KEY=replace-with-a-long-random-secret
PURCIVAL_DASHBOARD_EXPOSURE=local
PURCIVAL_DASHBOARD_HOST=127.0.0.1
PURCIVAL_DASHBOARD_PORT=8000
```

Run the authenticated dashboard:

```powershell
.\venv\Scripts\Activate.ps1
python scripts\seed_dev_data.py --reset
python scripts\run_dashboard.py
```

Then open:

```text
http://127.0.0.1:8000
```

Log in with the password you hashed into `.env`.

Run the non-Telegram background agent loop:

```powershell
python scripts\run_agent_loop.py --persona jo
```

For Windows Task Scheduler, point tasks at the wrapper scripts:

```text
scripts/start_dashboard.ps1
scripts/start_agent_loop.ps1
```

Suggested task names:

- `PurcivalDashboard`
- `PurcivalAgentLoop`

Each task should run as Zach's normal user account at logon with "run whether
user is logged on or not" only if Zach wants background access while logged
out. The PowerShell wrappers append stdout/stderr to `logs/`.

For private phone access, keep the dashboard bound to `127.0.0.1` and use
Tailscale Serve to expose that socket inside Zach's tailnet. Do not use
Funnel, router port forwarding, or a public tunnel for this service. The exact
Tailscale CLI commands still need local verification on Zach's Windows machine
before they should be treated as canonical setup instructions.

Capture dashboard screenshots:

```powershell
python scripts\capture_dashboard_screenshot.py
```

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
  dashboard/
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

Use the venv interpreter from Windows:

```powershell
.\venv\Scripts\python.exe -m pytest
```

Live OpenAI, Google Calendar, and Ollama summarization tests are skipped by
default during dashboard design work. To run them, set
`PURCIVAL_RUN_LIVE_TESTS=1` and make sure the relevant credentials or local
services are available.

## Roadmap

- Finish Phase E mobile verification on Zach's actual tailnet and phone.
- Continue the event/job/opportunity redesign beyond the dashboard auth slice.
- Add the untrusted-content boundary before web and local file tools.
- Keep Telegram dormant unless Zach explicitly reopens it.
