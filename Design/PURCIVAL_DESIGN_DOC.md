# Purcival — Project Design Document & Conversation Handoff

## Purpose of This Document

This document captures the full state of the Purcival project — what has been
built, why each decision was made, and what comes next. It is designed to
initialize a new Claude conversation so work can continue without loss of
context.

---

## About Zach (the user)

Zach Blickensderfer is a 31-year-old teacher living in Menlo Park, California.
He studied computer science at Yale and earned a Master's in Education from
Penn. He is actively transitioning from teaching into the tech industry as a
software engineer. He trained for over 100 hours with Landy Wang, a Microsoft
Technical Fellow, on systems programming.

Zach values learning, intellectual honesty, and being challenged rather than
validated. He prefers direct communication and substantive engagement. He is
building Purcival as both a practical tool and a learning project — he wants
to understand the technology deeply, not just get code that works.

Zach is married to Tessa. They have a cat named Purcival (spelled with a U),
who is the namesake of this project. His parents are Claudia and Jim
(divorced). His sister Sarah is 34, brothers Robert (24) and Seth (23).

---

## What Purcival Is

Purcival is a self-hosted personal AI assistant that runs on Zach's Linux
desktop (Ubuntu). It supports multiple "personas" — distinct AI personalities
for different purposes — each accessible as a separate Telegram bot. It can
route conversations to either Claude (via the Anthropic API) or a local model
running on his GPU via Ollama.

Each persona has its own persistent memory: every message is stored in a
per-persona SQLite database, older conversations are automatically summarized
using Claude, and relevant summaries are retrieved via semantic search using
vector embeddings.

The primary persona (Purcival) is now a **self-scheduling autonomous agent**
that plans its own day, manages its own wake-up schedule, and sends proactive
messages via Telegram. The agent reasons about what to do using Claude,
schedules targeted wake-ups with specific purposes, and manages its plan
across cycles. Other personas remain conversational (user-initiated only).

---

## Hardware & Environment

- **Machine:** Custom-built Linux desktop running Ubuntu
- **GPU:** NVIDIA RTX 3060 with 12GB VRAM
- **Local conversation model:** Mistral Small 3.2 via Ollama
- **Local embedding model:** nomic-embed-text via Ollama (~270MB, runs on CPU)
- **Summarization model:** Claude via Anthropic API (local models did not meet quality bar)
- **Agent reasoning model:** Claude via Anthropic API (reliable structured output)
- **Python:** 3.10+
- **Process management:** systemd
- **Mobile interface:** Telegram (one bot per persona)

---

## Current Architecture

```
Your phone (Telegram)                         Linux box (Ubuntu)
    │                                              │
    ├─ message @PurcivalBot ──→ Telegram cloud ──→ ├─ systemd: purcival@purcival
    ├─ message @AdaBot ───────→ Telegram cloud ──→ ├─ systemd: purcival@ada
    │                                              │
    │         Each process:                        │
    │         ┌──────────────────────────┐          │
    │         │ telegram_bot.py          │          │
    │         │  ├─ Long poll            │          │
    │         │  ├─ Auth check           │          │
    │         │  ├─ Persist message ─────┼──→ data/<persona>/memory.db
    │         │  ├─ Assemble context:    │          │
    │         │  │   ├─ Persona prompt   │←── personas/<persona>.md
    │         │  │   ├─ User context     │←── data/user_context.md
    │         │  │   ├─ Session info     │    (time, device type)
    │         │  │   ├─ Scheduled plan   │←── memory.db (agent triggers)
    │         │  │   ├─ Summaries        │←── memory.db (semantic search)
    │         │  │   └─ Recent messages  │←── memory.db
    │         │  ├─ brain.ask() ─────────┼──→ Ollama (local) or Claude (cloud)
    │         │  ├─ Strip <schedule_updates> from response
    │         │  ├─ Persist response ────┼──→ memory.db
    │         │  ├─ Apply schedule updates if any
    │         │  ├─ Summarize if needed ─┼──→ Claude API (always)
    │         │  │   └─ Embed summary ───┼──→ nomic-embed-text
    │         │  └─ Agent scheduler      │          │
    │         │      ├─ Check triggers/60s          │
    │         │      └─ Run agent cycle:            │
    │         │          ├─ Load trigger purpose     │
    │         │          ├─ Perceive (tools, no LLM) │
    │         │          ├─ Reason (Claude API) ─────┼──→ Claude API
    │         │          ├─ Validate + act            │
    │         │          ├─ Apply schedule changes    │
    │         │          └─ Update narrative state    │
    │         └──────────────────────────┘          │
    │                                              │
    │  Also available: terminal UI (main.py)       │
```

