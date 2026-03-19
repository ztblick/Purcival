# Purcival

A personal AI assistant built from scratch, with help from Claude, named after our cat.

Purcival runs on your own hardware, talks to you via Telegram on your phone,
and lets you choose between cloud AI (Claude) and a local model running on
your GPU. Each persona is a separate Telegram bot with its own personality,
its own persistent memory, and its own conversation history. Conversations
survive restarts, reboots, and crashes. Older conversations are automatically
summarized and retrieved by semantic similarity when they become relevant again.

The primary persona (Jo) is a **self-scheduling autonomous agent** that
plans its own day, manages its own wake-up schedule, reads your Google
Calendar, and sends proactive messages via Telegram. The agent reasons about
what to do using Claude, schedules targeted wake-ups with specific purposes,
and adjusts its plan when your situation changes.

## How it works

```
Your phone (Telegram)                         Your Linux box
    │                                              │
    ├─ message Jo bot  ──→  Telegram servers  ──→  ├─ run_telegram.py --persona jo
    │                                              │
    │            Each process:                     │
    │            ┌───────────────────────────┐     │
    │            │ Long poll Telegram        │     │
    │            │ Persist user message      │──→ data/<persona>/memory.db
    │            │ Assemble context:         │     │
    │            │   ├─ Persona prompt       │←── personas/<persona>.md
    │            │   ├─ User context         │←── data/user_context.md
    │            │   ├─ Session info         │    (current time, device type)
    │            │   ├─ Scheduled plan       │←── memory.db (agent wake-ups)
    │            │   ├─ Relevant summaries   │←── memory.db (semantic search)
    │            │   └─ Recent messages      │←── memory.db (verbatim history)
    │            │ Call brain.ask()        ──┼──→ Ollama (local GPU)
    │            │       or                ──┼──→ Claude API (cloud)
    │            │ Strip <schedule_updates>  │     │
    │            │ Persist response          │──→ memory.db
    │            │ Apply schedule updates    │     │
    │            │ Check if summarization    │     │
    │            │   needed → if so:         │     │
    │            │   Summarize old messages  │──→ Claude API (always)
    │            │   Embed summary           │──→ nomic-embed-text
    │            │   Store summary + vector  │──→ memory.db
    │            │                           │     │
    │            │ Agent scheduler:          │     │
    │            │   Check triggers every 60s│     │
    │            │   If due → run agent cycle│     │
    │            │     ├─ Perceive (tools)   │     │
    │            │     │  └─ Google Calendar ─┼──→ Google Calendar API
    │            │     ├─ Reason (Claude) ───┼──→ Claude API
    │            │     ├─ Validate + act     │     │
    │            │     ├─ Update schedule    │     │
    │            │     └─ Update narrative   │──→ memory.db
    │            └──────────────────────────┘      
```

Each persona runs as its own process with its own Telegram bot and its own
SQLite database. Your Linux box long-polls Telegram's servers — no open ports
or public IP needed. The model doesn't remember anything between API calls;
Purcival manages all conversation context, persistence, and retrieval.

## Quick start

```bash
git clone <your-repo-url> purcival
cd purcival

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Pull the local models
ollama pull mistral-small3.2    # main conversation model
ollama pull nomic-embed-text    # embedding model for memory retrieval

cp .env.example .env
# Edit .env with your settings
```

## Two interfaces

### Terminal (for local development and testing)

```bash
python main.py                          # pick persona interactively
python main.py --persona jo             # jump straight in
python main.py --provider claude        # use Claude instead of Ollama
python main.py --debug                  # dump full prompts to debug/
python main.py -m "hello" --persona ada # single message
```

Terminal commands: `/persona`, `/claude`, `/ollama`, `/schedule`, `/status`,
`/debug`, `clear`, `quit`

The terminal interface shows a spinner when summarization is running
("Committing conversation to memory...") and allows longer, more detailed
responses from the LLM. The `clear` command requires typed confirmation
before deleting data. Schedule updates in LLM responses are stripped and
applied automatically — status messages show what was applied or rejected.

### Telegram (for daily use from your phone)

```bash
# Test manually first
python run_telegram.py --persona jo

# Run as a background service (starts on boot, auto-restarts)
sudo systemctl start purcival@jo
sudo systemctl enable purcival@jo

# Run multiple personas simultaneously
sudo systemctl start purcival@ada
sudo systemctl enable purcival@ada
```

Telegram commands: `/start`, `/provider`, `/status`

The Telegram interface instructs the LLM to keep responses concise and
scannable for mobile reading. The `/clear` command is disabled on Telegram
to prevent accidental data loss — use the terminal interface to clear history.

