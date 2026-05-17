# Goals Dashboard Design

**Status:** Phase 4 complete; Phase 5 ready for next development cycle
**Date:** 2026-05-17
**Phase:** 4 - Chat-on-step and chat-on-goal
**Scope:** shared goal state, scoped conversations, dashboard UI, and agent integration

---

## 1. Purpose

The Goals dashboard gives Purcival a local web interface for tracking Zach's goals, proposing concrete next steps, and opening focused conversations about a goal or step.

The dashboard should not become a parallel assistant stack. It should be a new interface over the existing Purcival brain, persona, memory, summarization, retrieval, and tool loop.

The v1 frame is:

- Zach creates goals.
- Jo proposes one-shot steps toward those goals during planning cycles.
- Zach accepts, rejects, completes, or abandons steps.
- Clicking a goal or step opens a scoped Jo chat about that entity.
- Accepted steps become accountability context for future agent cycles and conversations.

Out of scope for v1:

- AI-proposed goals.
- Web search.
- Recurring steps.
- Direct external execution beyond the existing tool tiers.

---

## 2. Existing System Constraints

Purcival currently has strong boundaries that the dashboard should preserve:

- The active assistant persona is Jo, whose SQLite database lives at `data/jo/memory.db`.
- The code still supports multiple personas, but the dashboard design targets Jo only.
- Conversation history lives in Jo's `messages` table.
- Summaries live in Jo's `summaries` table and are retrieved by embedding similarity.
- Shared user context is file-based in `data/user_context.md`.
- The agent loop discovers tools from `tools.create_tools()` and interacts with them only through `get_context()`, `get_methods()`, and `execute()`.
- `brain.ask()` is the LLM gateway and already supports per-task model routing with `task="chat"`, `task="summary"`, and `task="reasoning"`.

The dashboard introduces one new category of state: user-level goals, steps, and sparse future feedback. That state should be stored outside Jo's persona database so it can remain user-owned if more personas become active again later. In the v1 UI, accept/reject status is the suggestion feedback signal; thumbs and rejection-reason controls are intentionally not shown.

---

## 3. Proposed Architecture

High-level layout:

```text
dashboard/ FastAPI app
  |
  | reads/writes
  v
data/user.db
  goals
  steps
  step_feedback

data/jo/memory.db
  messages(scope_type, scope_id)
  summaries(scope_type, scope_id)
  triggers
  tool_state
  agent state

agent loop
  |
  | create_tools(...)
  v
GoalTool + SuggestionTool
  |
  | read/write
  v
data/user.db
```

Core choices:

- Use `data/user.db` for shared user-level goal state.
- Keep all dashboard chat messages in Jo's existing memory database.
- Add explicit scope columns to `messages` and `summaries` rather than creating separate chat tables.
- Dashboard persona is Jo.
- Add goal tools to the existing tool registry rather than creating dashboard-specific agent logic.

---

## 4. Shared Database Layout

Create a new SQLite database:

```text
data/user.db
```

This database is user-level shared state. It is not owned by a persona.

Recommended helper:

```python
class SharedGoalStore:
    def __init__(self, db_path: Path | None = None): ...
```

This should live in a new module such as `goals.py` or `shared_state.py`. I prefer `goals.py` because this database's first responsibility is goal state, not a generic dumping ground.

Cross-database references are application-enforced:

- `steps.goal_id` can use a normal FK inside `data/user.db`.
- `messages.scope_id` in `data/jo/memory.db` points at `data/user.db` rows by convention only.
- SQLite cannot enforce FKs across separate database files in the way we want here.
- The application must validate scope targets before opening or writing scoped messages.

This avoids contaminating persona memory with shared user data while still letting persona chats attach to shared entities.

---

## 5. Data Models

### goals

```sql
CREATE TABLE goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    priority    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'user',
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL,
    archived_at TIMESTAMP,

    CHECK (status IN ('active', 'paused', 'completed', 'abandoned', 'archived')),
    CHECK (source IN ('user', 'import', 'agent'))
);

CREATE INDEX idx_goals_status_category
    ON goals(status, category);
```

