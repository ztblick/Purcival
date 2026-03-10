# Zach's Personal AI Assistant

A simple, self-hosted AI assistant built in stages.

## Architecture

```
You (Telegram) ──→ Bot Layer ──→ Router ──→ Claude API (primary)
                                    │
                                    └──→ Ollama (local, future)
                                    
Scheduler ──→ Calendar Check ──→ Router ──→ You (Telegram)

Everything persists to SQLite.
```

## Build Stages

- [x] **Stage 1:** Claude API integration — send messages, get responses
- [ ] **Stage 2:** Telegram bot — chat from your phone
- [ ] **Stage 3:** SQLite persistence — conversation memory
- [ ] **Stage 4:** Scheduled triggers — proactive morning briefings
- [ ] **Stage 5:** Google Calendar integration (read-only)
- [ ] **Stage 6:** Local model via Ollama — dual-model routing

## Setup

### 1. Clone and create virtual environment

```bash
git clone <your-repo-url>
cd assistant
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure your API key

```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

### 4. Run

```bash
# Interactive chat mode
python main.py

# Single message
python main.py --message "What's on my mind today?"
```

## Project Structure

```
assistant/
├── .env.example        # Template for secrets (never commit .env)
├── .gitignore          # Keeps secrets and venv out of git
├── requirements.txt    # Python dependencies
├── config.py           # Loads configuration from environment
├── brain.py            # LLM interface (Claude now, Ollama later)
├── main.py             # Entry point — interactive chat loop
└── README.md
```

## Key Principles

- **Secrets never go in code.** API keys live in `.env`, which is gitignored.
- **The brain is swappable.** `brain.py` abstracts the LLM provider so we can
  add Ollama later without changing anything else.
- **Each stage builds on the last.** No rewrites, just new layers.
