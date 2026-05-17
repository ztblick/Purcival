# OpenAI Provider Integration — Design Doc

**Status:** Draft v2 — awaiting Zach's review  
**Date:** 2026-05-16  
**Scope:** `brain.py`, `config.py`, `agent.py`, `summarizer.py`, `main.py`, new tests

---

## 1. Current state

### Provider abstraction

`brain.py` is the single LLM gateway. One public function:

```python
def ask(
    messages: list[dict],
    system: str | None = None,
    provider: str | None = None,
    max_tokens: int = 2048,
) -> str:
```

A `_PROVIDERS` dict maps string names to handler functions of the shape
`(messages, system, max_tokens) -> str`. The router looks up the name and
delegates. Nothing outside `brain.py` knows which model answered.

### How the call sites work today

| Call site | File | Provider | Max tokens |
|---|---|---|---|
| Chat | `main.py` | Runtime-switchable, starts from `DEFAULT_PROVIDER` | 2048 |
| Reasoning | `agent.py` | `AGENT_REASONING_PROVIDER = "claude"` (hardcoded) | 4096 |
| Summary | `summarizer.py` | `SUMMARIZE_PROVIDER = "claude"` (hardcoded) | 2048 |

### Problems with the current design

1. **Provider and model are conflated.** `config.CLAUDE_MODEL` is one model for
   every task. There's no way to use a cheaper model for summaries and a better
   one for reasoning — all calls go to the same model.

2. **Provider constants are hardcoded in modules.** Changing the reasoning
   provider requires editing `agent.py`. There's no single lever.

3. **No fallback.** If a provider is unconfigured, calls fail hard.

---

## 2. Proposed integration

### Design principle

A single `DEFAULT_PROVIDER` in `.env` — one of `"ollama"`, `"claude"`,
`"chatgpt"` — is the only thing that needs to change to switch the whole system
between provider families. Per-task models within each family are configurable
via env vars but have sensible defaults that require no tuning.

### What changes in `brain.ask()`

Add one parameter: `task: str = "chat"`. The task tells `brain.py` which model
within the provider family to use. Valid values: `"chat"`, `"summary"`,
`"reasoning"`.

New signature:

```python
def ask(
    messages: list[dict],
    system: str | None = None,
    provider: str | None = None,
    max_tokens: int = 2048,
    task: str = "chat",
) -> str:
```

`provider` still works as an explicit override (for runtime switching in the
CLI). When omitted, it falls back to `config.DEFAULT_PROVIDER`.

### What changes in `config.py`

Replace the single `CLAUDE_MODEL` and `OLLAMA_MODEL` with per-task models for
all three provider families:

```python
# --- Provider (single lever) ---
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "ollama")

# --- Claude ---
ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_CHAT_MODEL      = os.getenv("CLAUDE_CHAT_MODEL",      "claude-sonnet-4-6")
CLAUDE_SUMMARY_MODEL   = os.getenv("CLAUDE_SUMMARY_MODEL",   "claude-haiku-4-5-20251001")
CLAUDE_REASONING_MODEL = os.getenv("CLAUDE_REASONING_MODEL", "claude-opus-4-7")

# --- ChatGPT ---
OPENAI_API_KEY           = os.getenv("OPENAI_API_KEY", "")  # standard env var name
CHATGPT_CHAT_MODEL       = os.getenv("CHATGPT_CHAT_MODEL",      "gpt-5.4-mini")
CHATGPT_SUMMARY_MODEL    = os.getenv("CHATGPT_SUMMARY_MODEL",   "gpt-5.4-nano")
CHATGPT_REASONING_MODEL  = os.getenv("CHATGPT_REASONING_MODEL", "gpt-5.5")

# --- Ollama ---
OLLAMA_BASE_URL         = os.getenv("OLLAMA_BASE_URL",        "http://localhost:11434")
OLLAMA_CHAT_MODEL       = os.getenv("OLLAMA_CHAT_MODEL",      "phi4")
OLLAMA_SUMMARY_MODEL    = os.getenv("OLLAMA_SUMMARY_MODEL",   "phi4")
OLLAMA_REASONING_MODEL  = os.getenv("OLLAMA_REASONING_MODEL", "phi4")
```