Notes:

- `source='agent'` is included for forward compatibility, but v1 UI and tools should not create agent-proposed goals.
- `category` is a simple string in v1, not a separate categories table. A table is unnecessary until category metadata exists.
- `priority` gives us cheap ordering without needing a full rank table.

### steps

```sql
CREATE TABLE steps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id            INTEGER NOT NULL,
    title              TEXT NOT NULL,
    description        TEXT,
    rationale          TEXT,
    status             TEXT NOT NULL DEFAULT 'suggested',
    source             TEXT NOT NULL DEFAULT 'user',
    created_by_persona TEXT,
    due_at             TIMESTAMP,
    accepted_at        TIMESTAMP,
    rejected_at        TIMESTAMP,
    completed_at       TIMESTAMP,
    abandoned_at       TIMESTAMP,
    last_touched_at    TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL,

    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE,
    CHECK (status IN ('suggested', 'accepted', 'rejected', 'completed', 'abandoned')),
    CHECK (source IN ('user', 'agent_planning', 'dashboard_seed'))
);

CREATE INDEX idx_steps_goal_status
    ON steps(goal_id, status);

CREATE INDEX idx_steps_status_updated
    ON steps(status, updated_at);
```

Status semantics:

- `suggested`: candidate step Zach has not accepted or rejected.
- `accepted`: Zach has committed to doing it.
- `rejected`: Zach rejected the suggestion.
- `completed`: Zach did it.
- `abandoned`: Zach accepted it but later decided not to do it.

The word "step" should be used everywhere in code, routes, templates, and prompts. Do not introduce "task" as an alias.

### step_feedback

```sql
CREATE TABLE step_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    value       TEXT,
    created_at  TIMESTAMP NOT NULL,

    FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE,
    CHECK (kind IN (
        'completion_note',
        'abandon_reason',
        'freeform_note'
    ))
);

CREATE INDEX idx_step_feedback_step_created
    ON step_feedback(step_id, created_at);
```

Use `kind` + `value` rather than many nullable columns. Feedback is sparse and likely to grow.

Examples:

```text
kind='completion_note', value='Went to the 6pm class.'
kind='abandon_reason', value='No longer relevant this week.'
kind='freeform_note', value='Useful context from chat.'
```

---

## 6. Message Scoping Decision

### Decision

Add typed scope columns to `messages` and `summaries`:

```sql
ALTER TABLE messages
    ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'default';

ALTER TABLE messages
    ADD COLUMN scope_id INTEGER;

CREATE INDEX idx_messages_scope_id
    ON messages(scope_type, scope_id, id);

ALTER TABLE summaries
    ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'default';

ALTER TABLE summaries
    ADD COLUMN scope_id INTEGER;

CREATE INDEX idx_summaries_scope_range
    ON summaries(scope_type, scope_id, message_start, message_end);
```

Scope values:

```text
scope_type='default', scope_id=NULL
scope_type='goal',    scope_id=<goals.id>
scope_type='step',    scope_id=<steps.id>
```

Application rule:

```text
default scope must have scope_id NULL
goal and step scopes must have scope_id NOT NULL
```

SQLite cannot add all desired constraints via simple reversible migration on existing tables, so enforce this in `PersonaMemory` methods and tests.

### Why typed columns instead of `scope='step:123'`

A single string column would be easy, but typed columns are better here:

- Indexes remain straightforward.
- Filtering is explicit and less error-prone.
- `scope_id` remains an integer.
- Future scopes, if needed, do not require string parsing.
- Tests can assert scope rules without coupling to a string encoding.

If the UI or logs need a compact display label, generate it in Python:

```python
scope_label = "default" if scope_type == "default" else f"{scope_type}:{scope_id}"
```

### Persona boundary

Scoped messages live in Jo's persona database. A step chat with Jo lives in `data/jo/memory.db`.

