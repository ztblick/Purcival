# PROGRESS.md — Purcival

This file is the rolling state of Purcival core development across
Claude Code sessions. Read it in full each session. Update sections
marked *updatable*; leave *STABLE* sections alone.

---

## Current focus                                          *updatable*

**Last updated:** 2026-05-16
**Active task:** OpenAI provider integration
**Phase:** Complete
**Status:** Implemented and tested. 16/16 tests pass (14 offline + 2 smoke).

---

## Active task — OpenAI provider integration

### Goal

Add OpenAI as a third LLM provider in Purcival alongside the existing
Anthropic Claude and Ollama providers, with **per-call-site model
selection** controlled by environment variables.

### Constraints from Zach (decided — do not relitigate)

- **Three call sites in scope:** chat, summary, reasoning. Embeddings
  remain on Ollama local (`nomic-embed-text`) and are out of scope.
- **Per-task model selection.** Each call site has its own model
  configured independently via env vars. The default env-var names
  are below; refine if Purcival already has an established naming
  convention.

```
OPENAI_API_KEY=sk-...
OPENAI_CHAT_MODEL=gpt-5.4-mini
OPENAI_SUMMARY_MODEL=gpt-5.4-nano
OPENAI_REASONING_MODEL=gpt-5.5
```

- **Starting models.** Use the values above as the initial defaults.
  Rationale: reasoning gets the frontier model because it's the most
  consequential call; chat gets the mini for fast/cheap conversation
  quality; summary gets the nano because the task is well-bounded.
  These can be swapped via env without code changes.
- **No reasoning-model (o-series) support in v1.** The o3/o4 family
  has a different API surface (no system prompt on some variants,
  reasoning tokens, different streaming). Document the path for
  adding them later, but don't implement.
- **No mixed-provider-per-call-site in v1.** The abstraction may
  support it naturally if the existing provider system is clean
  (Zach's stance: "this is already done pretty well"), but it's not
  a requirement. Don't bend the design for it.

### Design phase — what to produce

Read the current LLM provider abstraction in Purcival (start with
`brain.py` and trace from there) and produce a design doc at
`docs/openai_integration_design.md` covering:

1. **Current state.** What does the existing provider abstraction
   look like? How is the choice between Anthropic and Ollama made
   today? What is the actual signature and behavior of `brain.ask()`?
   Where does config live? Where do API keys come from?

2. **Proposed integration.** How does an `OpenAIProvider` plug in?
   - Does the existing abstraction support a new provider as-is, or
     does it need refining first?
   - If refining: what's the proposed shape of the unified provider
     interface? Show it.
   - How is per-call-site model selection wired? Where do the
     `OPENAI_CHAT_MODEL` / `OPENAI_SUMMARY_MODEL` /
     `OPENAI_REASONING_MODEL` env vars get read, and how are they
     dispatched to the right call site?
   - What's the path to mixing providers per call site later (e.g.,
     Anthropic for reasoning, OpenAI for chat) — is it free given
     the abstraction, or does it require additional design?

