# PROGRESS.md — Purcival

This file is the rolling state of Purcival core development across
agent sessions. Read it in full each session. Update sections marked
*updatable*; leave *STABLE* sections alone.

---

## Current focus                                          *updatable*

**Last updated:** 2026-05-19
**Active task:** Core agent reliability redesign
**Phase:** Phase F - structured working memory and reflection first slice implemented
**Status:** The Goals dashboard task is closed as the primary development focus. Do not continue dashboard Phase 6 as previously scoped. Zach reopened and approved a narrow dashboard category-filter UI adjunct on 2026-05-19; category bubbles, category/goal filter state, filtered steps/inbox cards, and reset behavior are now implemented. The active redesign plan remains `Design/core_agent_reliability_redesign.md`: migrate the agent loop toward an event log, explicit job types, an opportunity queue, planner/policy/compiler separation, durable execution state, a dashboard delivery inbox, scoped capabilities, and an untrusted-content boundary before adding web/file/computer tools. Phase A safety/instrumentation, Phase B event/jobs, Phase C opportunities, Phase D accountability receipts, and Phase E inbox/mobile access are all in place. Phase F's first narrow slice is now implemented: per-persona typed `memory_items`, validators and confidence/status lifecycle rules, `conversation_message` events, auto-scheduled `reflection` jobs, deterministic event-to-memory processing for step outcomes/inbox feedback/user corrections, and structured-memory inclusion in chat/reasoning context. The remaining open question is the future correction/activity UX for autonomous memory writes. Do not jump ahead to planner/compiler, capability registry, web tools, file tools, public hosting, or computer-control work without explicitly deciding whether any more Phase F correction surfacing is needed first.

---

## Active task — Core agent reliability redesign

### What this is

Purcival is moving from "AI chatbot with tools and memory" toward a trusted
self-hosted assistant that learns from conversation, maintains durable goals
and commitments, proactively identifies useful opportunities, schedules its
own work, and safely uses tools such as calendar, Gmail, web search, and local
file search.

The canonical design doc for this next chapter is:

- `Design/core_agent_reliability_redesign.md`

The core thesis:

- The current one-shot prompt/action loop is too fused: one model response can
  reason, schedule, mutate state, and act.
- The next architecture needs durable events, explicit jobs, opportunities,
  policy gates, action compilation, execution receipts, and dashboard-visible
  correction paths.
- Internal goal/step/memory/dashboard state is trusted and autonomous.
  External actions and broad computer capabilities remain separate trust
  boundaries.

### Immediate handoff instructions

1. Read `Design/core_agent_reliability_redesign.md` in full.
2. Review the Phase A implementation notes and keep the safety/instrumentation
   work scoped: dashboard hidden controls are suppressed, and trigger/schedule
   instrumentation remains logging-only around the old scheduler path.
3. Review the Phase B implementation notes: this slice keeps triggers as the
   low-level scheduler, layers `agent_jobs` over them, and writes tool
   observations to `agent_events` before reasoning.
4. Review the Phase C implementation notes: this slice keeps opportunities in
   per-persona memory, records goal-step opportunities before delivering
   suggested steps, and routes legacy direct suggestion actions through the new
   opportunity path during planning.
5. Review the Phase D implementation notes: this slice keeps accountability
   receipts in persona events, creates scored accountability opportunities for
   accepted steps, and keeps the chat internal-action path narrow until the
   Phase E delivery inbox/side channel exists.
6. Review the Phase E implementation notes: the dashboard now has auth, CSRF,
   startup runners, PowerShell Task Scheduler wrappers, and Zach-verified
   Tailscale mobile access.
7. Review section 6.5 of the design doc before starting the next phase; it
   maps section 5.1-5.10 features to implementation status.
8. Next execution target: review the implemented Phase F first slice with Zach,
   decide whether Phase F needs a dashboard-visible memory correction/activity
   surface before closure, and only then move on to Phase G planner/policy/compiler
   work.

### Do:

- Treat `Design/core_agent_reliability_redesign.md` as the active design doc.
- When finished, prepare the repo for a fresh Codex handoff.
- Keep the Goals dashboard as supporting UI/state, not the active project.

### Do not:

- Add web search or local file search before the capability and untrusted
  content model is implemented.
- Start public hosting, web/file tools, or computer-control tools before the
  structured memory, planner/policy/compiler, durable execution, scoped
  capability, and untrusted-content phases have landed.

---

## Completed project — Goals dashboard reference

This section is retained as historical/reference context for the dashboard
features that were already built. It is not the active project. The old
dashboard Phase 6 and Phase 7 plans are superseded by the core agent redesign
unless Zach explicitly reopens them.

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

