# PROGRESS.md — Purcival

This file is the rolling state of Purcival core development across
agent sessions. Read it in full each session. Update sections marked
*updatable*; leave *STABLE* sections alone.

---

## Current focus                                          *updatable*

**Last updated:** 2026-05-17
**Active task:** Goals dashboard — local web app with goal/step tracking and proactive suggestions
**Phase:** 5 — Agent loop integration: suggestion generation (ready for next development cycle)
**Status:** Phase 4 complete. Clicking a goal or step opens a scoped Jo chat panel, messages persist with `scope_type` / `scope_id`, responses are delivered over SSE using the `brain.stream()` fallback interface, and scoped summarization runs after responses. Route plus Playwright coverage verifies streaming, reload persistence, and no leakage into Jo's default chat. Full pytest passes.

---

## Active task — Goals dashboard

### What this is

A local web app that:

- Surfaces Zach's active goals (organized by category) and the suggested next steps toward each
- Lets Zach accept (✓) or reject (✗) suggested steps
- Tracks accepted steps and holds Zach accountable to them
- Opens a focused chat thread with the active persona about any goal or step when clicked
- Generates new suggestions via the agent loop during planning cycles
- Uses accept/reject outcomes as feedback to improve future suggestions

The single-sentence framing: **Purcival gains a UI that tracks Zach's goals, suggests concrete next steps, and supports focused conversations about each.**

### Hierarchy (decided)

```
Category (e.g., "career", "health", "home", "money")
  └── Goal (e.g., "Learn more about AI safety")
        └── Step (e.g., "Research LucidAI and their tech")
```

Categories tag goals. Goals contain steps. Every step belongs to exactly one goal. The term **"step"** is used throughout (not "task") — the connotation of incremental movement toward a goal matters.

### Constraints from Zach (decided — do not relitigate)

- **Goals, steps, and feedback are user-level shared state.** They live in a new shared SQLite database (location TBD in design phase — likely `data/user.db` or similar). Every persona can read them. The dashboard writes to them.
- **Primary persona is Jo.** Jo handles the dashboard's chat threads and produces suggestions during planning cycles. Other personas can still see goals and steps in their context, but Jo is the default.
- **Steps are normal Purcival chats.** Clicking a step opens a focused chat thread scoped to that step. The chat uses the existing brain / persona / memory infrastructure — summarization, semantic retrieval, persona prompt, user context, all of it applies unchanged.
- **No AI-proposed goals in v1.** Goals are user-created. Purcival proposing goals from observed conversation patterns is a future feature.
- **No web search in v1.** Suggestions are generated from the agent's existing reasoning over Purcival's existing knowledge. The "look up Yoga6's schedule and propose a class" use case is Stage 6+ work.
- **No recurring steps in v1.** Every step is one-shot. A recurring goal like "stay active" produces fresh steps each planning cycle, not a single recurring instance.
- **Stack: FastAPI + Jinja2 + HTMX + vanilla CSS + Playwright.** Server-rendered, no JS build step. Streaming via Server-Sent Events.
- **Visual identity: cyberpunk dark theme.** Orange and purple accents, mono font for code-like elements, subtle accent glow on interactive surfaces. CSS custom properties from the outset so the theme is one place.

### Architectural question for the design phase

The mockup shows focused chat panels — each step gets its own chat thread, isolated from other step chats and from Jo's default conversation. Purcival's existing `messages` table assumes one chat per persona. **The design doc must propose a concrete answer to this.** The leading direction:

