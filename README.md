# Purcival

A personal AI assistant built from scratch, with help from Claude.S

Purcival runs on your own hardware, talks to you in the terminal, and lets you
choose between cloud AI (Claude) and a local model running on your GPU. Different
personas give it different personalities for different jobs.

## How it works

```
You (terminal) ──→ Main ──→ Brain ──→ Claude API (cloud)
                     │         │
                     │         └──→ Ollama / Phi-4 (local GPU)
                     │
                     └──→ Persona files (personality)
                     └──→ Conversation history (in-memory, per session)
```

You type a message. The app loads the active persona's system prompt, appends
your message to the conversation history, sends everything to whichever LLM
provider is active, and renders the response with markdown formatting in your
terminal. The model doesn't remember anything — your app manages all context.

## Quick start

```bash
git clone <your-repo-url> purcival
cd purcival

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your settings

# Make sure Ollama is running with a model pulled:
#   ollama pull phi4

python main.py
```

## Usage

```bash
# Interactive mode — pick a persona at startup
python main.py

# Jump straight to a persona
python main.py --persona percival

# Use Claude instead of Ollama
python main.py --provider claude

# Send a single message
python main.py -m "What should I focus on today?" --persona jocelyn
```

### In-session commands

| Command    | What it does                              |
|------------|-------------------------------------------|
| `/persona` | Switch persona (clears conversation)      |
| `/claude`  | Switch to Claude API                      |
| `/ollama`  | Switch to local model                     |
| `/status`  | Show current persona, provider, and model |
| `clear`    | Reset conversation history                |
| `quit`     | Exit                                      |

## Personas

Each persona is a markdown file in `personas/` that defines a system prompt.
The filename becomes the persona's name.

```
personas/
├── percival.md    — intellectual sparring partner
├── jocelyn.md     — efficient executive assistant
└── default.md     — general-purpose assistant
```

**To create a new persona,** add a `.md` file to `personas/`. No code changes
needed — the app discovers it automatically. Write it however makes sense to
you; the full file contents become the system prompt.

## Project structure

```
purcival/
├── main.py             Entry point — terminal UI, persona picker, chat loop
├── brain.py            LLM interface — routes to Claude or Ollama
├── config.py           Loads settings from .env
├── personas.py         Discovers and loads persona files
├── personas/           Personality definitions (markdown)
├── requirements.txt    Python dependencies
├── .env.example        Configuration template
└── .gitignore          Keeps secrets and artifacts out of git
```

## Dual-model architecture

Purcival can talk to two different LLM backends:

**Claude** (via Anthropic API) — highest quality, costs money per token,
requires internet. Best for deep conversations and complex reasoning.

**Ollama** (local inference) — free, private, runs on your GPU, works offline.
Quality depends on your hardware and model choice. Good for routine tasks,
quick questions, and development.

Both use the same message format. You can switch mid-conversation and the
history carries over. The brain module hides the provider differences so
the rest of the app doesn't care which model is answering.

## Configuration
S
All settings live in `.env` (never committed to git):

```bash
DEFAULT_PROVIDER=ollama          # or "claude"
ANTHROPIC_API_KEY=your-key-here  # for Claude
CLAUDE_MODEL=claude-sonnet-4-6
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=phi4
DEFAULT_PERSONA=default          # used for single-message mode
```

## Roadmap

- [x] Claude API integration
- [x] Local inference via Ollama
- [x] Dual-model routing with mid-conversation switching
- [x] Rich terminal UI with markdown rendering
- [x] Persona system with multiple personalities
- [ ] Telegram bot — chat from your phone
- [ ] SQLite persistence — memory across sessions
- [ ] Scheduled triggers — proactive messages
- [ ] Google Calendar integration (read-only)

## Requirements

- Python 3.10+
- For local inference: Ollama + a GPU with enough VRAM for your model
- For Claude: an Anthropic API key with credits
