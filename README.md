# Purcival

A personal AI assistant built from scratch, with help from Claude, named after our cat.

Purcival runs on your own hardware, talks to you via Telegram on your phone,
and lets you choose between cloud AI (Claude) and a local model running on
your GPU. Each persona is a separate Telegram bot with its own personality,
its own persistent memory, and its own conversation history. Conversations
survive restarts, reboots, and crashes. Older conversations are automatically
summarized and retrieved by semantic similarity when they become relevant again.

## How it works

```
Your phone (Telegram)                         Your Linux box
    │                                              │
    ├─ message Purcival bot ──→ Telegram servers ──→ ├─ run_telegram.py --persona purcival
    ├─ message Ada bot ───────→ Telegram servers ──→ ├─ run_telegram.py --persona ada
    │                                              │
    │            Each process:                     │
    │            ┌──────────────────────────┐       │
    │            │ Long poll Telegram       │       │
    │            │ Persist user message     │──→ data/<persona>/memory.db
    │            │ Assemble context:        │       │
    │            │   ├─ Persona prompt      │←── personas/<persona>.md
    │            │   ├─ User context        │←── data/user_context.md
    │            │   ├─ Session info        │    (current time, device type)
    │            │   ├─ Relevant summaries  │←── memory.db (semantic search)
    │            │   └─ Recent messages     │←── memory.db (verbatim history)
    │            │ Call brain.ask()       ──┼──→ Ollama (local GPU)
    │            │       or              ──┼──→ Claude API (cloud)
    │            │ Persist response         │──→ memory.db
    │            │ Send response to user    │       │
    │            │ Check if summarization   │       │
    │            │   needed → if so:        │       │
    │            │   Summarize old messages  │──→ Claude API (always)
    │            │   Embed summary           │──→ nomic-embed-text
    │            │   Store summary + vector  │──→ memory.db
    │            │                          │       │
    │            │ Proactive scheduler:      │       │
    │            │   Check triggers every 60s│       │
    │            │   If due → compose msg ──┼──→ Ollama or Claude
    │            │   Send to user            │       │
    │            └──────────────────────────┘       │
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
python main.py --persona purcival       # jump straight in
python main.py --provider claude        # use Claude instead of Ollama
python main.py --debug                  # dump full prompts to debug/
python main.py -m "hello" --persona ada # single message
```

Terminal commands: `/persona`, `/claude`, `/ollama`, `/status`, `/debug`, `clear`, `quit`

The terminal interface shows a spinner when summarization is running
("Committing conversation to memory...") and allows longer, more detailed
responses from the LLM. The `clear` command requires typed confirmation
before deleting data.

### Telegram (for daily use from your phone)

```bash
# Test manually first
python run_telegram.py --persona purcival

# Run as a background service (starts on boot, auto-restarts)
sudo systemctl start purcival@purcival
sudo systemctl enable purcival@purcival

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
   TELEGRAM_TOKEN_PURCIVAL=7123456:AAF...
   TELEGRAM_TOKEN_ADA=7234567:BBG...
   ```
4. Test: `python run_telegram.py --persona purcival`

### Setting up systemd

1. Edit `purcival@.service` — replace `YOUR_USERNAME` with your Linux username
2. `sudo cp purcival@.service /etc/systemd/system/`
3. `sudo systemctl daemon-reload`
4. `sudo systemctl start purcival@purcival`
5. `sudo systemctl enable purcival@purcival`

Useful commands:
```bash
journalctl -u purcival@purcival -f     # live logs
sudo systemctl restart purcival@purcival  # restart after code changes
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
├── purcival.md    — English butler, detail-oriented life manager
├── ada.md         — spunky technical sparring partner
├── jo.md          — efficient executive assistant
└── default.md     — general-purpose assistant
```

**Purcival** is a detail-oriented personal assistant modeled after a capable
English butler. Witty, warm, and to the point. He remembers the details of
your life — deadlines, commitments, people's names — and keeps you on track
with your goals. He gives gentle reminders when things are slipping and is
honest when you need to hear it.

**Ada** is a technical expert and thinking partner. Sharp, curious, and a
little irreverent. She is who you talk to about coding, systems design,
math, science, and engineering. She explains things clearly without dumbing
them down, pushes back on weak ideas, and gets excited when a conversation
goes somewhere interesting. She does not care about schedules or to-do lists.

To add a new persona: create a `.md` file in `personas/`, create a Telegram
bot with @BotFather, add the token to `.env`, and start the service. The
persona gets a fresh, independent memory database automatically.

## Memory system

Purcival uses a three-tier persistent memory system. Every conversation is
stored, older conversations are automatically compressed into summaries
(using Claude for quality), and relevant summaries are retrieved via semantic
search to give each persona long-term memory.