---

## File Structure

```
purcival/
├── main.py              Terminal UI — persona picker, chat loop, /schedule
├── run_telegram.py      Telegram entry point — one persona per process
├── telegram_bot.py      PersonaBot class — messaging, agent scheduler
├── brain.py             LLM abstraction — routes to Claude or Ollama
├── context.py           Context assembly — builds full prompt from all sources
├── memory.py            Persistent storage — messages, summaries, triggers, agent state
├── embeddings.py        Vector embeddings — generates embeddings via Ollama
├── summarizer.py        Summarization engine — compresses old conversations via Claude
├── proactive.py         Agent bootstrap and scheduler — ensure_agent_has_plan, start_scheduler
├── agent.py             Agent cycle — perceive, reason, act, plan (Stage 5)
├── tokens.py            Token counting — abstract interface for budget enforcement
├── config.py            Loads all settings from .env
├── personas.py          Discovers and loads persona files from personas/ directory
├── tools/
│   ├── __init__.py      Tool registry — create_tools() factory
│   ├── base.py          Tool and ToolMethod base classes
│   ├── schedule_tool.py ScheduleTool — agent manages its own wake-ups
│   └── telegram_tool.py TelegramTool — wraps send_fn for uniform interface
├── personas/
│   ├── purcival.md      English butler — detail-oriented life manager + agent
│   ├── ada.md           Technical sparring partner — CS, math, science
│   ├── jo.md            Efficient executive assistant
│   └── default.md       General-purpose assistant
├── data/
│   ├── user_context.md  Shared context about the user (read by all personas)
│   ├── purcival/
│   │   └── memory.db    Purcival's messages, summaries, triggers, agent state
│   ├── ada/
│   │   └── memory.db
│   └── jo/
│       └── memory.db
├── debug/               Prompt dumps from debug mode (gitignored)
├── tests/
│   ├── conftest.py      Path setup for test imports
│   └── test_stage5.py   60 tests for agent cycle, tools, parsing, validation
├── purcival@.service    Systemd template unit (uses %i for persona name)
├── requirements.txt     Python dependencies
├── STAGE5_AGENT_DESIGN.md  Design document for the self-scheduling agent
├── .env.example         Configuration template
├── .env                 Actual secrets (gitignored)
└── .gitignore           Excludes .env, venv/, __pycache__/, data/, debug/
```

---

## Personas

### Purcival — the Butler (agent-enabled)

Modeled after a capable English butler: detail-oriented, warm, witty, and to
the point. His job is to keep Zach's life running smoothly. He remembers
details — deadlines, commitments, people's names — and gives gentle reminders
when things are slipping. He is honest and direct. He cares about
organization, follow-through, and making sure nothing important falls through
the cracks. He is the primary persona with agent capabilities — he plans his
own day, schedules his own wake-ups, and sends proactive messages via Telegram.

### Ada — the Engineer

A technical sparring partner: sharp, curious, and a little irreverent. Deep
knowledge across CS, math, systems design, and science. She explains things
clearly without dumbing them down, pushes back on weak ideas, and gets
genuinely excited about interesting problems. She does not care about
schedules or to-do lists — that's Purcival's job.

### Jo — the Executive Assistant

Efficient and action-oriented. Good for task management and quick delegation.

---

## Key Design Decisions and Rationale

### 1. One persona per process, one Telegram bot per persona

Each persona appears as a separate contact on the phone. Simplifies code
and systemd management. Start/stop/restart individual personas independently.

### 2. Per-persona isolated databases

Each persona gets its own SQLite database. No cross-persona data sharing.
Mirrors how human relationships work. The one shared piece is
`user_context.md`, which is manually maintained.