Real data renders from the database. Category tags display on goals, and Phase 4.1 adds inherited category tags to step cards. Steps in `status='suggested'` show ✓ and ✗ buttons. ✓ transitions to `accepted`. ✗ transitions to `rejected` without asking for a reason. Accepted steps display alongside open suggestions, visually distinct but without extra status metadata. HTMX endpoints handle state transitions; full page never reloads.

Acceptance: end-to-end Playwright test of the accept/reject flow. Screenshot updated.

**Phase 4 — Chat-on-step and chat-on-goal**

Clicking a goal or step opens the chat panel on the right, scoped to that entity. Backend: if no thread exists, the reasoner assembles initial context per the design doc; if a thread exists, load messages. SSE streaming for response. Messages persist with the entity scope. Uses the existing brain/persona infrastructure — no parallel chat machinery, no shortcuts.

Acceptance met: Playwright test sends a message, verifies SSE response delivery, verifies reload persistence, and verifies scoped messages do not leak into Jo's default chat.

**Phase 5 — Agent loop integration: suggestion generation**

Register `GoalTool` (observe-tier; surfaces active goals via `get_context()`) and `SuggestionTool` (observe-tier methods: `propose_suggestion`, `list_suggestions`, `update_status`). Planning-cycle prompt updated to include current goals and recently-accepted/rejected suggestions, with explicit instruction to propose 1–3 new candidate suggestions tied to specific goals each planning cycle. Suggestions land in `steps` with `status='suggested'` and `source='agent_planning'`.

Acceptance: a real planning cycle produces sensible suggestions visible on the dashboard within seconds.

**Phase 5 adjunct TODOs - dashboard usability**

- Step cards now display their inherited goal category as a compact tag.
- Chat history now scrolls inside the focused chat panel and lazily loads older
  scoped messages when Zach scrolls upward.
- The scoped goal/step context now sits directly beside the bottom text input,
  roughly 25% context and 75% input row, to reclaim vertical chat space.
- Step category tags are display-only. Do not implement clickable
  step-category reassignment in v1.
- Do not add an independent step category field, categories table,
  many-to-many step/category join table, or step re-parenting flow unless Zach
  explicitly reopens richer taxonomy work later.

**Phase 6 — Accountability (superseded)**

This old plan is not active. Accountability should be rebuilt through the
event/job/opportunity architecture in `Design/core_agent_reliability_redesign.md`.
Do not implement the old approach of adding accepted steps to every prompt and
mutating status through hidden chat control tags.

Historical acceptance target was: end-to-end test of "accept a step, ignore it,
see it referenced in next chat, mark it done." The redesigned version should
meet that behavior through events, opportunities, receipts, and reversible
internal writes.

**Phase 7 — Feedback loop polish (superseded)**

This old plan is not active. Feedback learning should be handled by the
redesigned event and reflection path.

Historical acceptance target was subjective: Zach reviews the next week's
suggestions and confirms they feel better-tuned.

---

## Open questions for Zach                               *updatable*

- Phase I still needs a concrete correction UX for autonomous internal writes
  such as created goals, modified goals, completed steps, abandoned steps, and
  learned memories. This does not block Phase F's first typed-memory slice.

---

## Decisions awaiting Zach's approval                    *updatable*

- None currently.

When you stop at a gate, append an entry with:
- The phase / context
- The decision being requested
- Your proposed direction with rationale
- Path to a design doc if relevant

---

## Recent activity                                       *updatable*

Most recent first. Format:
`YYYY-MM-DD — task — what was done — commit shortref`.