If a future dashboard reintroduces multiple active personas, another persona could use the same `scope_type='step'` and `scope_id` in its own database with independent history. That is not v1.

---

## 7. PersonaMemory Changes

Introduce a small value object:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MessageScope:
    scope_type: str = "default"
    scope_id: int | None = None

    @classmethod
    def default(cls) -> "MessageScope": ...
    @classmethod
    def goal(cls, goal_id: int) -> "MessageScope": ...
    @classmethod
    def step(cls, step_id: int) -> "MessageScope": ...
```

Update existing methods to default to the default scope:

```python
add_message(role, content, scope=MessageScope.default()) -> int
get_recent_messages(limit=20, scope=MessageScope.default()) -> list[dict]
get_messages_since(after_id, scope=MessageScope.default()) -> list[dict]
get_message_count(scope=MessageScope.default() | None = None) -> int
get_last_summarized_id(scope=MessageScope.default()) -> int
get_unsummarized_messages(scope=MessageScope.default()) -> list[dict]
add_summary(..., scope=MessageScope.default()) -> int
search_summaries(..., scope=MessageScope.default(), include_default=False) -> list[dict]
```

Important migration rule:

- Existing messages and summaries become `scope_type='default', scope_id=NULL`.
- Existing callers do not pass a scope and should behave exactly as before.
- New scoped callers must pass a scope explicitly.

### Summarization per scope

Summarization must be scope-aware. Otherwise a step thread could cause Jo's default chat summarization cursor to skip messages, or vice versa.

Change:

```python
get_last_summarized_id()
```

to:

```python
get_last_summarized_id(scope: MessageScope = MessageScope.default())
```

and ensure it calculates `MAX(message_end)` only for summaries in that same scope.

`check_and_summarize(memory, scope=MessageScope.default())` should summarize only that scope. The existing CLI path uses the default scope. The Telegram path should also use the default scope if it is reactivated. Dashboard chat uses the active goal or step scope.

---

## 8. Scoped Context Assembly

Update `context.assemble_context()` to accept optional scope and entity context:

```python
def assemble_context(
    persona_prompt: str,
    memory: PersonaMemory,
    device: str = DEVICE_TERMINAL,
    scope: MessageScope = MessageScope.default(),
    entity_context: str | None = None,
) -> tuple[str, list[dict]]:
    ...
```

The system prompt gains a new optional section:

```text
## ACTIVE DASHBOARD CONTEXT

You are chatting with Zach about this step:
Goal: Stay active & healthy
Step: Try one Yoga6 class this week
Status: accepted
Rationale: ...
Recent feedback: ...
Sibling steps: ...
```

### Retrieval policy

For default chats:

- Same as today.
- Search default summaries.
- Include default recent messages.

For goal or step chats:

- Recent messages: only the active scope.
- Scope summaries: search only the active scope.
- Default background summaries: also search Jo's default summaries, but use a smaller budget and label them clearly as background.
- Do not search other goal or step scopes by default.

Recommended budgets:

```text
Active scope summaries: 5,000 tokens
Default background summaries: 3,000 tokens
Recent scoped messages: 8,000 tokens
```

Rationale:

- A step chat should feel focused and isolated.
- Jo should still remember relevant background from normal conversations.
- Other step threads should not leak into the active thread unless explicitly designed later.

### Empty step thread flow

When Zach opens a step chat with no prior messages:

1. Dashboard validates the step exists in `data/user.db`.
2. Dashboard builds `MessageScope.step(step_id)`.
3. Dashboard builds entity context from the step, parent goal, sibling steps, and feedback.
4. `assemble_context()` includes persona prompt, user context, current session, active dashboard context, relevant default background summaries, and no scoped recent messages.
5. Zach's first chat message is stored with `scope_type='step'`.
6. Jo's response is generated through `brain.ask(..., task='chat')` or the streaming equivalent.
7. Jo's response is stored with the same scope.
8. Summarization checks only that step scope.

No synthetic assistant greeting should be persisted just because the panel opened. If the UI wants placeholder text, render it as UI chrome, not a stored message.

### Existing step thread flow

When a step thread already has messages:

1. Load recent messages for `MessageScope.step(step_id)`.
2. Load active-scope summaries for that step.
3. Include the current entity snapshot because step status may have changed since older messages.
4. Include default background summaries as lower-priority context.
5. Continue the conversation normally.

### Goal thread flow

Goal-scoped chat is the same mechanism with `MessageScope.goal(goal_id)`. Entity context should include:

- Goal title, category, description, and status.
- Suggested steps.
- Accepted steps.
- Recently completed or rejected steps.
- Feedback patterns for that goal.

---

## 9. Tools

### GoalTool

Purpose: surface current goals and active accepted steps to the agent.

Registration:

```python
def create_tools(memory: PersonaMemory, send_fn=None) -> dict[str, Tool]:
    tools = {}
    tools["schedule"] = ScheduleTool(memory)
    tools["goals"] = GoalTool(SharedGoalStore())
    tools["suggestions"] = SuggestionTool(SharedGoalStore())
    ...