### 3. Dual-model architecture (Claude + Ollama)

Both behind a common `brain.ask()` interface, switchable mid-conversation.
Local inference is free and private; Claude is higher quality but costs money.

### 4. Semantic search with embeddings for memory retrieval

Vector embeddings (nomic-embed-text) and cosine similarity to find relevant
summaries. Matches on meaning, not keywords. Runs on CPU in milliseconds.
Avoids extra LLM call per message.

### 5. Token-based summarization trigger

Trigger at 6,000 tokens unsummarized (not message count). Batch size of
3,000 tokens produces granular summaries (~one per conversation session).
Up to 5 batches processed per pass to handle backlogs.

### 6. Summarization uses Claude

Local models did not meet the accuracy bar — they hallucinated details and
sometimes fell into conversational mode instead of summarizing. Claude
produces accurate, concise natural paragraphs. Summarization is infrequent
enough that API cost stays low, and summary quality compounds over time.

### 7. Cost-optimized token budgets

Verbatim message window reduced from 32K to 8K tokens (~25-35 exchanges).
This is the biggest cost driver since it's sent on every API call. Typical
total prompt: ~20K tokens. Upper bound: ~32K. The savings come from not
sending the full conversation history on every call.

### 8. Device-aware response length

System prompt includes device type (terminal or Telegram). Telegram gets
instructions for concise, scannable responses. Terminal allows longer detail.

### 9. Timestamps in local time

All timestamps generated by Python's `datetime.now()`, not SQLite's
`CURRENT_TIMESTAMP` (which stores UTC). User messages include timestamps
in the LLM context. Summaries include temporal context.

### 10. Self-scheduling agent (Stage 5)

The agent plans its own day using a ScheduleTool that manipulates the
triggers table. Instead of fixed-interval check-ins, the agent schedules
purposeful wake-ups with specific contexts. Planning cycles (empty tools
list) load all tools and let the agent discover new information. Targeted
cycles (specific tools list) focus on a specific purpose. Every cycle
reasons — no skip gate. The LLM's output includes actions, schedule
changes, and an updated narrative state. All actions go through a code-level
validation gate. The agent operates within guardrails: wake/sleep times
and a daily action budget, both code-enforced.

### 11. Agent uses Claude for reasoning

The proactive provider was switched from Ollama to Claude after discovering
that three concurrent Ollama requests (one per persona) cause GPU contention
and timeouts on the RTX 3060. Claude eliminates this bottleneck and provides
more reliable structured output for the agent's reasoning format.

### 12. Safe clear command

`/clear` is disabled on Telegram to prevent accidental data loss. On the
terminal, `clear` shows how much data will be destroyed and requires typing
"yes" to confirm. Schedule config and narrative state are preserved.

### 13. Debug mode for prompt inspection

`--debug` flag or `/debug` toggle dumps the full assembled prompt to a
timestamped file in `debug/`. Shows system prompt sections, retrieved
summaries with similarity scores, all messages with token counts.

### 14. Abstract interfaces for swappable implementations

Token counting (`tokens.py`), embeddings (`embeddings.py`), and LLM routing
(`brain.py`) all expose simple function interfaces. Implementations can be
swapped without changing other code.

### 15. Persona prompts as plain markdown files

Each persona is a `.md` file. Adding a persona requires no code changes.
Persona prompts are read from disk on every message — changes take effect
immediately without restarting.

### 16. Configuration via .env file

All secrets and settings in `.env`, loaded by `python-dotenv`. `config.py`
is the single module that reads environment variables.

### 17. Security: Telegram user ID allowlist

Only messages from a specific Telegram user ID are processed. All others
silently ignored.

### 18. Long polling, not webhooks

No inbound ports, no public IP, no TLS certificates needed. Works behind
home router/NAT.

### 19. Telegram chat ID persistence

The chat ID required for proactive messaging is stored in the `tool_state`
table, surviving service restarts. The user no longer needs to send a message
after every restart to re-enable proactive messaging.

---

## Memory System — Technical Details

### Database Schema (per persona)