- 2026-05-19 - Dashboard family category color - added an explicit teal family category accent so Family pills no longer fall back to Career orange, and browser-verified the rendered computed colors - committed.
- 2026-05-19 - Dashboard accepted-step visibility fix - rendered accepted steps before suggestions so accepting a filtered step remains visible, added regression coverage for accepting under a category filter, and kept full pytest passing - committed.
- 2026-05-19 - Dashboard category filter visual polish - matched category filter bubbles to the compact colored card-tag pill format, cache-busted the dashboard stylesheet, browser-verified computed styles against goal card tags, and kept full pytest passing - committed.
- 2026-05-19 - Dashboard category filter implementation - added top-strip category bubbles, query-backed category/goal filtering, filtered goal/step/inbox rendering, JS partial refresh/reset behavior, active visual states, dashboard/Playwright coverage, a seeded browser smoke check, and full pytest passing - committed.
- 2026-05-19 - Dashboard category filter design - drafted the category bubble/filter design in `Design/dashboard_goals_design.md`, including the `All` reset, category/goal filtering rules, inbox behavior, and test targets; no production code written pending Zach approval - committed.
- 2026-05-19 - Dashboard chat streaming scroll fix - preserved manual upward scroll position while scoped chat responses stream, added a browser-level regression for streaming while scrolled away from the bottom, and kept full pytest passing - uncommitted.
- 2026-05-18 - Core agent reliability Phase E.5 verification handoff - recorded Zach's successful Tailscale mobile verification, marked Phase F structured working memory/reflection as the next approved implementation target, refreshed `AGENTS.md`, `README.md`, `PROGRESS.md`, and `Design/core_agent_reliability_redesign.md` for a fresh Codex handoff - committed.
- 2026-05-18 - Core agent reliability Phase F first slice - added per-persona typed `memory_items`, validators and status transitions, `conversation_message` events, auto-scheduled reflection jobs, deterministic event-to-memory processing for step outcomes/inbox feedback/user corrections, structured-memory prompt context, and focused reflection/context/agent tests with full pytest passing - committed.
- 2026-05-18 - Core agent reliability architecture audit - mapped proposed architecture features 5.1 through 5.10 against the Phase A-E code, documented the partial/missing pieces in `Design/core_agent_reliability_redesign.md`, and revised the phase plan so structured memory/reflection, planner-policy-compiler separation, durable action execution, capability metadata, delivery correction UX, and the untrusted-content boundary land before web/file tools - committed.
- 2026-05-18 - Core agent reliability Phase E secure access implementation - added dashboard runtime config guards, PBKDF2 password hashing, signed session auth, CSRF on post-login mutations, verified actor attribution, dashboard and agent-loop runner scripts, Windows Task Scheduler PowerShell wrappers, README/.env updates, and restored full pytest to passing - committed.
- 2026-05-18 - Core agent reliability Phase E anti-impersonation design - tightened the mobile access design so Tailscale controls network reachability, Purcival signed sessions control Zach identity, tailnet admission uses device approval and least-privilege policy, and dashboard actor fields cannot be spoofed from requests - superseded by implementation and verification.
- 2026-05-18 - Core agent reliability Phase E security/access design - drafted the mobile access/security handoff: Tailscale Serve over loopback, dashboard-local auth and CSRF, safe bind modes, Task Scheduler startup, a non-Telegram agent-loop runner requirement, test plan, acceptance criteria, and rollback path - superseded by implementation and verification.
- 2026-05-18 - Core agent reliability Phase E - added per-persona `agent_inbox_items`, delivered opportunity-backed dashboard inbox cards for suggestions and stale accountability checks, wired card actions through existing receipts, added snooze/dismiss/open-chat handling, and kept focused inbox/dashboard tests passing - committed.
- 2026-05-18 - Core agent reliability Phase D - added event-backed accountability receipts for step status changes, scored `accountability_check` opportunities for accepted/stale steps, dashboard complete/abandon controls, focused-chat completion/abandonment receipts, and full pytest passing - committed.
- 2026-05-18 - Core agent reliability Phase C - added per-persona `agent_opportunities`, registered `OpportunityTool`, routed planning-cycle step suggestions through opportunity records, delivered low-risk opportunities as dashboard-visible suggested steps, added duplicate suppression, and kept focused Phase C tests passing - committed.
- 2026-05-18 - Core agent reliability Phase B - added per-persona `agent_events`, explicit `agent_jobs`, `job_type` trigger metadata, job leasing/retry/completion receipts, and durable tool observation events before reasoning; focused Phase B tests and full pytest pass - committed.
- 2026-05-17 - Core agent reliability Phase A - fixed dashboard SSE filtering so hidden `<schedule_updates>` control blocks cannot stream even across split chunks, added trigger/schedule mutation logging, documented Phase A implementation status, and verified the full pytest suite passes - committed.
- 2026-05-17 - Project focus transition - marked the Goals dashboard as closed as the primary development task, made `Design/core_agent_reliability_redesign.md` the active design doc, instructed future sessions not to separately fix the dashboard schedule-update streaming leak, and prepared for a Phase A design-freeze handoff - awaiting final design freeze.
- 2026-05-17 - Core agent reliability redesign - paused Goals dashboard Phase 6 for a design checkpoint and drafted `Design/core_agent_reliability_redesign.md`, then incorporated Zach's answers on internal autonomy, secure mobile access, broad future user-directory reads, overnight work, and trusted inferred memory; `pytest` and `python -m pytest` could not run because pytest is not installed in the active Python - awaiting revised architecture/receipt UX approval.
- 2026-05-17 - Goals dashboard step-category decision - recorded Zach's decision that step category tags are display-only inherited labels; no editable step categories, independent step category field, category table, join table, or step re-parenting flow in v1 - committed.
- 2026-05-17 - Goals dashboard step-category design - proposed treating editable step categories as moving a step to another active goal, preserving goal-owned categories and deferring independent step taxonomy - awaiting review.
- 2026-05-17 - Goals dashboard Phase 5 - added GoalTool and SuggestionTool, registered them with the agent loop, planning-gated suggestion generation, and verified a planning cycle can create dashboard-visible suggested steps - committed.
- 2026-05-17 - Goals dashboard bottom input polish - moved the scoped goal/step context directly beside the bottom text input at roughly 25/75 width so the chat history can show more messages - committed.
- 2026-05-17 - Goals dashboard usability polish - added inherited category tags to step cards, pinned the chat composer while history scrolls inside the panel, added scoped message pagination for lazy upward loading, and documented step-category editing as design-gated - committed.
- 2026-05-17 — Goals dashboard Markdown and streaming — added dependency-free Markdown rendering for scoped chat messages, provider-native `brain.stream()` handlers for ChatGPT, Claude, and Ollama with fallback, per-chunk SSE delivery, and regression coverage — committed.
- 2026-05-17 — Goals dashboard chat composer fix — fixed keyboard activation so Space/Enter inside the chat textarea no longer reloads the scoped panel, and added Playwright regression coverage for messages with spaces — committed.
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
- **2026-05-17 - Step category editing is design-gated.** Step cards may display the inherited goal category immediately, but changing a step's category or assigning multiple categories affects the current `category -> goal -> step` model and needs a design update before production code.
- **2026-05-17 - Step category tags are display-only in v1.** Zach rejected editable step categories as more trouble than they are worth. Each step belongs to one goal, each goal belongs to one category, and a step's category tag is inherited from its parent goal. Do not add independent step categories, category tables, join tables, or step re-parenting flows in v1.
- **2026-05-17 - Purcival may autonomously mutate internal goal and step state.** Zach clarified that the chat is the primary app interface and that Purcival should reason from conversation to create goals, modify goals, complete steps, or abandon steps without routine confirmation. This applies to trusted internal state; external actions remain a separate trust boundary.
- **2026-05-17 - Proactive dashboard cards and opportunities are trusted internal writes.** Purcival may create opportunities and dashboard cards on its own initiative. The design should favor evidence, receipts, and correction over approval prompts for these internal actions.
- **2026-05-17 - Mobile access path is implementation-flexible but must be secure.** Zach has no preference for Tailscale or another option as long as the selected path is secure. The implementation should justify the access model before exposing the dashboard beyond localhost.
- **2026-05-17 - Future file read scope may cover the user's main directory.** Purcival may eventually receive read access across `C:\Users\ztbli`, excluding system files and explicitly sensitive locations. Write access is separate and not implied.
- **2026-05-17 - Overnight work and inferred memory are allowed.** Purcival may do silent research, indexing, reflection, and planning overnight, and Zach is comfortable waking up to useful notifications. Purcival should infer important long-term memories from conversation without routine confirmation.
- **2026-05-17 - Core agent redesign supersedes Goals dashboard Phase 6 as primary work.** The Goals dashboard is no longer the active project. Do not continue the old Phase 6 accountability plan or separately fix the dashboard schedule-update streaming leak unless Zach reopens it; the redesign may replace that control path.
- **2026-05-17 - Phase A local leak fix suppresses old hidden schedule controls rather than applying them.** The dashboard now filters `<schedule_updates>` blocks before streaming or persisting focused chat responses. It intentionally does not execute those schedule actions from the dashboard; future state changes should use the redesigned event/action path with receipts.
- **2026-05-17 - Phase A trigger instrumentation is logging, not the event log.** Schedule config changes, trigger mutations, planning-cycle reschedules, bulk trigger clears, and `ScheduleTool` mutations now write explicit logs for investigation. The durable append-only event substrate remains Phase B work.
- **2026-05-18 - Phase B keeps jobs in per-persona memory DB first.** Triggers, reasoning logs, narrative state, and action logs already live in each persona's `memory.db`, so the first event/job substrate is colocated there. A shared/user-level event layer can be added later if cross-persona planning needs it.
- **2026-05-18 - Explicit jobs layer over triggers before replacing them.** Phase B preserves the existing scheduler and trigger table, adds `agent_jobs` for job type, lease, retry, and completion state, and writes `job_type` into new trigger contexts. Legacy JSON triggers still work through fallback inference.
- **2026-05-18 - Phase C keeps opportunities per-persona for the first slice.** Opportunities are colocated with agent events, jobs, reasoning logs, and narrative state in each persona's `memory.db`. Delivered goal-step opportunities create shared dashboard steps in `data/user.db` and retain the delivered `step_id` as the bridge.
- **2026-05-18 - Planning suggestions route through opportunities.** The planning prompt now asks for `opportunities.propose_goal_step`, and the agent loop routes legacy planning-cycle `suggestions.propose_suggestion` actions through the opportunity tool when available. This keeps old model behavior compatible while enforcing the new observation-to-opportunity-to-suggestion path.
- **2026-05-18 - Phase D step accountability uses shared receipts.** Dashboard step controls, focused-chat internal actions, and `SuggestionTool` status updates now route through one receipt helper that writes `step_*` events with previous status, message/evidence links, and undo-status metadata. Accepted steps create scored `accountability_check` opportunities, and completed/abandoned steps advance linked opportunities.
- **2026-05-18 - Focused-chat internal actions are a narrow transitional bridge.** The dashboard may suppress and apply `<internal_actions>` only for completing or abandoning the active step, with active-scope validation and visible receipts. This should not be expanded to broad tool use; Phase E should replace it with a proper delivery/action side channel or inbox.
- **2026-05-18 - Phase E is split into local delivery first, secure access second.** The dashboard inbox can be implemented safely on localhost because it only acts on trusted internal state through existing receipts. Authentication, bind-address changes, Windows service setup, and Tailscale/mobile access remain a separate security slice and should not be slipped in casually.
- **2026-05-18 - Phase E dashboard auth uses PBKDF2 plus signed sessions.** The secure-access slice uses a standard-library PBKDF2-HMAC-SHA256 password hash, one HMAC-signed cookie session, and CSRF tokens derived from the session nonce. This keeps the one-user local dashboard dependency-light while still enforcing a real auth boundary before any non-local exposure.
- **2026-05-18 - Phase E Windows startup uses dedicated runners plus PowerShell wrappers.** `scripts/run_dashboard.py` and `scripts/run_agent_loop.py` are the supported Python entrypoints, and `scripts/start_dashboard.ps1` / `scripts/start_agent_loop.ps1` are the Windows Task Scheduler targets so startup behavior stays explicit, repo-local, and easy to audit without introducing a service-wrapper dependency.
- **2026-05-18 - Section 5.1-5.10 audit gates web/file tools behind missing core architecture.** Phases A-E produced a useful event/job/opportunity/inbox substrate, but did not complete structured working memory, planner/policy/compiler separation, per-action execution state, scoped capability metadata, the untrusted-content boundary, or the learning loop. The revised plan makes those Phase F-I prerequisites before Phase J web/file tools and Phase K higher-autonomy external actions.
- **2026-05-18 - Phase E.5 mobile verification is accepted and Phase F is approved.** Zach verified the private mobile dashboard setup through Tailscale. The next Codex chat should begin Phase F implementation for typed working memory and reflection, while keeping web/file tools and broader planner/compiler/capability work out of scope until their later phases.
- **2026-05-18 - Phase F reflection is deterministic in the first slice.** The first typed-memory implementation processes unprocessed `agent_events` into memory records with code, not another LLM prompt, so memory writes stay evidence-backed, idempotent, and easy to test before later planner/learning work broadens the loop.
- **2026-05-18 - Phase F typed memory uses per-persona records with 1-5 confidence and active kind/subject uniqueness.** `memory_items` now lives beside events/jobs/opportunities in each persona's `memory.db`; active memories merge or supersede by `(kind, subject)` while preserving history through status transitions instead of in-place deletion.

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
- **Richer goal/step taxonomy.** Deferred unless Zach explicitly reopens it. For v1, each step belongs to one goal, each goal belongs to one category, and step category tags are display-only inherited labels.

### Other deferred work

- Restructure tests into a formal `tests/` directory
- Memory architecture evolution beyond Stage 3 (lossy summaries → something closer to a "living memory" graph; see Zach's prior exploration of Graphiti and faceted entities)

---

## Reference docs (read these for context)

- `AGENTS.md` — operating manual
- `Design/PURCIVAL_DESIGN_DOC.md` — overall system design (living)
- `Design/core_agent_reliability_redesign.md` — active redesign doc for the next development chapter
- `Design/agent_loop_design.md` — agent loop architecture
- `Design/openai_integration_design.md` — OpenAI integration design (completed)
- `Design/dashboard_goals_design.md` — Goals dashboard design
- `README.md` — project overview and setup

If any of these go stale because of changes you make, update them in the same session.