### Setting up Telegram

1. Message @BotFather on Telegram, create a bot for each persona
2. Message @userinfobot to get your user ID
3. Add tokens and user ID to `.env`:
   ```
   TELEGRAM_ALLOWED_USER_ID=123456789
   TELEGRAM_TOKEN_JO=7123456:AAF...
   TELEGRAM_TOKEN_ADA=7234567:BBG...
   ```
4. Test: `python run_telegram.py --persona jo`

### Setting up systemd

1. Edit `purcival@.service` — replace `YOUR_USERNAME` with your Linux username
2. `sudo cp purcival@.service /etc/systemd/system/`
3. `sudo systemctl daemon-reload`
4. `sudo systemctl start purcival@jo`
5. `sudo systemctl enable purcival@jo`

Useful commands:
```bash
journalctl -u purcival@jo -f     # live logs
sudo systemctl restart purcival@jo  # restart after code changes
sudo systemctl status 'purcival@*'     # status of all personas
```

Note: changes to Python files require a service restart to take effect.
Changes to `user_context.md` and persona `.md` files are read from disk
on every message and take effect immediately.

## Personas

Each persona is a markdown file in `personas/` that defines a system prompt.
The filename becomes the persona's name. Each persona gets its own Telegram
bot, its own process, and its own memory database.

```
personas/
├── jo.md          — Detail-oriented life manager (agent-enabled)
└── ada.md         — Spunky technical sparring partner
```

**Jo** is a detail-oriented personal assistant. They are witty, warm, and to
the point. They remember the details of your life — deadlines, commitments,
people's names — and keeps you on track with your goals. They give gentle
reminders when things are slipping and is honest when you need to hear it.
They are the primary persona with autonomous agent capabilities — Jo plans the
day, reads your Google Calendar, schedules wake-ups, and sends proactive
messages via Telegram.

**Ada** is a technical expert and thinking partner. Sharp, curious, and a
little irreverent. She is who you talk to about coding, systems design,
math, science, and engineering. She explains things clearly without dumbing
them down, pushes back on weak ideas, and gets excited when a conversation
goes somewhere interesting. She does not care about schedules or to-do lists.

To add a new persona: create a `.md` file in `personas/`, create a Telegram
bot with @BotFather, add the token to `.env`, and start the service. The
persona gets a fresh, independent memory database automatically.

## Self-scheduling agent

Purcival is an autonomous agent that plans its own day. Instead of firing on
a fixed interval, the agent schedules purposeful wake-ups with specific
contexts:

- "Wake me at 9:52 to encourage Zach before his 10:00 meeting"
- "Wake me at 22:30 to remind Zach to start winding down"
- "Wake me tomorrow at 6:00 for morning planning"

### How it works

1. **Bootstrap:** At the configured wake time, the system seeds a planning
   cycle. The agent wakes up, reads your calendar, and plans its day.
2. **Planning cycles:** The agent schedules periodic check-ins for itself
   to scan for new information. Empty tools list = load all tools.
3. **Targeted wake-ups:** The agent schedules specific wake-ups with a
   purpose and the tools it needs. Each cycle reasons about its purpose.
4. **User messages update plans:** When you tell the agent something
   time-sensitive ("remind me to give Tessa Tylenol at 9pm"), it silently
   schedules a reminder. No separate planning cycle needed.

### Configuring the agent

Use `/schedule` in the terminal to set:
- **Wake time:** When the agent's first planning cycle fires (e.g., 06:00)
- **Sleep time:** No agent-initiated wake-ups after this (e.g., 23:00)
- **Daily action limit:** Max messages/drafts/executions per day (default: 25)

```bash
python main.py --persona jo
# Then type /schedule and follow the prompts
```

The running Telegram service picks up schedule changes without restarting.
Changing operating hours removes old planning cycles and seeds new ones.
Targeted wake-ups (reminders, meeting prep) are always preserved.

### Guardrails

All enforced by code, not just the LLM prompt:
- Wake-ups outside operating hours are rejected
- Daily action budget caps how much the agent does
- Execute-tier actions (sending email, creating events) require explicit approval
- Every action goes through a 7-check validation gate before execution
- `/schedule` never deletes targeted wake-ups

### Tools

The agent interacts with the world through tools:

| Tool | What it does | Status |
|------|-------------|--------|
| **ScheduleTool** | Agent manages its own wake-up schedule | Built |
| **TelegramTool** | Send messages to the user | Built |
| **GoogleCalendarTool** | Read events from all visible calendars | Built |
| **GmailTool** | Read/send email | Planned |