**On naming:** The env var for the API key is `OPENAI_API_KEY` — the industry
standard, familiar to anyone who has used the OpenAI ecosystem. The provider
name used everywhere in code and CLI is `"chatgpt"`, consistent with how
`"claude"` names the Anthropic provider (product name, not company name).

**On backward compat:** `CLAUDE_MODEL` and `OLLAMA_MODEL` are removed. Any
`.env` file using them will need updating. Since these are secrets-adjacent
config files, Zach manages them manually — no migration code needed.

### What changes in `brain.py`

Replace the `_PROVIDERS` dict with two structures: a handler map (one entry per
provider) and a model map (one entry per provider+task combination):

```python
# Model lookup — keyed (provider, task), values read from config at call time
_MODELS: dict[tuple[str, str], Callable[[], str]] = {
    ("claude",   "chat"):      lambda: config.CLAUDE_CHAT_MODEL,
    ("claude",   "summary"):   lambda: config.CLAUDE_SUMMARY_MODEL,
    ("claude",   "reasoning"): lambda: config.CLAUDE_REASONING_MODEL,
    ("chatgpt",  "chat"):      lambda: config.CHATGPT_CHAT_MODEL,
    ("chatgpt",  "summary"):   lambda: config.CHATGPT_SUMMARY_MODEL,
    ("chatgpt",  "reasoning"): lambda: config.CHATGPT_REASONING_MODEL,
    ("ollama",   "chat"):      lambda: config.OLLAMA_CHAT_MODEL,
    ("ollama",   "summary"):   lambda: config.OLLAMA_SUMMARY_MODEL,
    ("ollama",   "reasoning"): lambda: config.OLLAMA_REASONING_MODEL,
}

# Handler lookup — one function per provider, each takes (messages, system, max_tokens, model)
_HANDLERS = {
    "claude":  _ask_claude,
    "chatgpt": _ask_chatgpt,
    "ollama":  _ask_ollama,
}
```

Each handler now takes an explicit `model` parameter. The router picks model and
handler, then delegates:

```python
def ask(messages, system, provider=None, max_tokens=2048, task="chat") -> str:
    if system is None:
        raise ValueError("system prompt is required — load a persona first")

    effective = provider or config.DEFAULT_PROVIDER
    handler = _HANDLERS.get(effective)

    if handler is None:
        logger.warning(f"Unknown provider '{effective}', falling back to ollama")
        effective = "ollama"
        handler = _ask_ollama

    model = _MODELS.get((effective, task), _MODELS[("ollama", "chat")])()

    try:
        return handler(messages, system, max_tokens, model)
    except RuntimeError as e:
        # Raised by _ensure_* when API key is missing
        if effective != "ollama":
            logger.warning(
                f"Provider '{effective}' unavailable: {e}. Falling back to ollama."
            )
            fallback_model = _MODELS[("ollama", task)]()
            return _ask_ollama(messages, system, max_tokens, fallback_model)
        raise
```

The ChatGPT handler follows the same lazy-load pattern as Claude:

```python
_chatgpt_client = None

def _ensure_chatgpt():
    global _chatgpt_client
    if _chatgpt_client is not None:
        return _chatgpt_client
    if not config.OPENAI_API_KEY:
        raise RuntimeError("ChatGPT provider requires OPENAI_API_KEY in .env")
    from openai import OpenAI
    _chatgpt_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _chatgpt_client


def _ask_chatgpt(messages: list[dict], system: str, max_tokens: int, model: str) -> str:
    client = _ensure_chatgpt()
    full_messages = [{"role": "system", "content": system}] + messages
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=full_messages,
    )
    return response.choices[0].message.content
```

The Claude and Ollama handlers gain a `model` parameter where they previously
used `config.CLAUDE_MODEL` / `config.OLLAMA_MODEL` directly.

### What changes in `agent.py`

Remove the `AGENT_REASONING_PROVIDER` constant entirely. Replace the `brain.ask()`
call:

```python
# before
llm_response = brain.ask(
    messages, system=system_prompt,
    provider=AGENT_REASONING_PROVIDER,
    max_tokens=AGENT_REASONING_MAX_TOKENS,
)

# after
llm_response = brain.ask(
    messages, system=system_prompt,
    max_tokens=AGENT_REASONING_MAX_TOKENS,
    task="reasoning",
)
```