```sql
-- Original tables (Stages 1-4)
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP
);

CREATE TABLE summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary         TEXT NOT NULL,
    message_start   INTEGER NOT NULL,
    message_end     INTEGER NOT NULL,
    embedding       BLOB,
    created_at      TIMESTAMP
);

CREATE TABLE triggers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    fire_at     TIMESTAMP NOT NULL,
    context     TEXT,
    recurring   TEXT,
    fired       BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP
);

CREATE TABLE schedule_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    start_time          TEXT NOT NULL,
    end_time            TEXT NOT NULL,
    interval_minutes    INTEGER NOT NULL,
    max_actions_per_day INTEGER NOT NULL DEFAULT 25,
    updated_at          TIMESTAMP
);

-- Stage 5 tables
CREATE TABLE tool_state (
    tool_name   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tool_name, key)
);

CREATE TABLE agent_actions (
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

CREATE TABLE agent_narrative (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    narrative   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reasoning_log (
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
```

### Context Assembly Token Budgets

| Section | Budget | Typical | Source |
|---------|--------|---------|--------|
| Persona prompt | 2,000 | ~500 | `personas/*.md` |
| User context | 2,000 | ~640 | `data/user_context.md` |
| Session info | 500 | ~100 | Generated (time, device) |
| Scheduled plan | 2,000 | ~300 | Agent triggers |
| Relevant summaries | 8,000 | ~2,000 | Semantic search |
| Additional context | 8,000 | 0 | Future integrations |
| Verbatim messages | 8,000 | ~6,000 | Recent messages with timestamps |
| **Total** | **~30,500** | **~9,540** | |

### Retention Policies

| Table | Retention | Enforced by |
|-------|-----------|-------------|
| `reasoning_log` | 7 days | `cleanup_old_data()` at cycle start |
| `agent_actions` | 30 days | `cleanup_old_data()` at cycle start |
| `agent_narrative` | 30 days | `cleanup_old_data()` at cycle start |
| `tool_state` | Per-tool | Each tool prunes its own stale data |
| `messages` | Forever | Never deleted (summarized instead) |
| `summaries` | Forever | Never deleted |

---

## Current State of Each Module

### brain.py
Two providers: `_ask_claude()` and `_ask_ollama()`. Router via `_PROVIDERS`
dict. `ask()` requires explicit system prompt. Claude client lazy-loaded.

### config.py
Loads `.env` via `python-dotenv`. Single source of truth for all settings.
`get_telegram_token()` constructs env var names dynamically.

### context.py
`assemble_context(persona_prompt, memory, device)` returns `(system_prompt,
messages)`. Builds from sections with per-section token budgets. Includes
the agent's scheduled plan when a schedule is configured. Retrieves
summaries via semantic search. Timestamps on user messages. Device type
controls response length guidance. Empty sections omitted cleanly.

### memory.py
`PersonaMemory` class. Original tables (messages, summaries, triggers,
schedule_config) plus Stage 5 tables (tool_state, agent_actions,
agent_narrative, reasoning_log). WAL mode. Local timestamps via
`datetime.now()`. Embeddings as numpy float32 BLOBs. Cosine similarity
search in Python. Schema migrations run on startup for backward compat.

### embeddings.py
`get_embedding(text)` returns 768-dim numpy array via Ollama's
nomic-embed-text. Runs on CPU.

### summarizer.py
`check_and_summarize(memory)` returns count of summaries created. Loops
through up to 5 batches per pass. Uses Claude for quality. Embeds each
summary for retrieval. Graceful degradation if embedding fails.

### proactive.py
Bootstrap (`ensure_agent_has_plan`) and scheduler (`start_scheduler`).
The scheduler checks triggers every 60 seconds and runs the agent cycle
for each due trigger. The old three-layer proactive system (scheduler,
decision gate, message composer) has been fully replaced by the agent
cycle in `agent.py`.

### agent.py
The self-scheduling agent cycle. Perceive → Reason → Act → Plan.
Assembles a structured prompt with the trigger's purpose, tool contexts,
the agent's plan, narrative state, and available actions. Parses the
LLM's response into reasoning, actions, schedule changes, and narrative
state. Validates all actions through a code-level gate (7 checks for
actions, 5 for schedule changes). Handles both keyword and positional
argument styles from the LLM. Safety net seeds tomorrow's planning cycle
if the agent forgets to schedule anything.

