# Purcival

A personal AI assistant built from scratch, with help from claude, named after our cat.

Purcival runs on your own hardware, talks to you via Telegram on your phone,
and lets you choose between cloud AI (Claude) and a local model running on
your GPU. Each persona is a separate Telegram bot with its own personality
— message Purcival for intellectual sparring, Jocelyn for task management.

## How it works

```
Your phone (Telegram)                         Your Linux box
    │                                              │
    ├─ message Purcival bot ──→ Telegram servers ──→ ├─ run_telegram.py --persona purcival
    ├─ message Jocelyn bot ───→ Telegram servers ──→ ├─ run_telegram.py --persona jocelyn
    │                                              │
    │            Each process:                     │
    │            ┌─────────────────────┐            │
    │            │ Long poll Telegram  │            │
    │            │ Load persona prompt │            │
    │            │ Append to history   │            │
    │            │ Call brain.ask()  ──┼──→ Ollama (local GPU)
    │            │       or          ──┼──→ Claude API (cloud)
    │            │ Send response back  │            │
    │            └─────────────────────┘            │
```

Each persona runs as its own process with its own Telegram bot. Your Linux
box long-polls Telegram's servers — no open ports or public IP needed. The
model doesn't remember anything between API calls; your app manages all
conversation context.

## Quick start

```bash
git clone <your-repo-url> purcival
cd purcival

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your settings
```

## Two interfaces

### Terminal (for local development and testing)

```bash
python main.py                          # pick persona interactively
python main.py --persona purcival       # jump straight in
python main.py --provider claude        # use Claude instead of Ollama
python main.py -m "hello" --persona jocelyn  # single message
```

Terminal commands: `/persona`, `/claude`, `/ollama`, `/status`, `clear`, `quit`

### Telegram (for daily use from your phone)

```bash
# Test manually first
python run_telegram.py --persona purcival

# Run as a background service (starts on boot, auto-restarts)
sudo systemctl start purcival@purcival
sudo systemctl enable purcival@purcival

# Run multiple personas simultaneously
sudo systemctl start purcival@jocelyn
sudo systemctl enable purcival@jocelyn
```

Telegram commands: `/start`, `/provider`, `/status`, `/clear`

### Setting up Telegram

1. Message @BotFather on Telegram, create a bot for each persona
2. Message @userinfobot to get your user ID
3. Add tokens and user ID to `.env`:
   ```
   TELEGRAM_ALLOWED_USER_ID=123456789
   TELEGRAM_TOKEN_PURCIVAL=7123456:AAF...
   TELEGRAM_TOKEN_JOCELYN=7234567:BBG...
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

## Personas

Each persona is a markdown file in `personas/` that defines a system prompt.
The filename becomes the persona's name. Each persona gets its own Telegram
bot and runs as its own process.

```
personas/
├── purcival.md    — intellectual sparring partner
├── jocelyn.md     — efficient executive assistant
└── default.md     — general-purpose assistant
```

To add a new persona: create a `.md` file in `personas/`, create a Telegram
bot with @BotFather, add the token to `.env`, and start the service.

## Project structure

```
purcival/
├── main.py              Terminal UI — persona picker, chat loop
├── run_telegram.py      Telegram entry point — one persona per process
├── telegram_bot.py      Telegram bot logic — long polling, message handling
├── brain.py             LLM interface — routes to Claude or Ollama
├── config.py            Loads settings from .env
├── personas.py          Discovers and loads persona files
├── personas/            Personality definitions (markdown)
├── purcival@.service    Systemd template for background services
├── requirements.txt     Python dependencies
├── .env.example         Configuration template
└── .gitignore           Keeps secrets and artifacts out of git
```

## Dual-model architecture

**Claude** (via Anthropic API) — highest quality, costs per token, requires
internet. Switch to it with `/provider claude` in Telegram.

**Ollama** (local inference) — free, private, runs on your GPU. Purcival
currently runs Phi-4 on an RTX 3060 (12GB VRAM).

Both use the same message format. The brain module abstracts the provider
so nothing else in the app knows which model is responding.

## Known limitations

**Conversation history grows unbounded.** Every message in a session gets
appended to a list and sent to the model on every turn. Long conversations
will eventually hit the model's context window limit and increase latency
and cost. Stage 3 (SQLite persistence) will address this with context
windowing and summarization.

**History is in-memory only.** Conversations are lost when a process
restarts. Stage 3 will persist them to disk.

## Configuration

All settings live in `.env` (never committed to git):

```bash
DEFAULT_PROVIDER=ollama
ANTHROPIC_API_KEY=your-key-here
CLAUDE_MODEL=claude-sonnet-4-6
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=phi4
DEFAULT_PERSONA=default
TELEGRAM_ALLOWED_USER_ID=
TELEGRAM_TOKEN_PURCIVAL=
TELEGRAM_TOKEN_JOCELYN=
```

## Roadmap

- [x] Claude API integration
- [x] Local inference via Ollama (Phi-4 on RTX 3060)
- [x] Dual-model routing with mid-conversation switching
- [x] Rich terminal UI with markdown rendering
- [x] Persona system — multiple personalities from markdown files
- [x] Telegram bots — one per persona, chat from your phone
- [x] Systemd services — auto-start on boot, auto-restart on crash
- [ ] SQLite persistence — memory across sessions, context management
- [ ] Scheduled triggers — proactive messages
- [ ] Google Calendar integration (read-only)

## Requirements

- Python 3.10+
- Linux with systemd (for background services)
- For local inference: Ollama + a GPU (tested on RTX 3060 12GB)
- For Claude: an Anthropic API key with credits
- Telegram account and bot tokens from @BotFather