Adding a new tool: create a class implementing the `Tool` interface in
`tools/`, register it in `tools/__init__.py`. No changes to the agent
loop needed.

## Google Calendar integration

The agent reads all calendars visible in your Google Calendar sidebar —
personal, school, shared calendars, birthdays, etc. It detects new events,
changed events, cancelled events, and imminent events (starting within 15
minutes). Each event is tagged with which calendar it came from.

### Setup

1. Create a Google Cloud project and enable the Calendar API
2. Create OAuth credentials and download the client secret JSON
3. Run the auth flow once from the terminal:
   ```bash
   python -c "from google_auth import run_auth_flow; run_auth_flow('jo')"
   ```
4. See `GOOGLE_CALENDAR_SETUP.md` for detailed step-by-step instructions

The agent automatically loads the calendar tool when credentials exist.
No configuration needed — just run the auth flow and restart the service.

### Error resilience

If the Google Calendar API starts failing, the agent tracks consecutive
failures. After 3 failures, it tells you via Telegram. After 10, it asks
you to re-authorize. Success resets the counter. The agent never silently
goes blind.

## Memory system

Purcival uses a three-tier persistent memory system. Every conversation is
stored, older conversations are automatically compressed into summaries
(using Claude for quality), and relevant summaries are retrieved via semantic
search to give each persona long-term memory.

### Data layout

```
data/
├── user_context.md          ← shared context about you (manually maintained)
├── jo/
│   ├── memory.db            ← messages, summaries, triggers, agent state
│   └── google_credentials.json  ← Google OAuth tokens (gitignored)
├── ada/
│   └── memory.db
└── ...
```

Each persona's memory is completely isolated — Jo doesn't know what
you discussed with Ada, and vice versa. This is a deliberate design choice
that mirrors how human relationships work.

The shared `user_context.md` file is the one piece of cross-persona context:
your background, values, family, goals. You update it manually when things
change. Every persona reads it on every message.

### Three tiers of memory

**Tier 1: Shared context** (`user_context.md`) — who you are. Manually
maintained. Read by all personas. Contains background information that
doesn't change often: family, career, values, interests.

**Tier 2: Conversation summaries** (SQLite `summaries` table) — automatically
generated condensations of older conversations using Claude. Each summary is
stored with a vector embedding for semantic search. When a new message
arrives, the system embeds the message, finds the most similar stored
summaries, and includes them in the prompt.

**Tier 3: Verbatim messages** (SQLite `messages` table) — the full record
of every message exchanged, timestamped in local time. Recent messages are
included directly in the API call's messages array. Older messages stay in
the database and are accessible only through their summaries.

### Agent state

In addition to conversation memory, the agent-enabled persona stores:

- **Narrative state** (`agent_narrative` table) — prose written by the LLM
  at the end of each cycle summarizing its current understanding. Read at
  the start of the next cycle for continuity. Append-only log with 30-day
  retention.
- **Structured state** (`tool_state` table) — key-value store for each
  tool's internal state (sync timestamps, seen IDs, event action history,
  calendar list cache, error tracking).
- **Action log** (`agent_actions` table) — audit trail of every action
  taken or proposed, with 30-day retention.
- **Reasoning log** (`reasoning_log` table) — full reasoning traces for
  debugging, with 7-day retention.

## Debug mode

The terminal interface supports prompt dumping for inspecting exactly what
the LLM receives on each message.

```bash
# Start with debug on
python main.py --persona jo --debug

# Or toggle mid-session
/debug
```

Debug dumps are saved to `debug/` as timestamped text files containing
the full system prompt (with all sections and retrieved summaries),
every message in the array with individual token counts, and a total
token breakdown.

## Project structure

```
purcival/
├── main.py              Terminal UI — persona picker, chat loop, /schedule
├── run_telegram.py      Telegram entry point — one persona per process
├── telegram_bot.py      PersonaBot class — messaging, agent scheduler
├── brain.py             LLM interface — routes to Claude or Ollama
├── context.py           Context assembly — builds full prompts from all sources
├── memory.py            Persistent storage — messages, summaries, triggers, agent state
├── embeddings.py        Vector embeddings — generates embeddings via Ollama
├── summarizer.py        Summarization engine — compresses old conversations via Claude
├── proactive.py         Agent bootstrap and scheduler
├── agent.py             Agent cycle + shared schedule update functions
├── google_auth.py       Google OAuth2 flow + credential management
├── tokens.py            Token counting — abstract interface for budget enforcement
├── config.py            Loads settings from .env
├── personas.py          Discovers and loads persona files
├── tools/
│   ├── __init__.py      Tool registry and factory
│   ├── base.py          Tool and ToolMethod base classes
│   ├── schedule_tool.py ScheduleTool — self-scheduling interface
│   ├── telegram_tool.py TelegramTool — Telegram send wrapper
│   └── google_calendar.py GoogleCalendarTool — multi-calendar reader
├── personas/            Personality definitions (markdown)
├── data/                Per-persona databases + shared user context (gitignored)
├── debug/               Prompt dumps from debug mode (gitignored)
├── tests/               Test suite
├── google_client_secret.json  Google OAuth client secret (gitignored)
├── purcival@.service    Systemd template for background services
├── requirements.txt     Python dependencies
├── .env.example         Configuration template
└── .gitignore           Keeps secrets, data, debug dumps, and artifacts out of git
```