### tools/base.py
`Tool` and `ToolMethod` base classes defining the interface contract.

### tools/schedule_tool.py
`ScheduleTool` — the agent's self-scheduling interface. Methods:
get_plan, add_wakeup, modify_wakeup, cancel_wakeup. All observe-tier.

### tools/telegram_tool.py
`TelegramTool` — wraps the Telegram send function. Pending message queue
bridges async send_fn with synchronous execute().

### tools/__init__.py
`create_tools()` factory. Currently creates ScheduleTool and TelegramTool.
Future tools (GoogleCalendar, Gmail) will be added here.

### tokens.py
`get_token_count(text)` — currently `len(text) // 4`. Abstract interface
for future real tokenizer.

### telegram_bot.py
`PersonaBot` class. Chat ID persisted to database for restart resilience.
Response handler strips `<schedule_updates>` tags and applies them.
Starts agent scheduler via `post_init` callback. `/status` shows agent
state, action budget, and trigger count.

### main.py
Terminal UI with `rich`. `/schedule` configures wake time, sleep time,
and daily action limit. Banner shows "Stage 5" and agent state. Debug
mode via `--debug` flag or `/debug` toggle.

---

## Known Issues & Limitations

1. **Summary quality depends on prompt tuning.** Claude produces good
   summaries but occasionally includes mild inferences.

2. **Token counting is approximate.** The `len(text) // 4` heuristic is
   good enough for budget enforcement with generous margins.

3. **Disappearing triggers.** Some agent-scheduled triggers have been
   found deleted between cycles without corresponding cancel commands
   in the reasoning log. Under investigation.

4. **Test files partially consolidated.** Stage 5 tests are in
   `tests/test_stage5.py`. Older test files may still exist in the
   project root.

---

## Future Roadmap

### Near term (Stage 5 completion)
- Google Calendar integration (read-only) via GoogleCalendarTool
- Gmail integration (read-only) via GmailTool
- Google API OAuth2 authentication flow
- Investigate and fix disappearing triggers issue

### Medium term
- Execute-tier approval flow (propose → approve → execute via Telegram)
- Gmail send and Calendar event creation (write scopes)
- Evening background work (research, drafting outside messaging hours)
- Large local model for agent reasoning (async, latency doesn't matter)

### Long term
- Semantic search over individual old messages (not just summaries)
- Dedicated low-power hardware for always-on operation
- Signal as alternative to Telegram
- Real tokenizer for precise token counting
- Additional tools (Slack, weather, web search)

---

## Dependencies

```
# requirements.txt
anthropic>=0.84.0
python-dotenv>=1.0.0
requests>=2.31.0
rich>=13.0.0
python-telegram-bot>=22.0
numpy>=1.24.0
apscheduler>=3.10.0
```

---

## Key Technical Concepts Zach Has Learned

Through building this project, Zach has developed understanding of:

- Python virtual environments, .env files, secrets management
- LLM API mechanics — statelessness, context windows, token budgets, system prompts
- Local model inference — Ollama, VRAM constraints, quality/size tradeoffs, GPU contention
- Vector embeddings — text to numerical representations, cosine similarity, semantic search
- RAG pattern — embed, store vectors, retrieve relevant context
- SQLite — schema design, WAL mode, concurrent access, binary BLOBs, schema migrations
- Telegram Bot API — BotFather, long polling, user ID authorization, chat ID persistence
- systemd — service units, template units, journalctl, restart policies
- Async scheduling — APScheduler, event loops, post_init callbacks
- Cost optimization — token budget analysis, context window management
- Software architecture — provider abstraction, abstract interfaces, tiered decision systems, graceful degradation, debug instrumentation
- Agent architecture — tool interfaces, perception/reasoning/action loops, self-scheduling, structured vs. narrative state, action validation gates, code-level guardrails
- LLM output parsing — handling both keyword and positional arguments, XML tag extraction, escape sequence processing, defensive parsing strategies