### Data layout

```
data/
├── user_context.md          ← shared context about you (manually maintained)
├── purcival/
│   └── memory.db            ← Purcival's messages, summaries, embeddings, triggers
├── ada/
│   └── memory.db            ← Ada's messages, summaries, embeddings, triggers
└── jo/
    └── memory.db
```

Each persona's memory is completely isolated — Purcival doesn't know what
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

### Context assembly

Every API call assembles a prompt from these components, each with a token budget:

```
System prompt:
  1. Persona prompt          (< 2,000 tokens)  — from personas/*.md
  2. User context            (< 2,000 tokens)  — from data/user_context.md
  3. Session info            (< 500 tokens)    — current time, device type
  4. Relevant summaries      (< 8,000 tokens)  — semantic search results
  5. Additional context      (< 8,000 tokens)  — reserved for future integrations

Messages array:
  6. Recent verbatim messages (< 8,000 tokens)  — with timestamps on user messages
```

Typical total: ~20,000 tokens. Upper bound: ~32,000 tokens. The verbatim
message window is deliberately moderate — this is the largest cost driver
since it's sent on every API call. Older messages are covered by summaries.

### Summarization

Summarization triggers after a response when unsummarized messages exceed
6,000 tokens. Multiple batches (up to 5) can be processed in a single pass
to catch up on backlogs. Each batch (~3,000 tokens, roughly 15-25 exchanges)
becomes one summary. Summarization always uses Claude for quality — local
models did not meet the accuracy bar. The summarization prompt instructs
the model to write natural paragraphs, include timestamps, and never invent
details that weren't discussed.

### Retrieval

When a message arrives, the system embeds it and searches for stored
summaries with high cosine similarity. Summaries below a minimum
similarity threshold (0.35) are excluded. The 2 most recent summaries
are always included regardless of similarity to maintain conversational
continuity. Up to 8 summaries can be retrieved per message.

## Proactive messaging

Each persona can initiate conversations via Telegram on a schedule.
Hourly check-in triggers fire from 6am to 11pm. When a trigger fires,
the decision gate checks whether to actually send a message:

- **Reminders** → always send
- **Calendar events** → always send
- **Check-ins** → skip if conversation was active in the last 15 minutes

If the gate approves, the message composer assembles context (including
the current time, time since last message, and trigger-specific instructions)
and asks the LLM to compose a natural proactive message. The message is
persisted to the database so the conversation continues naturally if the
user replies.

Triggers are stored in the persona's SQLite database and survive restarts.
New hourly triggers are seeded automatically on bot startup.

## Debug mode

The terminal interface supports prompt dumping for inspecting exactly what
the LLM receives on each message.

```bash
# Start with debug on
python main.py --persona purcival --debug

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
├── main.py              Terminal UI — persona picker, chat loop, debug mode
├── run_telegram.py      Telegram entry point — one persona per process
├── telegram_bot.py      PersonaBot class — messaging, proactive scheduler
├── brain.py             LLM interface — routes to Claude or Ollama
├── context.py           Context assembly — builds full prompts from all sources
├── memory.py            Persistent storage — messages, summaries, embeddings, triggers
├── embeddings.py        Vector embeddings — generates embeddings via Ollama
├── summarizer.py        Summarization engine — compresses old conversations via Claude
├── proactive.py         Proactive messaging — scheduler, decision gate, composer
├── tokens.py            Token counting — abstract interface for budget enforcement
├── config.py            Loads settings from .env
├── personas.py          Discovers and loads persona files
├── personas/            Personality definitions (markdown)
├── data/                Per-persona databases + shared user context (gitignored)
├── debug/               Prompt dumps from debug mode (gitignored)
├── tests/               Test suite (future: consolidate all tests here)
├── purcival@.service    Systemd template for background services
├── requirements.txt     Python dependencies
├── .env.example         Configuration template
└── .gitignore           Keeps secrets, data, debug dumps, and artifacts out of git
```

## Dual-model architecture

**Claude** (via Anthropic API) — highest quality, costs per token, requires
internet. Switch to it with `/provider claude` in Telegram. Also used for
summarization regardless of active chat provider.

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
TELEGRAM_TOKEN_PURCIVAL=
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
- [ ] Organize tests into tests/ directory
- [ ] Google Calendar integration (read-only)
- [ ] Gmail integration (read-only)
- [ ] Reminder parsing — teach LLM to create triggers from natural language

## Requirements

- Python 3.10+
- Linux with systemd (for background services)
- For local inference: Ollama + a GPU (tested on RTX 3060 12GB)
- For embeddings: `ollama pull nomic-embed-text`
- For Claude: an Anthropic API key with credits
- Telegram account and bot tokens from @BotFather