## Dual-model architecture

**Claude** (via Anthropic API) — highest quality, costs per token, requires
internet. Switch to it with `/provider claude` in Telegram. Also used for
summarization and agent reasoning regardless of active chat provider.

**Ollama** (local inference) — free, private, runs on your GPU. Purcival
currently runs Mistral Small 3.2 on an RTX 3060 (12GB VRAM). A separate
embedding model (`nomic-embed-text`, ~270MB) runs on CPU for memory
retrieval without competing for GPU VRAM.

Both use the same message format. The brain module abstracts the provider
so nothing else in the app knows which model is responding.

## Configuration

All settings live in `.env` (never committed to git):

```bash
DEFAULT_PROVIDER=ollama
ANTHROPIC_API_KEY=your-key-here
CLAUDE_MODEL=claude-sonnet-4-6
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral-small3.2
DEFAULT_PERSONA=default
TELEGRAM_ALLOWED_USER_ID=
TELEGRAM_CHAT_ID=
TELEGRAM_TOKEN_ADA=
TELEGRAM_TOKEN_JO=
```

## Roadmap

- [x] Claude API integration
- [x] Local inference via Ollama
- [x] Dual-model routing with mid-conversation switching
- [x] Rich terminal UI with markdown rendering
- [x] Persona system — multiple personalities from markdown files
- [x] Telegram bots — one per persona, chat from your phone
- [x] Systemd services — auto-start on boot, auto-restart on crash
- [x] SQLite persistence — messages survive restarts
- [x] Shared user context (`user_context.md`)
- [x] Context assembly — full prompts from persona + context + history
- [x] Embedding infrastructure — vector similarity via nomic-embed-text
- [x] Conversation summarization — via Claude for quality
- [x] Semantic retrieval — surface relevant past conversations
- [x] Timestamps — local time on all messages and summaries
- [x] Device-aware responses — concise on Telegram, detailed in terminal
- [x] Proactive messaging — scheduled wake-ups with decision gate
- [x] Debug mode — dump full prompts for inspection
- [x] Cost optimization — balanced token budgets (~20K typical)
- [x] Safe clear — disabled on Telegram, confirmation required on CLI
- [x] Self-scheduling agent — plans own day, manages own wake-ups
- [x] Tool interface — extensible base class for agent capabilities
- [x] ScheduleTool — agent manages its own trigger schedule
- [x] TelegramTool — uniform interface for proactive messaging
- [x] Action validation — 7-check code-level gate, budget enforcement
- [x] Narrative state — LLM maintains prose understanding across cycles
- [x] Reasoning log — full traces for debugging (7-day retention)
- [x] User message plan updates — <schedule_updates> in both terminal and Telegram
- [x] Chat ID persistence — survives service restarts
- [x] /schedule preserves targeted wake-ups
- [x] Time awareness — agent knows current time and trigger fire time
- [x] Google Calendar integration (read-only, multi-calendar)
- [x] Calendar event diffing — new, changed, cancelled, imminent detection
- [x] Calendar error resilience — consecutive failure tracking with user notification
- [x] All-day event support — shown separately, no imminent logic
- [x] Calendar list caching with 24-hour TTL
- [ ] Gmail integration (read-only)
- [ ] Execute-tier approval flow via Telegram
- [ ] Large local model for async agent reasoning

## Requirements

- Python 3.10+
- Linux with systemd (for background services)
- For local inference: Ollama + a GPU (tested on RTX 3060 12GB)
- For embeddings: `ollama pull nomic-embed-text`
- For Claude: an Anthropic API key with credits
- For Google Calendar: Google Cloud project with Calendar API enabled
  (see GOOGLE_CALENDAR_SETUP.md)
- Telegram account and bot tokens from @BotFather