- Add a `scope` column (or polymorphic `entity_type` + `entity_id`) to `messages`. `null` = default scope (Jo's existing chat). `step:N` = scoped to a step. `goal:N` = scoped to a goal.
- Summarization runs per-scope. A long step thread summarizes independently of Jo's default thread.
- Semantic retrieval primarily searches within the active scope, with a smaller weighted pull from the default scope as background context. Exact retrieval policy is Codex's call to propose.

If the design doc lands on something different, that's fine — but it has to *engage* with the scoping question, not skip past it.

### Phases

Each phase ships independently testable behavior. Don't merge phases.

**Phase 0 — Design**

Read the codebase (especially `agent.py`, `brain.py`, `memory.py`, the tool registry, and the existing persona/message infrastructure). Produce `Design/dashboard_goals_design.md` covering:

- Data models for `goals`, `steps`, `step_feedback`, and the message-scoping mechanism
- Database layout: where is the new shared DB, how does it relate to per-persona memory DBs, how are cross-DB references handled (likely application-level, no FK constraints)
- How `GoalTool` and `SuggestionTool` register with the tool registry; what their methods are; what tier each is
- Reasoning-prompt changes for planning cycles (new tools, current goals, recent acceptance/rejection signal)
- Reasoning-prompt changes for non-planning cycles (active steps surfaced for accountability)
- UI architecture: routes, templates, HTMX patterns, SSE wiring for streaming chat
- Chat-on-step context flow: when a step thread is empty, what context does the reasoner assemble? When the thread exists, how is it loaded?
- Resolution of the scoping question above
- Test strategy across unit, integration, e2e

No production code. Acceptance: design doc reviewed and approved by Zach before Phase 1 starts.

**Phase 1 — Data layer**

Schema additions: `goals`, `steps`, `step_feedback`, plus the scoping column on `messages` per the design doc. Migration that's reversible. PersonaMemory or new shared-state helpers for CRUD. Seed-data utility (`scripts/seed_dev_data.py`) that loads the mockup's example goals — "Learn more about AI safety" (career), "Stay active & healthy" (health), "Be a good husband and father" (home), "Make some extra money" (money) — and a couple of seed steps for development. Tests for every CRUD path. No UI yet.

Acceptance: tests pass, seed data loads cleanly into a dev database, scoping column doesn't break existing tests.

**Phase 2 — Dashboard skeleton**

FastAPI app in `dashboard/`. Cyberpunk theme via CSS custom properties. Layout per the mockup: goals strip at top, suggestions strip in the middle, collapsible chat panel on the right. No interactivity yet — all data loaded from the seed. Playwright screenshot script and at least one screenshot committed.

Acceptance: Zach reviews the screenshot and approves the visual identity.

**Phase 3 — Goal and step display, accept/reject**

Real data renders from the database. Category tags display on goals, while step cards stay minimal. Steps in `status='suggested'` show ✓ and ✗ buttons. ✓ transitions to `accepted`. ✗ transitions to `rejected` without asking for a reason. Accepted steps display alongside open suggestions, visually distinct but without extra status metadata. HTMX endpoints handle state transitions; full page never reloads.

Acceptance: end-to-end Playwright test of the accept/reject flow. Screenshot updated.

**Phase 4 — Chat-on-step and chat-on-goal**

Clicking a goal or step opens the chat panel on the right, scoped to that entity. Backend: if no thread exists, the reasoner assembles initial context per the design doc; if a thread exists, load messages. SSE streaming for response. Messages persist with the entity scope. Uses the existing brain/persona infrastructure — no parallel chat machinery, no shortcuts.

Acceptance met: Playwright test sends a message, verifies SSE response delivery, verifies reload persistence, and verifies scoped messages do not leak into Jo's default chat.

**Phase 5 — Agent loop integration: suggestion generation**

Register `GoalTool` (observe-tier; surfaces active goals via `get_context()`) and `SuggestionTool` (observe-tier methods: `propose_suggestion`, `list_suggestions`, `update_status`). Planning-cycle prompt updated to include current goals and recently-accepted/rejected suggestions, with explicit instruction to propose 1–3 new candidate suggestions tied to specific goals each planning cycle. Suggestions land in `steps` with `status='suggested'` and `source='agent_planning'`.

Acceptance: a real planning cycle produces sensible suggestions visible on the dashboard within seconds.

**Phase 6 — Accountability**

Accepted steps (`status='accepted'`) surface in the agent's reasoning context for *every* cycle, not just planning. The agent can reference them in regular conversation ("how'd that yoga class go?"). Steps can be marked `completed` or `abandoned` from the UI (button on the card) or from chat (the agent infers from conversation and calls `SuggestionTool.update_status` after explicit confirmation — never silently). Stale steps (accepted but no progress for N days) get surfaced more prominently in the planning prompt as reminder candidates.

Acceptance: end-to-end test of "accept a step, ignore it, see it referenced in next chat, mark it done."

**Phase 7 — Feedback loop polish**

Aggregated feedback (acceptance and rejection rates per category/pattern) gets summarized into the planning prompt: e.g., "Zach has accepted N of M suggestions tagged Health and rejected most Money suggestions." Text summarization at this stage — not a fine-tuned scorer.

Acceptance: subjective — Zach reviews the next week's suggestions and confirms they feel better-tuned.

---

## Open questions for Zach                               *updatable*

(None pending for the goals dashboard. The architectural scoping question
above is for Codex's design phase, not for Zach — Codex proposes a concrete
answer in the design doc, Zach reviews.)

---

## Decisions awaiting Zach's approval                    *updatable*

(None pending. Phase 4 is complete; Phase 5 is ready for the next development cycle.)

When you stop at a gate, append an entry with:
- The phase / context
- The decision being requested
- Your proposed direction with rationale
- Path to a design doc if relevant

---

## Recent activity                                       *updatable*

Most recent first. Format:
`YYYY-MM-DD — task — what was done — commit shortref`.

- 2026-05-17 — Goals dashboard chat composer fix — fixed keyboard activation so Space/Enter inside the chat textarea no longer reloads the scoped panel, and added Playwright regression coverage for messages with spaces — pending.
- 2026-05-17 — Goals dashboard Phase 4 — implemented scoped goal/step chat panel loading, message persistence, SSE response delivery via `brain.stream()`, scoped context assembly, and Playwright coverage for streaming, reload persistence, and default-chat isolation — committed.
- 2026-05-17 — Goals dashboard Phase 3 UI correction — removed thumbs feedback, rejection reasons, step-section metadata, and title/subtitle step cards; accept/reject status is now the feedback signal — committed.
- 2026-05-17 — Goals dashboard Phase 3 — implemented database-backed suggested/accepted step rendering, accept/reject and feedback endpoints, rejection reasons, thumbs feedback, refreshed Phase 3 screenshots, and full pytest passing — committed.
- 2026-05-17 — Goals dashboard Phase 2 completion check — confirmed local dashboard loads, desktop/mobile screenshots are committed, Zach approved the visual identity, full pytest passes, and Phase 3 is ready for the next development cycle — committed.
- 2026-05-17 — Goals dashboard Phase 2 compact rail polish — merged the title and goals rail, made goals smaller than steps, removed step details/counts from goal cards, reduced step cards to concise prompts, refreshed screenshots, and kept dashboard tests passing — caf6fd9.
- 2026-05-17 — Goals dashboard Phase 2 chat-first polish — changed the motivational title to once-per-day selection, removed visible seed-source tags and manual step buttons, enlarged the Jo chat workspace, moved steps into a secondary context rail, refreshed screenshots, and kept dashboard tests passing — 5cad0b9.
- 2026-05-17 — Goals dashboard Phase 2 visual polish — reduced the header title size, added rotating motivational title phrases, assigned stable category colors, replaced goal suggested/accepted counts with steps-in-progress counts, refreshed screenshots, and kept full pytest passing — e7d3288.
- 2026-05-17 — Goals dashboard Phase 2 — implemented the FastAPI/Jinja dashboard skeleton, cyberpunk CSS theme, static Jo chat panel, dashboard route tests, and desktop/mobile Playwright screenshots — e8fd5fe.
- 2026-05-17 — Test baseline cleanup — marked live OpenAI / Google / Ollama tests as opt-in, updated stale proactive and summarizer tests, and restored full pytest to passing — committed.
- 2026-05-17 — Goals dashboard Phase 1 — implemented shared `data/user.db` goal storage, scoped persona messages/summaries, scoped summarization/context hooks, idempotent mockup seed data, and focused data-layer tests — committed.
- 2026-05-17 — Documentation consistency — renamed the agent loop design doc, rewrote stale README and overall design handoff for Windows / Jo-only / inactive Telegram context, and removed active numbered-agent terminology — uncommitted.
- 2026-05-17 — Goals dashboard Phase 0 — drafted `Design/dashboard_goals_design.md` covering shared goal schema, scoped chat architecture, tool integration, dashboard UI routes, SSE chat flow, and test strategy — uncommitted.
- 2026-05-17 — Project docs — normalized operating docs to `AGENTS.md`, moved design-doc references to `Design/`, and logged Telegram `/status` provider-model drift for later fix — 241f844.
- 2026-05-16 — Goals dashboard project — scoped, phased, constraints locked. New active project. Phase 0 awaiting first session.
- 2026-05-16 — ChatGPT integration — Implemented: brain.py (task dispatch + chatgpt provider + ollama fallback), config.py (per-task models for all 3 providers), agent.py/summarizer.py (task= param), main.py (/chatgpt switch), tests/test_brain_chatgpt.py (16/16 pass). max_completion_tokens fix applied from live API feedback.

---

## Decisions log                                       *append-only*

Format: `YYYY-MM-DD — title — rationale`.

Append entries; never edit prior ones.

- **2026-05-16 — OpenAI integration scoped to three call sites: chat, summary, reasoning.** Zach's brief. Embeddings remain on Ollama local. o-series reasoning models out of scope for v1.
- **2026-05-16 — Per-task model configuration via env vars.** Each call site independently configurable. Provider mixing across call sites is a possible future feature, not a v1 requirement.
- **2026-05-16 — Starting model assignments: reasoning → gpt-5.5, chat → gpt-5.4-mini, summary → gpt-5.4-nano.** Chosen for cost/capability tiering across call sites. Swappable via env.
- **2026-05-16 — Chat Completions API chosen over Responses API.** Structurally parallel to existing Ollama integration; Purcival manages its own session state; Responses API benefits don't apply here.
- **2026-05-16 — Single provider lever + task parameter design.** `DEFAULT_PROVIDER` controls all call sites. `brain.ask()` gains a `task` parameter (`"chat"`, `"summary"`, `"reasoning"`) that selects the right model within the provider family. `AGENT_REASONING_PROVIDER` and `SUMMARIZE_PROVIDER` module constants removed — call sites pass `task=` instead.
- **2026-05-16 — Per-task models for all three providers.** `CLAUDE_MODEL` and `OLLAMA_MODEL` replaced by per-task model vars for each family. Defaults: Claude uses Sonnet for chat, Haiku for summary, Opus for reasoning; Ollama defaults all tasks to phi4; ChatGPT uses gpt-5.4-mini / gpt-5.4-nano / gpt-5.5.
- **2026-05-16 — Provider name "chatgpt" not "openai".** Consistent with "claude" (product name, not company). API key env var stays `OPENAI_API_KEY` (industry standard); provider name in code/CLI is `"chatgpt"`.
- **2026-05-16 — GPT-5 models use max_completion_tokens, not max_tokens.** Confirmed via live API: gpt-5.4-mini, gpt-5.4-nano, gpt-5.5 reject `max_tokens` with a 400 error. `_ask_chatgpt` uses `max_completion_tokens` instead. The `max_tokens` parameter to `brain.ask()` is still the public interface name.
- **2026-05-16 — Goals dashboard: goals/steps are user-level shared state.** They live in a shared SQLite DB, accessible to every persona. Each persona's conversation memory remains per-persona. Cross-DB references are application-enforced (no FK constraints across SQLite files).
- **2026-05-16 — Terminology: "step", not "task".** Each step has an id and belongs to exactly one goal. Goals belong to one category. Three-level hierarchy: category → goal → step.
- **2026-05-16 — Primary persona for the dashboard is Jo.** Other personas can read goals and steps but Jo is the default for chat-on-step and suggestion generation.
- **2026-05-16 — Step chats are scope-tagged within Purcival's existing message infrastructure.** The leading proposal for Phase 0: add a `scope` (or `entity_type` + `entity_id`) column to `messages`; null = default chat, `step:N` = step-scoped. Summarization and retrieval honor scope. Design phase confirms exact shape.
- **2026-05-17 — Design documents live under `Design/`.** Zach clarified that all design documents should be referenced from the `Design/` directory. Operational instructions live in `AGENTS.md`.
- **2026-05-17 — Agent loop design doc renamed.** The old numbered agent design doc became `Design/agent_loop_design.md` to avoid confusing the completed agent architecture with future numbered Goals dashboard phases.
- **2026-05-17 — Goals dashboard Phase 1 began from the approved Phase 0 design.** Zach explicitly asked to begin Phase 1 after reviewing the dashboard design and mockup direction. Implementation follows the approved `data/user.db` + typed message scope design.
- **2026-05-17 — Live integration tests are opt-in during dashboard design.** OpenAI, Google Calendar, and live Ollama summarization tests are skipped by default under pytest. Run them with `PURCIVAL_RUN_LIVE_TESTS=1` plus the relevant API keys, credentials, or local services. This keeps dashboard data/UI work from being blocked by secondary integrations.
- **2026-05-17 — Goals dashboard is chat-first.** Zach clarified that chatting with Jo is the main way goals, suggestions, and steps should change. Dashboard goal/step controls should stay limited; manual editing is not the product center.
- **2026-05-17 — Goals dashboard feedback is accept/reject only.** Zach clarified that ✓ means the suggestion was good enough to accept, ✕ means it was bad enough to reject, and Jo should infer why from context rather than asking for thumbs or rejection reasons.

---

## Backlog                                               *updatable*

### Known issues

- **Trigger-deletion bug.** Some agent-scheduled triggers have been found deleted between cycles with no corresponding cancel commands in the reasoning log. Root cause unknown. Investigation, not a quick patch.
- **Telegram `/status` model display drift.** `telegram_bot.py` still references removed `config.CLAUDE_MODEL` / `config.OLLAMA_MODEL` names after the per-task provider refactor. Fix later by routing status display through the same task-model lookup used by the CLI.

### Deferred work (related to current and recent projects)

- **OpenAI o-series reasoning models** (o3, o4-mini). Requires handling reasoning tokens, different system-prompt semantics, possibly the Responses API. Path documented in `Design/openai_integration_design.md`.
- **Mixing providers across call sites** (e.g., Anthropic for reasoning, OpenAI for chat). The handler/model tables in `brain.py` already support it; just needs per-call-site overrides in config.
- **Web search tool** for the agent — would unlock proactive suggestions like "Yoga6 has a 6pm class today, want me to put it on your calendar?" Needs its own design phase: generic URL fetch + extraction vs. search engine API vs. browser automation; rate limiting; caching; cost; trust boundary for arbitrary URL fetching.
- **AI-proposed goals.** Right now goals are user-created. Purcival proposing goals from observed conversation patterns is a future feature; moves the trust boundary, defer.
- **Recurring steps.** Currently all steps are one-shot. Recurring goals produce fresh steps per planning cycle.

### Other deferred work

- Restructure tests into a formal `tests/` directory
- Memory architecture evolution beyond Stage 3 (lossy summaries → something closer to a "living memory" graph; see Zach's prior exploration of Graphiti and faceted entities)

---

## Reference docs (read these for context)

- `AGENTS.md` — operating manual
- `Design/PURCIVAL_DESIGN_DOC.md` — overall system design (living)
- `Design/agent_loop_design.md` — agent loop architecture
- `Design/openai_integration_design.md` — OpenAI integration design (completed)
- `Design/dashboard_goals_design.md` — Goals dashboard design
- `README.md` — project overview and setup

If any of these go stale because of changes you make, update them in the same session.