```

`GoalTool.get_context()` should return compact, formatted state:

```text
GOALS

Career
  #1 Learn more about AI safety
    Accepted steps: none
    Open suggestions: Research LucidAI and their tech

Health
  #2 Stay active & healthy
    Accepted steps: Try one Yoga6 class this week
    Stale accepted steps: none
```

Methods:

```text
goals.list_goals       observe
goals.list_steps       observe
goals.get_goal_detail  observe
```

All methods are observe-tier because they read shared state only.

### SuggestionTool

Purpose: let the agent propose and manage candidate steps.

Methods:

```text
suggestions.propose_suggestion  observe
suggestions.list_suggestions    observe
suggestions.update_status       observe
```

Parameters:

```text
propose_suggestion:
  goal_id: int, required
  title: str, required
  description: str, optional
  rationale: str, optional

list_suggestions:
  status: str, optional
  goal_id: int, optional

update_status:
  step_id: int, required
  status: str, required
  note: str, optional
```

Tier rationale:

- These methods mutate internal Purcival state, not the outside world.
- They are similar in risk to ScheduleTool's internal planning mutations.
- They should be observe-tier to avoid approval friction during planning cycles.

Guardrail:

- `propose_suggestion` may only create steps under existing active goals.
- It creates `steps.status='suggested'`, never `accepted`.
- `update_status` may be used by the agent for bookkeeping, but Phase 6 must require explicit user confirmation before changing an accepted step to completed or abandoned from chat.

---

## 10. Agent Prompt Changes

### Planning cycles

Planning cycles are identified today by an empty trigger tool list. For planning cycles, include GoalTool context and SuggestionTool methods.

Add instructions to the agent prompt:

```text
During planning cycles, review Zach's active goals and accepted steps.
If you see a useful next step, propose 1-3 concrete one-shot suggestions.
Suggestions must attach to existing goals.
Do not propose new goals.
Do not use web search or external facts you do not already have.
Prefer small, concrete, checkable steps Zach can accept or reject.
Avoid duplicates of existing suggested, accepted, completed, or recently rejected steps.
Use suggestions.propose_suggestion for candidate steps.
```

Planning context should include:

- Active goals grouped by category.
- Suggested steps.
- Accepted steps.
- Recently completed steps.
- Recently rejected steps.
- Acceptance/rejection patterns if available.

The prompt should explicitly cap new suggestions:

```text
At most 3 new suggestions in a planning cycle. Fewer is better than noise.
```

This matters. A goal dashboard that floods Zach with weak suggestions becomes a guilt machine. Bad. Do not build the guilt machine.

### Non-planning cycles

For targeted wake-ups and regular agent cycles:

- Include accepted steps in context.
- Include stale accepted steps more prominently.
- Do not ask the model to generate new suggestions unless it is a planning cycle.

Prompt addition:

```text
Zach's accepted steps are commitments he chose. Use them for accountability,
but do not nag. If a step is relevant to the current cycle, ask about it
plainly or connect it to the current moment. If it is stale, consider a gentle
check-in. Do not mark a step completed or abandoned without explicit confirmation.
```

---

## 11. Dashboard UI Architecture

Directory:

```text
dashboard/
  app.py
  routes.py
  templates/
    base.html
    index.html
    partials/
      goal_strip.html
      goal_card.html
      suggestion_strip.html
      step_card.html
      chat_panel.html
      chat_message.html
  static/
    dashboard.css
    dashboard.js
  tests/