No provider argument — uses `DEFAULT_PROVIDER` from config. Also update the
`provider=AGENT_REASONING_PROVIDER` reference in `memory.add_reasoning_log()`
calls; replace with `config.DEFAULT_PROVIDER`.

### What changes in `summarizer.py`

Remove the `SUMMARIZE_PROVIDER` constant. Replace the `brain.ask()` call:

```python
# before
summary = brain.ask(
    messages=[{"role": "user", "content": user_prompt}],
    system=SUMMARIZE_SYSTEM_PROMPT,
    provider=SUMMARIZE_PROVIDER,
)

# after
summary = brain.ask(
    messages=[{"role": "user", "content": user_prompt}],
    system=SUMMARIZE_SYSTEM_PROMPT,
    task="summary",
)
```

### What changes in `main.py`

1. Add `/chatgpt` runtime switch (parallel to existing `/claude` and `/ollama`).
2. Update `argparse` `--provider` choices to include `"chatgpt"`.
3. Update the banner's Commands section to include `/chatgpt`.
4. Fix the `/status` model display — it currently hardcodes
   `config.CLAUDE_MODEL if provider == "claude" else config.OLLAMA_MODEL`.
   Replace with a lookup that uses the chat task's model for the active provider.

The interactive chat call site already passes `provider` explicitly and will
continue to do so. No change to the `brain.ask()` call itself — `task` defaults
to `"chat"`, which is correct.

### Mixing providers per call site (path forward)

The current design routes all call sites through `DEFAULT_PROVIDER`. If Zach
later wants (e.g.) Claude for reasoning and ChatGPT for chat, the path is:
add a `REASONING_PROVIDER` and `SUMMARY_PROVIDER` override in `config.py`, read
them in `brain.ask()` per task. The `_HANDLERS` and `_MODELS` tables already
support any combination — it's purely a config question.

---

## 3. API differences

### Chat Completions vs. Responses API

**Decision: Chat Completions.**

Rationale: Ollama already uses the Chat Completions endpoint, and `_ask_ollama`
already prepends system as a `"system"` role message — identical to what ChatGPT
requires. Same message shape, same response accessor
(`choices[0].message.content`), same sync call. The Responses API adds value for
multi-turn session state and built-in tool use, neither of which Purcival needs
(it manages both itself). Chat Completions is also more stable across the SDK.

The Responses API path is noted in section 7 (o-series / deferred).

### Message format

OpenAI Chat Completions uses the same `[{"role": ..., "content": ...}]` format
Purcival already uses. System prompt prepended as `role: "system"` — same as
the existing `_ask_ollama` implementation.

### Streaming

Not applicable. All current providers are synchronous single-shot calls.
`client.chat.completions.create()` without `stream=True` returns a complete
response.

### Token limits

| Model | Context | Output cap |
|---|---|---|
| gpt-5.4-mini (chat) | TBD — verify at deployment | TBD |
| gpt-5.4-nano (summary) | TBD — verify at deployment | TBD |
| gpt-5.5 (reasoning) | TBD — verify at deployment | TBD |

Purcival's calls use `max_tokens=4096` (reasoning) and `max_tokens=2048`
(chat/summary) — both should be within any expected limits.

### Error shapes

OpenAI raises `openai.APIError` subclasses rather than Anthropic's hierarchy.
The fallback in `brain.ask()` catches `RuntimeError` (raised by `_ensure_chatgpt`
for missing keys). The call sites use bare `except Exception` and log — they
absorb API errors cleanly without changes.

### Cost estimate

Cannot provide exact figures: GPT-5 family pricing is not in my knowledge base.
At deployment, estimate using current pricing. For reference at gpt-4o-mini
levels (~$0.15/M input, ~$0.60/M output): 12 reasoning cycles/day at 6K/500
tokens ≈ $0.04/day.

---

## 4. New dependency

`openai` Python package. New third-party dependency.

**Proposed addition to requirements (or equivalent):**
```
openai>=1.0.0
```

Functionally equivalent in project status to the `anthropic` package already in
use. Needs a Decisions log entry per project rules.

---

## 5. Test plan

### Unit tests (`tests/test_brain_chatgpt.py`)

All offline, mocking the OpenAI client.