3. **API differences to handle.**
   - **API choice:** OpenAI's newer Responses API vs. Chat
     Completions. Pick one with rationale — Chat Completions
     probably matches Purcival's existing provider patterns more
     cleanly, but the Responses API is what OpenAI recommends for
     new work. Document the call.
   - Message format (OpenAI `messages` array; system prompt as a
     first message with role `system`)
   - Streaming format (SSE shape differs from Anthropic's)
   - Token limits, defaults, error shapes
   - Cost estimate based on Stage 5's typical call shapes
     (~6K input, ~500 output per reasoning cycle, ~12/day)

4. **Test plan.** Unit tests with a mocked OpenAI client; an
   optional smoke test against the real API behind an env-var
   guard. Match the existing provider tests' patterns.

### Acceptance for the design phase

`docs/openai_integration_design.md` exists, any remaining open
questions are listed clearly, and PROGRESS.md → "Decisions awaiting
Zach" has an entry pointing to the doc. Zach reviews and either
approves to proceed to implementation, asks for revisions, or
rescopes.

### Implementation phase

Follows the approved design. **Do not begin implementation until
Zach approves the design doc.**

---

## Open questions for Zach                               *updatable*

(None pending.)

---

## Decisions awaiting Zach's approval                    *updatable*

(None pending.)

When you stop at a gate, append an entry with:
- The phase / context
- The decision being requested
- Your proposed direction with rationale
- Path to a design doc if relevant

---

## Recent activity                                       *updatable*

Most recent first. Format:
`YYYY-MM-DD — task — what was done — commit shortref`.

2026-05-16 — ChatGPT integration — Implemented: brain.py (task dispatch + chatgpt provider + ollama fallback), config.py (per-task models for all 3 providers), agent.py/summarizer.py (task= param), main.py (/chatgpt switch), tests/test_brain_chatgpt.py (16/16 pass). max_completion_tokens fix applied from live API feedback.

---

## Decisions log                                       *append-only*

Format: `YYYY-MM-DD — title — rationale`.

Append entries; never edit prior ones.

- **2026-05-16 — OpenAI integration scoped to three call sites:
  chat, summary, reasoning.** Zach's brief. Embeddings remain on
  Ollama local. o-series reasoning models out of scope for v1.
- **2026-05-16 — Per-task model configuration via env vars.** Each
  call site independently configurable. Provider mixing across
  call sites is a possible future feature, not a v1 requirement.
- **2026-05-16 — Starting model assignments: reasoning → gpt-5.5,
  chat → gpt-5.4-mini, summary → gpt-5.4-nano.** Chosen for
  cost/capability tiering across call sites. Swappable via env.
- **2026-05-16 — Chat Completions API chosen over Responses API.**
  Structurally parallel to existing Ollama integration; Purcival manages
  its own session state; Responses API benefits don't apply here.
- **2026-05-16 — Single provider lever + task parameter design.**
  `DEFAULT_PROVIDER` controls all call sites. `brain.ask()` gains a `task`
  parameter (`"chat"`, `"summary"`, `"reasoning"`) that selects the right model
  within the provider family. `AGENT_REASONING_PROVIDER` and `SUMMARIZE_PROVIDER`
  module constants removed — call sites pass `task=` instead.
- **2026-05-16 — Per-task models for all three providers.**
  `CLAUDE_MODEL` and `OLLAMA_MODEL` replaced by per-task model vars for each
  family. Defaults: Claude uses Sonnet for chat, Haiku for summary, Opus for
  reasoning; Ollama defaults all tasks to phi4; ChatGPT uses gpt-5.4-mini/gpt-5.4-nano/gpt-5.5.
- **2026-05-16 — Provider name "chatgpt" not "openai".**
  Consistent with "claude" (product name, not company). API key env var stays
  `OPENAI_API_KEY` (industry standard); provider name in code/CLI is `"chatgpt"`.
- **2026-05-16 — GPT-5 models use max_completion_tokens, not max_tokens.**
  Confirmed via live API: gpt-5.4-mini, gpt-5.4-nano, gpt-5.5 reject `max_tokens`
  with a 400 error. `_ask_chatgpt` uses `max_completion_tokens` instead. The
  `max_tokens` parameter to `brain.ask()` is still the public interface name.

---

## Backlog                                               *updatable*

Future tasks, roughly grouped. Not commitments — Zach decides what
becomes active.

### Known issues

- **Trigger-deletion bug.** Some agent-scheduled triggers have been
  found deleted between cycles with no corresponding cancel commands
  in the reasoning log. Root cause unknown. Investigation, not a
  quick patch.

### Deferred work (related to OpenAI integration)

- Add support for OpenAI o-series reasoning models (o3, o4-mini)
  once the chat-model integration is stable. Requires handling
  reasoning tokens, different system-prompt semantics, and possibly
  the Responses API.
- Allow mixing providers across call sites (e.g., Anthropic for
  reasoning, OpenAI for chat). May fall out for free once the v1
  integration ships; otherwise a follow-up.

### Other deferred work

- Restructure tests into a formal `tests/` directory
- Memory architecture evolution beyond Stage 3 (lossy summaries →
  something closer to a "living memory" graph; see Zach's prior
  exploration of Graphiti and faceted entities)

### Possible next features (post-OpenAI)

- *(Populated as Zach plans.)*

---

## Reference docs (read these for context)

- `CLAUDE.md` — operating manual (this file's companion)
- `PURCIVAL_DESIGN_DOC.md` — overall system design (living)
- `STAGE5_AGENT_DESIGN.md` — agent loop architecture
- `README.md` — project overview and setup

If any of these go stale because of changes you make, update them
in the same session.