```

Stack:

- FastAPI
- Jinja2 templates
- HTMX for partial updates
- Small vanilla JS for chat panel state and SSE
- Vanilla CSS with custom properties
- Playwright for e2e and screenshots

Dependencies to add in the implementation phase:

```text
fastapi
uvicorn
jinja2
python-multipart
playwright
pytest
```

`pytest` is included here because the repo already documents `pytest` as the test runner, but the current venv did not have it installed during inspection. If Zach wants dev dependencies separate from runtime dependencies, add a `requirements-dev.txt` rather than putting Playwright and pytest in runtime requirements.

### Routes

Page routes:

```text
GET  /                         dashboard page
GET  /partials/goals           goal strip partial
GET  /partials/suggestions     suggestion strip partial
GET  /partials/chat            chat panel partial for scope_type + scope_id
```

Step state routes:

```text
POST /steps/{step_id}/accept
POST /steps/{step_id}/reject
POST /steps/{step_id}/complete
POST /steps/{step_id}/abandon
```

Goal routes:

```text
POST /goals
POST /goals/{goal_id}/pause
POST /goals/{goal_id}/resume
POST /goals/{goal_id}/archive
```

Chat routes:

```text
GET  /chat/{scope_type}/{scope_id}
GET  /chat/{scope_type}/{scope_id}/messages
POST /chat/{scope_type}/{scope_id}/messages
GET  /chat/streams/{stream_id}
```

Why a `stream_id`:

- Browser `EventSource` is GET-only.
- The user message should be POSTed first so it can be validated and persisted.
- The POST returns a stream id.
- The client opens `GET /chat/streams/{stream_id}` to receive assistant deltas and completion events.

### HTMX patterns

Use HTMX for:

- Accept/reject buttons.
- Completing or abandoning steps.
- Loading chat panel partials.
- Refreshing strips after state changes.

Use vanilla JS only where HTMX is awkward:

- Opening and closing the chat panel.
- Managing an EventSource stream.
- Appending streamed chat deltas.

No frontend build step.

### SSE protocol

Events:

```text
event: delta
data: {"text": "..."}

event: done
data: {"message_id": 123}

event: error
data: {"message": "..."}
```

Implementation note:

Current `brain.ask()` is synchronous and non-streaming. To satisfy the dashboard streaming requirement cleanly, Phase 4 should add a small streaming interface beside it:

```python
def stream(
    messages: list[dict],
    system: str,
    provider: str | None = None,
    max_tokens: int = 2048,
    task: str = "chat",
) -> Iterator[str]:
    ...