- `test_chatgpt_sends_system_as_first_message` — system prompt prepended with `role: "system"`
- `test_chatgpt_uses_chat_model_for_chat_task` — `brain.ask(..., task="chat")` uses `config.CHATGPT_CHAT_MODEL`
- `test_chatgpt_uses_summary_model_for_summary_task` — uses `config.CHATGPT_SUMMARY_MODEL`
- `test_chatgpt_uses_reasoning_model_for_reasoning_task` — uses `config.CHATGPT_REASONING_MODEL`
- `test_ask_routes_to_chatgpt_when_default_provider` — `DEFAULT_PROVIDER="chatgpt"` → ChatGPT handler called
- `test_ask_falls_back_to_ollama_on_missing_key` — clear `OPENAI_API_KEY`, assert Ollama handler is called
- `test_ask_falls_back_to_ollama_on_unknown_provider` — `provider="unknown"` → Ollama
- `test_chatgpt_client_lazy_loaded` — client not initialized until first call
- `test_raises_without_api_key_before_fallback` — `_ensure_chatgpt()` raises `RuntimeError` with helpful message

### Config tests (add to existing or new `test_config.py`)

- `test_chatgpt_models_read_from_env` — set env vars, verify three model names read correctly
- `test_chatgpt_models_use_defaults` — clear env vars, verify default model names
- `test_claude_per_task_models` — verify Claude's three models are independently configurable
- `test_ollama_per_task_models` — verify Ollama's three models are independently configurable

### Regression: existing provider tests

Existing Claude and Ollama tests should pass unchanged. The handler signatures
change (gain a `model` parameter), so update any tests that call `_ask_claude`
or `_ask_ollama` directly.

### Smoke test (gated)

```python
def _chatgpt_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))
```

- `test_smoke_chatgpt_chat` — send a minimal message with `task="chat"`, assert non-empty string
- `test_smoke_chatgpt_reasoning` — same with `task="reasoning"`

---

## 6. Open questions

1. **GPT-5 model availability** — confirm `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.5`
   are live in the OpenAI API before wiring them as defaults. The env-var design
   means any substitution is a one-line `.env` change.

2. **`max_tokens` vs. `max_completion_tokens`** — newer OpenAI models may prefer
   `max_completion_tokens`. Verify at implementation time; adjust `_ask_chatgpt`
   if needed.

3. **Claude per-task model defaults** — the proposed defaults
   (`claude-haiku-4-5-20251001` for chat/summary, `claude-sonnet-4-6` for
   reasoning) match the existing `CLAUDE_MODEL` value for the reasoning case.
   Check whether chat and summary should also default to Sonnet for consistency
   with today's behavior, or whether Haiku is acceptable for those tasks.

4. **Ollama per-task model defaults** — all three currently default to `phi4`.
   If Zach wants different local models for reasoning vs. chat, set
   `OLLAMA_REASONING_MODEL` in `.env`. No design change required.

---

## 7. Path to o-series support (deferred, not v1)

o3 / o4-mini have different API semantics (no system prompt in some variants,
`max_completion_tokens`, reasoning tokens). To add: register an
`"openai_o"` handler that converts the system prompt into the first user message
and uses `max_completion_tokens`. Set `DEFAULT_PROVIDER=openai_o` to activate.
The `_HANDLERS` / `_MODELS` tables make this a drop-in addition.

---

## Summary of files changed

| File | Change |
|---|---|
| `brain.py` | Add `_ask_chatgpt()`, replace `_PROVIDERS` with `_HANDLERS` + `_MODELS`, add `task` param to `ask()`, add Ollama fallback |
| `config.py` | Replace `CLAUDE_MODEL` / `OLLAMA_MODEL` with 9 per-task model vars + `OPENAI_API_KEY` + 3 ChatGPT model vars |
| `agent.py` | Remove `AGENT_REASONING_PROVIDER`, pass `task="reasoning"` to `brain.ask()` |
| `summarizer.py` | Remove `SUMMARIZE_PROVIDER`, pass `task="summary"` to `brain.ask()` |
| `main.py` | Add `/chatgpt` switch, update `argparse` choices, fix `/status` model display |
| `tests/test_brain_chatgpt.py` | New: unit + smoke tests |
| `requirements.txt` (or equiv) | Add `openai>=1.0.0` |