```

Provider support:

- OpenAI Chat Completions supports streaming.
- Anthropic supports streaming.
- Ollama's OpenAI-compatible endpoint supports streaming.

Fallback:

- If a provider stream implementation fails or is unavailable, send one `delta` containing the full `brain.ask()` response, then `done`.

This keeps the UI protocol stable while allowing provider streaming to mature.

---

## 12. Visual Direction

The visual identity is decided: cyberpunk dark theme, orange and purple accents, mono font for code-like elements, subtle glow on interactive surfaces.

CSS should start with custom properties:

```css
:root {
  --bg: #08070b;
  --panel: #111018;
  --panel-strong: #181521;
  --text: #f2efe7;
  --muted: #a79fb5;
  --orange: #ff8a2a;
  --purple: #9b5cff;
  --green: #6dffb3;
  --red: #ff5d73;
  --line: rgba(255, 255, 255, 0.11);
  --glow-orange: 0 0 24px rgba(255, 138, 42, 0.22);
  --glow-purple: 0 0 24px rgba(155, 92, 255, 0.22);
}
```

Layout:

- Top strip: goal categories and active goals.
- Main work area: large focused Jo chat.
- Secondary rail: suggested and accepted steps as context.
- Goal and step cards use stable category accent colors.
- Dashboard title changes once per calendar day, not on a timer.
- The title and goal rail are merged to preserve vertical space for chat.
- Goal cards are compact and do not surface step details or step counts.
- Step cards are larger than goal cards and show only one compact suggestion
  text, not title/subtitle metadata; full context belongs in the focused Jo chat.
- Mobile: stack strips vertically and turn chat into a full-width drawer.

Phase 2 visual review moved the dashboard toward a chat-first layout: goals and
steps remain visible context, but the primary action is talking with Jo. Manual
goal/step editing controls should stay minimal because Jo is expected to create
and revise goals and steps through conversation.

Phase 2 acceptance is screenshot-driven. Do not bury visual decisions in code without showing Zach.

Phase 2 was approved by Zach on 2026-05-17 after the compact, chat-first
dashboard polish. Phase 3 is complete after Zach's UI correction: suggested and
accepted steps render from `data/user.db`, accept/reject posts update the steps
panel without a full-page reload, rejected steps do not ask for reasons, thumbs
controls are not shown, step cards avoid title/subtitle treatment, and Phase 3
screenshots plus a Playwright accept/reject flow cover the behavior. Phase 4 is
complete: clicking a goal or step loads a scoped Jo chat panel, messages persist
to Jo's existing memory database with the matching scope, responses are delivered
over SSE through the `brain.stream()` fallback interface, and Playwright verifies
streaming, reload persistence, and default-chat isolation. Phase 5 should begin
in the next development cycle.

---

## 13. Phase Plan

### Phase 1 - Data layer

Implement:

- `data/user.db`.
- `SharedGoalStore`.
- `goals`, `steps`, `step_feedback`.
- Message/summaries scope migration.
- Scope-aware `PersonaMemory`.
- Scope-aware summarization.
- `scripts/seed_dev_data.py`.

Seed goals:

- Learn more about AI safety - career.
- Stay active & healthy - health.
- Be a good husband and father - home.
- Make some extra money - money.

Acceptance:

- CRUD tests pass.
- Existing default chat tests pass.
- Seed utility loads cleanly.
- Existing unscoped messages behave exactly as default-scope messages.

### Phase 2 - Dashboard skeleton

Implement:

- FastAPI app under `dashboard/`.
- Jinja templates.
- CSS theme.
- Seed-backed static render.
- Playwright screenshot script.

Acceptance:

- Local page loads.
- Screenshot committed.
- Zach approves visual identity before dynamic behavior.

### Phase 3 - Real goal and step interactions

Implemented:

- Render real goals and steps from `data/user.db`.
- Accept/reject suggested steps.
- Display accepted steps distinctly.
- HTMX partial updates without full reload.

Acceptance met:

- Playwright accept/reject test.
- Screenshot updated.

### Phase 4 - Chat-on-step and chat-on-goal

Implemented:

- Chat panel loading by scope.
- Scoped message persistence.
- Scoped context assembly.
- Scoped summarization.
- SSE response delivery.
- No leakage into Jo's default chat.

Acceptance met:

- Playwright sends a scoped message.
- Response streams back.
- Message persists in scoped history.
- Jo default chat remains untouched.

### Phase 5 - Agent suggestion generation

Implement:

- `GoalTool`.
- `SuggestionTool`.
- Tool registry integration.
- Planning prompt additions.
- Suggestion insertion into `steps`.

Acceptance:

- Real planning cycle creates sensible `suggested` steps.
- Dashboard shows them within seconds.

### Phase 6 - Accountability

Implement:

- Accepted steps in every agent cycle context.
- Complete/abandon from UI.
- Completion/abandon from chat only after explicit confirmation.
- Stale accepted step surfacing.

Acceptance:

- End-to-end flow: accept step, ignore it, see it referenced later, mark done.

### Phase 7 - Feedback loop polish

Implement:

- Feedback aggregation.
- Category-level acceptance/rejection patterns.
- Rejection reason summaries in planning prompt.

Acceptance:

- Zach confirms suggestions improve after real feedback.

---

## 14. Test Strategy

### Unit tests

Data layer:

- Create goals.
- Update goal status.
- Create suggested, accepted, rejected, completed, abandoned steps.
- Add feedback.
- List goals by category.
- List steps by status.
- Delete goal cascades to steps and feedback.

Scope:

- Existing messages default to `scope_type='default'`.
- Scoped messages do not appear in default recent messages.
- Default messages do not appear in step recent messages.
- Summaries are calculated per scope.
- Retrieval can include default background summaries for scoped chat when requested.

Tools:

- GoalTool context formatting.
- SuggestionTool creates suggested steps only under active goals.
- SuggestionTool rejects nonexistent goal IDs.
- SuggestionTool status transitions are valid.

### Integration tests

Context:

- Empty step thread includes active dashboard context and default background.
- Existing step thread loads scoped recent messages.
- Goal thread includes goal-level step summary.

Agent:

- Planning prompt includes active goals and recent feedback.
- Planning cycle can call `suggestions.propose_suggestion`.
- Non-planning cycle includes accepted steps but does not encourage new suggestions.

Routes:

- Dashboard page renders with seed data.
- Accept/reject endpoints mutate state and return updated partials.
- Chat POST validates scope and stores user message.

### E2E tests

Use Playwright:

- Dashboard loads and matches expected core layout.
- Accept a suggested step; it moves into accepted visual state.
- Reject a suggested step; it leaves the visible step list and stores no reason.
- Open a step chat, send a message, receive streamed assistant response.
- Reload page; scoped messages still appear.
- Verify Jo default chat has no step-scoped message leak.

### Migration tests

- Existing `memory.db` without scope columns migrates.
- Existing messages are queryable as default scope.
- Existing summaries are queryable as default scope.
- Re-running migrations is idempotent.

---

## 15. Risks and Pushback

### Risk: shared goals create a second memory system

The goals database should store structured commitments and feedback, not conversational memory. Chats stay in persona memory. This line matters.

### Risk: scope-aware summarization is easy to get subtly wrong

If summary cursors are not scope-aware, one thread can accidentally mark another thread summarized. Phase 1 needs direct tests for this.

### Risk: suggestion spam

The agent should propose fewer, better steps. Hard cap planning cycles at 1-3 suggestions and tell the model that silence is acceptable.

### Risk: true streaming expands provider complexity

SSE is straightforward. Provider streaming across Claude, ChatGPT, and Ollama is the harder part. The design keeps `brain.ask()` intact and adds a `brain.stream()` sibling with a full-response fallback.

### Risk: dashboard dependency creep

FastAPI, Jinja2, HTMX, and Playwright are already decided. Do not add a frontend framework, bundler, ORM, vector DB, or migration framework unless Zach explicitly approves it.

---

## 16. Decisions Requested

This draft proposes the following concrete decisions for Zach's review:

- Use `data/user.db` for shared goals, steps, and feedback.
- Add `scope_type` and `scope_id` columns to persona `messages` and `summaries`.
- Keep scoped chats in Jo's active persona database.
- Search active-scope summaries first, then a smaller amount of Jo's default-scope background.
- Add `GoalTool` and `SuggestionTool` to the existing agent tool registry.
- Use `Design/dashboard_goals_design.md` as the canonical Goals dashboard design doc.

Phase 1 proceeded from this approved direction: data layer and scope migration first, with no dashboard UI code before the focused data tests passed.
