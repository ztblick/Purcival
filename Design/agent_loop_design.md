# Purcival Agent Loop — The Self-Scheduling Agent

## Purpose of This Document

This document defines the architecture that transformed Purcival from a
scheduled messenger into an autonomous agent that plans its own day,
perceives its environment through tools, reasons about what to do, and
manages its own schedule. It covers the tool interface, the agent cycle,
schedule management, state tracking, and integration with both the
proactive system and user-initiated conversations.

This was originally written before the Goals dashboard work. The filename and
active terminology now use "agent loop" to avoid confusing the completed agent
architecture with future numbered dashboard phases.

Telegram examples in this document describe the original message tool and
historical mobile interface. Telegram is not currently operable in Zach's
Windows setup; Jo via local CLI/dashboard is the active path.

---

## The Core Idea

Today, Purcival's proactive system works like this:

    fixed timer fires → compose message → send

The agent loop replaces this with an agent that manages its own schedule:

    agent wakes up → reads its own note about why it's awake →
    loads relevant tools → reasons about what to do →
    takes actions → plans its next wake-ups → sleeps

The key insight: **the agent plans its own day.** Instead of waking up
on a dumb fixed interval and scanning everything, the agent schedules
purposeful wake-ups with specific contexts. "Wake me at 9:52 to
encourage Zach before his 10:00 meeting." "Wake me at 11:05 to ask
how the Q3 review went." "Wake me at 11:00 tomorrow to remind Zach
about the Benihana reservation."

Each wake-up carries context the agent wrote for its future self:
what to do, why, and which tools are needed. This makes every cycle
focused and efficient — the agent already knows why it's awake.

**Discovery happens through planning cycles.** The agent can't schedule
a wake-up for an email that hasn't arrived yet. So it maintains periodic
planning cycles — a morning briefing, a midday check, an afternoon
re-plan — where it scans all tools for new information and adjusts its
schedule accordingly. These planning cycles are themselves triggers that
the agent manages. A busy day gets more planning cycles. A quiet
Saturday might just get a morning check-in.

**User messages can update plans.** When the user says "my meeting got
moved to 2pm," the agent responds naturally and adjusts its scheduled
wake-ups as part of the same response. No separate planning cycle needed.

---

## Action Permission Tiers

Every action the agent can take falls into one of four tiers. Permissions
are set per-persona and stored in the database. The user configures them
via the terminal (/tools command, designed in a future session).

| Tier | What it means | Default | Examples |
|------|---------------|---------|----------|
| **Observe** | Read external data, update internal state | Allowed | Read calendar, read inbox |
| **Message** | Send a Telegram message to the user | Allowed | "You have a meeting in 5 minutes" |
| **Draft** | Prepare an action for user review | Allowed | Draft an email, propose a calendar event |
| **Execute** | Take an action in the real world as the user | Requires explicit approval | Send an email, create a calendar event |

The tiers are ordered by risk. An execute-tier method can never run
without the user saying yes. An observe-tier method runs silently.

**Approval flow for execute-tier actions:**

1. Agent proposes the action via Telegram message (using message tier).
2. User replies with approval (e.g., "yes", "send it", "go ahead").
3. Agent executes the action on the next cycle.
4. If no response by next cycle, the proposal stays pending. The agent
   can remind once, then drops it after a configurable timeout.
5. If user rejects ("no", "don't send"), the proposal is cancelled.

Pending proposals are stored in structured state (see State Tracking
below) so they survive restarts and are visible across cycles.

---

## Guardrails

The agent operates within hard boundaries enforced by code. The LLM
is told about these boundaries in its prompt, but the code enforces
them regardless of what the LLM outputs.

### Wake and sleep times

The user sets explicit wake and sleep times via `/schedule` in the
terminal. These are stored in `schedule_config` as `start_time` and
`end_time`. They define the agent's operating window:

- **First planning cycle** fires at `start_time` every day. This is
  the "agent wakes up and plans its day" moment. The bootstrap logic
  seeds this automatically.

- **No wake-ups outside the window.** The schedule validation gate
  (step 7 of the cycle) rejects any wake-up the LLM tries to schedule
  before `start_time` or after `end_time`. The trigger is silently
  dropped and logged.

- **User conversations still work outside hours.** If the user messages
  the agent at midnight, the conversation handler responds normally.
  The restriction is on agent-initiated cycles, not user-initiated
  messages.

These are code-level constraints. The prompt tells the LLM "your
operating hours are 06:00–23:00" so it plans accordingly, but the
code enforces the boundary even if the LLM ignores the instruction.

### Daily action limit

The user sets a maximum number of actions per day via `/schedule`.
This is stored in `schedule_config` as `max_actions_per_day`. Default
starting value: 25.

"Actions" means tool executions that affect the outside world —
message-tier (sending Telegram messages), draft-tier, and execute-tier.
Observe-tier methods (reading calendar, reading email) and schedule
management (adding/modifying wake-ups) do NOT count against the limit.
The agent can always perceive and plan. The limit constrains how much
it *does*.

**Enforcement:** At step 6 (VALIDATE + ACT), the code checks how many
actions have been logged in `agent_actions` today with status
`'completed'` and tier in (`'message'`, `'draft'`, `'execute'`). If
the count has reached the limit, further actions are blocked for the
day. The agent can still reason and update its schedule, but tool
executions are held.

**Budget visibility:** The reasoning prompt includes the remaining
action budget so the LLM can prioritize. "You have 8 actions remaining
today." This lets the agent make informed decisions about what's worth
doing — sending a pre-meeting encouragement is worth an action; sending
a "quiet afternoon" check-in may not be.

---

## Tool Interface

### Base class

Every tool implements a common interface. The agent loop interacts with
tools only through this interface — it doesn't know or care whether a
tool talks to Google, a local file, or a web API.

```python
class Tool:
    """Base class for all agent tools."""

    name: str               # e.g. "google_calendar"
    description: str        # Human-readable, shown to LLM for reasoning
    enabled: bool           # Can be toggled per persona

    def get_context(self) -> str | None:
        """
        Perception: return current state as a plain text string.

        Called when the agent cycle includes this tool. Should be fast
        and deterministic. Returns None if there's nothing relevant
        to report.

        The tool is responsible for diffing against its own last-known
        state internally. It only returns NEW or CHANGED information,
        not a full dump every time.

        This method should NEVER call an LLM. It reads external APIs,
        diffs against stored state, and formats the result as text.
        """
        ...

    def get_methods(self) -> list[ToolMethod]:
        """
        Return the actions this tool can perform, with tier tags.

        The agent loop includes these in the LLM prompt so the model
        knows what actions are available. The permission system checks
        tiers before execution.
        """
        ...

    def execute(self, method_name: str, **kwargs) -> str:
        """
        Perform an action. Returns a human-readable result string.

        Only called after the agent loop has verified permissions
        through the code-level validation gate. The result is logged
        and can be sent to the user if appropriate.
        """
        ...


class ToolMethod:
    """Describes one action a tool can take."""
    name: str           # e.g. "send_email"
    description: str    # For the LLM: "Send an email from the user's account"
    tier: str           # "observe", "message", "draft", "execute"
    parameters: dict    # JSON-schema-like description of expected kwargs
```

### Why this interface works

- **`get_context()` is the perception layer.** It runs only when needed,
  costs nothing (no LLM), and returns only what's new. If it returns
  None, the tool has nothing to contribute this cycle.

- **`get_methods()` tells the LLM what it can do.** The agent loop
  formats these into the system prompt so the model can reason about
  available actions. Methods it doesn't have permission for are excluded.

- **`execute()` is the action layer.** Called only after code-level
  validation of tool, method, tier, and parameters.

- **State diffing lives inside each tool.** The GoogleCalendarTool
  tracks its own known events. The GmailTool tracks its own seen
  message IDs. The agent loop doesn't understand tool internals.

### Tool state storage

Each tool persists its own state between cycles (last sync time, known
event IDs, seen email IDs, etc.) via a generic key-value store:

```sql
CREATE TABLE tool_state (
    tool_name   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tool_name, key)
);
```

Tools read and write through PersonaMemory helpers:

```python
memory.get_tool_state(tool_name, key) -> str | None
memory.set_tool_state(tool_name, key, value)
```

Values are text. Tools serialize complex data as JSON. This avoids
schema migrations when adding new tools.

---

## Concrete Tools

### GoogleCalendarTool

**Data source:** Google Calendar API (read-only initially).

**`get_context()` logic (no LLM):**

1. Fetch events from now to +24 hours via Calendar API.
2. Load last-known events from tool_state ("known_events" key, JSON).
3. Diff: identify new events, changed events, cancelled events, and
   events starting within the next 15 minutes.
4. Load event action history from tool_state ("event_actions" key).
5. If nothing changed and nothing is imminent, return None.
6. Otherwise, return a formatted text block that includes both the
   event data and the agent's previous actions on each event:
   - "IMMINENT: 'Q3 Review' starts in 8 minutes"
   - "  Previous actions: (none — first engagement)"
   - "NEW: 'Lunch with Sarah' added for 12:00 PM"
   - "UPCOMING: 3 events today — [list]"
7. Update tool_state with current known events.

**Methods:**

| Method | Tier | Description |
|--------|------|-------------|
| `get_upcoming` | observe | Fetch next N hours of events |
| `create_event` | execute | Create a new calendar event |
| `update_event` | execute | Modify an existing event |

**State stored in tool_state:**

| Key | Value | Purpose |
|-----|-------|---------|
| `last_sync_at` | ISO timestamp | Avoid re-fetching too frequently |
| `known_events` | JSON array of event objects | Diff detection |
| `event_actions` | JSON object (see below) | Track full event lifecycle |

**Event action history:** The agent may engage with the same event
multiple times — encouragement before, debrief after, follow-up the
next day. The `event_actions` state tracks what the agent has done:

```json
{
  "event_id_abc123": [
    {"action": "pre_meeting_encouragement", "at": "2026-03-16 09:52"},
    {"action": "post_meeting_debrief_prompt", "at": "2026-03-16 11:05"}
  ]
}
```

The LLM sees this history and decides what's appropriate next. Stale
entries (events older than 7 days) are pruned during `get_context()`.

### GmailTool

**Data source:** Gmail API (read-only initially).

**`get_context()` logic (no LLM):**

1. Fetch messages via Gmail API with a query that filters at the API
   level: `category:primary is:unread newer_than:1d`. Promotional
   emails, social notifications, and updates never leave Google's
   servers.
2. Load last-known message IDs from tool_state ("seen_message_ids").
3. Diff: identify genuinely new messages not in the seen set.
4. If no new messages pass the filter, return None. The LLM is never
   invoked to reason about spam or coupons.
5. For new messages, return a formatted text block with sender,
   subject, date, and snippet (~200 chars). NOT full bodies — the
   agent can request full content via a method call if needed.
6. Update tool_state with current seen message IDs.

**Secondary Python filter (after API results):**

- Messages marked important by Gmail → always surface
- Messages from senders the user has previously replied to → surface
- All other primary messages → surface (primary is already filtered)

**Methods:**

| Method | Tier | Description |
|--------|------|-------------|
| `get_unread` | observe | Fetch unread message list with snippets |
| `get_message` | observe | Fetch full content of a specific message |
| `get_thread` | observe | Fetch all messages in a conversation thread |
| `draft_reply` | draft | Create a draft reply to a message |
| `send_email` | execute | Send an email (new or reply) |

**State stored in tool_state:**

| Key | Value | Purpose |
|-----|-------|---------|
| `last_sync_at` | ISO timestamp | Avoid re-fetching too frequently |
| `seen_message_ids` | JSON array of message IDs | Diff detection |
| `message_actions` | JSON object {msg_id: [actions]} | Track engagement per message |

### TelegramTool

Wraps the existing `send_fn` for a uniform tool interface.

**Methods:**

| Method | Tier | Description |
|--------|------|-------------|
| `send_message` | message | Send a Telegram message to the user |

No `get_context()` — incoming Telegram messages are handled by the
existing `handle_message` path, not by the agent loop.

### ScheduleTool

**This is the tool that makes the agent self-scheduling.** It exposes
the triggers table as a tool the LLM can manipulate. The agent uses
it to plan its own wake-ups.

**Methods:**

| Method | Tier | Description |
|--------|------|-------------|
| `get_plan` | observe | Return all upcoming scheduled wake-ups |
| `add_wakeup` | observe | Schedule a new wake-up with context and tool tags |
| `modify_wakeup` | observe | Change the time, context, or tools of an existing wake-up |
| `cancel_wakeup` | observe | Remove a scheduled wake-up |

**All ScheduleTool methods are observe-tier.** The agent managing its
own schedule is internal housekeeping, not a user-facing action. It
doesn't need approval to decide when to wake up next. The schedule
is the agent's own planning mechanism, not an action taken on behalf
of the user.

**`get_plan()` returns a formatted view of upcoming wake-ups:**

```
YOUR SCHEDULED PLAN:
  #42  Today 09:52  — Encourage Zach before Q3 Review [calendar, telegram]
  #43  Today 11:05  — Ask Zach how the Q3 Review went [calendar, telegram]
  #44  Today 13:00  — Midday planning cycle: check calendar and email [calendar, email]
  #45  Today 17:00  — Afternoon review [calendar, email]
  #46  Tomorrow 06:30  — Morning planning: review day and plan [calendar, email]
  #47  Tomorrow 11:00  — Remind Zach about Benihana reservation [telegram]
```

This view is always included in the reasoning prompt, giving the LLM
awareness of its own future. The LLM can see what it has planned,
decide if adjustments are needed, and add/modify/cancel wake-ups as
part of its action output.

**Validation lives inside the tool.** ScheduleTool validates operating
hours, future time checks, trigger existence, and tool name normalization
in its own `execute()` method. The agent loop's generic validation gate
handles tool existence, method existence, tier permissions, and budget.
Tool-specific rules are the tool's responsibility. Invalid inputs raise
`ValueError` with descriptive messages, which the agent loop catches and
logs as failed actions.

**How wake-ups are stored:**

Wake-ups use the existing triggers table with an extended context format:

```json
{
  "purpose": "Encourage Zach before Q3 Review at 10:00",
  "tools": ["google_calendar", "telegram"]
}
```

The `tools` field determines which tools load their context for this
cycle. An empty tools list means "load all enabled tools" — this is
how planning cycles work. There is no separate planning_cycle flag.

**The daily bootstrap:** On the first wake-up of each day (or on
service start if no triggers exist), the agent runs a planning cycle
that reads the full calendar, checks email, and schedules all its
wake-ups for the day. This is the "agent plans its own day" moment.
If no wake-ups exist at all (fresh start, or all triggers have
fired), the system seeds a single planning cycle for the next
available time slot based on the persona's schedule_config.

### Adding tools in the future

To add a new tool (Slack, weather, web search, etc.):

1. Create a class implementing the Tool interface
2. Register it in tools/__init__.py
3. Enable it for a persona

No changes to the agent loop, the reasoning prompt structure, or the
schedule system. The agent will discover the new tool's methods and
can schedule wake-ups that use it.

---

## Agent Cycle

The agent cycle runs whenever a scheduled trigger fires. Each trigger
carries context about its purpose and which tools to load.

### Cycle steps

```
┌──────────────────────────────────────────────────────┐
│                    AGENT CYCLE                        │
│                                                       │
│  0. HOUSEKEEPING                                      │
│     Clean up reasoning log (> 7 days)                 │
│     Clean up agent_actions (> 30 days)                │
│     Clean up agent_narrative (> 30 days)              │
│                                                       │
│  1. LOAD STATE                                        │
│     Read this trigger's context (purpose + tools)     │
│     Read narrative state from last cycle               │
│     Read upcoming scheduled plan (via ScheduleTool)    │
│     Read pending proposals from agent_actions          │
│                                                       │
│  2. PERCEIVE (no LLM)                                 │
│     If tools list empty: run all enabled tools         │
│     If tools list has names: run only those tools      │
│     For each tool: context = tool.get_context()        │
│     Collect non-None results                          │
│                                                       │
│  3. CHECK PROPOSALS                                   │
│     Any execute-tier actions awaiting approval?        │
│     Did the user respond? Handle approved/rejected.    │
│                                                       │
│  4. REASON (LLM call — always)                        │
│     Assemble prompt:                                  │
│       - Persona prompt + user context                 │
│       - Narrative state                               │
│       - This trigger's purpose                        │
│       - Tool contexts (from step 2)                   │
│       - Upcoming scheduled plan                       │
│       - Pending proposals                             │
│       - Available actions (ALL tools incl. schedule)  │
│       - Action budget remaining                       │
│       - Recent conversation messages + summaries      │
│     LLM responds with:                                │
│       - <reasoning> (freeform — LLM thinking)         │
│       - <actions> (JSON array — ALL tool calls)       │
│       - <narrative_state> (freeform — LLM state)      │
│                                                       │
│  5. VALIDATE + ACT (unified for ALL tools)            │
│     Parse <actions> as JSON array.                    │
│     Check daily action budget (agent_actions today).   │
│     For each action object:                           │
│       a. Verify tool exists and is enabled            │
│       b. Verify method exists on tool                 │
│       c. Verify tier permission                       │
│       d. Check action budget (message/draft/execute)  │
│       e. If valid + budget → execute via tool         │
│          (tool handles its own validation — e.g.      │
│           ScheduleTool checks operating hours)        │
│       f. If needs approval → store as pending         │
│       g. If over budget → log "budget exhausted"      │
│       h. If invalid → log violation, skip             │
│     This is a CODE-LEVEL gate. LLM cannot bypass it.  │
│     Schedule operations flow through the same gate.   │
│                                                       │
│  6. UPDATE STATE                                      │
│     Append new narrative state                        │
│     Log reasoning trace                               │
│     Record actions in agent_actions                   │
│                                                       │
│  7. SAFETY NET                                        │
│     If agent has no future triggers after this cycle,  │
│     seed tomorrow's planning cycle at start_time.     │
│                                                       │
│  8. DONE — next trigger fires when scheduled          │
└──────────────────────────────────────────────────────┘
```

### Unified action format

All tool calls — including schedule management — use a single JSON
format in the `<actions>` tag. There is no separate `<schedule>` tag.
The LLM outputs three sections, not four:

- `<reasoning>` — freeform text (LLM thinking, not parsed by code)
- `<actions>` — JSON array of tool calls (parsed and validated)
- `<narrative_state>` — freeform text (LLM state for next cycle)

**Why JSON instead of freeform strings?** Inspired by the AutoBE
framework's "compiler strategy": the LLM fills structured forms
(JSON), not freeform text. This eliminates ~300 lines of fragile
string parsing code and makes validation trivial — `json.loads()`
either works or it doesn't. Schema constraints make invalid output
structurally impossible rather than relying on prompt instructions.

**Why not JSON for everything?** Reasoning and narrative state are
consumed by future LLM instances, not by code. Constraining the
LLM's thinking into a schema would reduce the quality of its
reasoning — research shows forcing JSON format competes with the
reasoning process for attention. Schema-constrain the interface
between LLM and code; leave freeform the interface between LLM
and LLM.

```
SYSTEM PROMPT:
  ## PERSONA
  [persona prompt from .md file]

  ## ABOUT THE USER
  [user_context.md]

  ## YOUR CURRENT STATE
  [narrative state from last cycle]

  ## WHY YOU ARE AWAKE
  [this trigger's purpose field]
  Example: "Encourage Zach before his Q3 Review at 10:00"
  Example: "Morning planning cycle — review calendar and email,
            plan your day"

  ## WHAT YOU PERCEIVE
  [tool contexts, only for tools loaded this cycle]
  Example:
    ### CALENDAR
    IMMINENT: "Q3 Review with leadership" starts in 8 minutes.
    PREVIOUS ACTIONS FOR THIS EVENT: (none)
    UPCOMING: Lunch with Sarah (12:00), Dentist (3:30 PM).

  ## YOUR SCHEDULED PLAN
  [upcoming wake-ups from ScheduleTool.get_plan()]
    #43  Today 11:05  — Ask Zach how Q3 Review went [calendar, telegram]
    #44  Today 13:00  — Midday planning cycle [calendar, email]
    ...

  ## PENDING PROPOSALS
  [any execute-tier actions awaiting user approval]

  ## AVAILABLE ACTIONS
  You may take these actions. Choose only what is appropriate.
  If nothing warrants action, use an empty array [] for actions.

  Operating hours: 06:00–23:00. Do not schedule wake-ups outside
  this window.

  Action budget: 8 actions remaining today (of 25).
  Actions that count: sending messages, drafting, executing.
  Reading data and scheduling wake-ups are free.

  Tools:
    - telegram.send_message: Send a Telegram message. [message]
      Parameters: text (required)
    - google_calendar.get_upcoming: View upcoming events. [observe]
      Parameters: hours (optional)
    - schedule.add_wakeup: Plan a future wake-up. [observe]
      Parameters: time (required); purpose (required); tools (required)
    - schedule.modify_wakeup: Change a plan. [observe]
      Parameters: id (required); time, purpose, tools (optional)
    - schedule.cancel_wakeup: Remove a planned wake-up. [observe]
      Parameters: id (required)

  Respond with these three sections:
  <reasoning>Your thinking</reasoning>
  <actions>JSON array of tool calls, or []</actions>
  <narrative_state>Updated situation summary</narrative_state>

  ## RELEVANT PAST CONVERSATIONS
  [summaries retrieved by semantic search]

MESSAGES ARRAY:
  [recent verbatim messages]
  [final message: "[Agent cycle: {trigger purpose}]"]
```

### The expected response format

```
<reasoning>
Zach's Q3 Review starts in 8 minutes. From yesterday's conversation,
he was nervous about presenting the roadmap. This is what I woke up
for — time to send encouragement. I also notice my 11:05 debrief
wake-up is already scheduled, so no planning changes needed.
</reasoning>

<actions>
[{"tool": "telegram", "method": "send_message", "parameters": {"text": "Quick heads up — Q3 review in 8 minutes. You've prepped well for this. Go get 'em."}}]
</actions>

<narrative_state>
Sent Zach encouragement before his Q3 Review at 10:00 AM. He was
nervous about the roadmap presentation. I have a debrief scheduled
for 11:05. Lunch with Sarah at noon, dentist at 3:30. John's email
about the resume is still pending — I'll bring that up after the
meeting debrief. Midday planning cycle at 1:00 PM.
</narrative_state>
```

Example of a planning cycle with schedule changes:

```
<reasoning>
Morning planning. Calendar shows: Q3 Review at 10:00, Lunch with
Sarah at noon, Dentist at 3:30. Zach mentioned being nervous about
the Q3 review yesterday. Two new emails — one from John about the
resume (important, Zach was excited about this), one from GitHub
(noise). I should plan my day around these events.
</reasoning>

<actions>
[{"tool": "telegram", "method": "send_message", "parameters": {"text": "Good morning! Busy day ahead — Q3 review at 10, lunch with Sarah at noon, dentist at 3:30. I'll keep an eye on things and give you a heads up before each one."}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 09:52", "purpose": "Encourage Zach before Q3 Review at 10:00. He was nervous about the roadmap presentation.", "tools": ["google_calendar", "telegram"]}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 11:05", "purpose": "Ask Zach how the Q3 Review went.", "tools": ["google_calendar", "telegram"]}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 11:45", "purpose": "Remind Zach about lunch with Sarah at noon.", "tools": ["google_calendar", "telegram"]}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 13:00", "purpose": "Midday planning cycle.", "tools": []}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 15:15", "purpose": "Remind Zach about dentist at 3:30.", "tools": ["google_calendar", "telegram"]}},
 {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "2026-03-16 17:00", "purpose": "Afternoon review.", "tools": ["google_calendar"]}}]
</actions>

<narrative_state>
Monday morning. Sent Zach a morning briefing. Today's schedule:
Q3 Review at 10 (he's nervous about the roadmap), lunch with Sarah
at noon, dentist at 3:30. John emailed about reviewing Zach's resume
— this is the opportunity he's been excited about. I'll bring it up
after the Q3 review debrief. Planned wake-ups throughout the day
for each event and two planning cycles to catch changes.
</narrative_state>
```

### LLM provider for reasoning

The reasoning call uses Claude for quality and reliable structured
output. The provider is configurable:

```python
AGENT_REASONING_PROVIDER = "claude"  # Easy to swap to "ollama" later
```

Zach wants to experiment with large local models for reasoning since
the cycle is asynchronous and latency doesn't matter. This requires
only changing the constant.

---

## User Messages and Plan Updates

When the user sends a message through an active chat interface, the conversation follows
the existing path: persist message → assemble context → call LLM →
persist and send response. The agent loop adds one thing: **the agent's
upcoming plan is included in the conversation context, and the LLM
can output schedule changes alongside its response.**

### How it works

1. The existing `assemble_context()` function gains a new section:
   "YOUR SCHEDULED PLAN" — the same view from ScheduleTool.get_plan().

2. The system prompt includes a note: "If the user's message affects
   any of your planned wake-ups, include schedule updates in your
   response using <schedule_updates> tags. Otherwise, respond normally."

3. The LLM responds with its normal conversational message. If it
   detects a conflict with its plan, it appends `<schedule_updates>`
   tags with the changes.

4. The code strips the `<schedule_updates>` tags before sending the
   message to the user via Telegram. It parses and applies the
   schedule changes to the triggers table.

5. Most messages won't affect the plan. In those cases, there are no
   tags, no parsing, no overhead. The conversation works exactly as
   it does today.

### Example

User: "Hey, the Q3 review got pushed to 2pm"

LLM response (what the code receives):
```
Got it — I've adjusted my reminders. I'll give you a heads up before
the 2pm slot instead. The rest of your day looks the same: lunch with
Sarah at noon and dentist at 3:30.

<schedule_updates>
[{"tool": "schedule", "method": "modify_wakeup", "parameters": {"id": 42, "time": "2026-03-16 13:52", "purpose": "Encourage Zach before Q3 Review, now at 2:00 PM."}},
 {"tool": "schedule", "method": "modify_wakeup", "parameters": {"id": 43, "time": "2026-03-16 15:05", "purpose": "Ask Zach how the Q3 Review went. Moved to 2 PM."}}]
</schedule_updates>
```

What the user sees on Telegram:
```
Got it — I've adjusted my reminders. I'll give you a heads up before
the 2pm slot instead. The rest of your day looks the same: lunch with
Sarah at noon and dentist at 3:30.
```

The schedule updates are applied silently. The user sees a natural
conversational response. The agent's plan is updated without a separate
cycle or LLM call.

### What if the LLM hallucinates schedule changes?

The same code-level validation gate applies. Trigger IDs must exist.
Times must be in the future. The parsing is strict. Invalid schedule
updates are logged and discarded — the user's conversational response
is still sent normally.

---

## State Tracking

### Structured state

Tracked by code, never by the LLM. Stored in SQLite.

**`agent_actions` table (30-day retention):**

```sql
CREATE TABLE agent_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    method_name TEXT NOT NULL,
    tier        TEXT NOT NULL,
    parameters  TEXT,
    result      TEXT,
    status      TEXT NOT NULL DEFAULT 'completed',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- status values:
--   'completed'         — executed successfully
--   'failed'            — attempted but failed
--   'pending_approval'  — execute-tier, awaiting user response
--   'approved'          — user approved, will execute next cycle
--   'rejected'          — user rejected
--   'expired'           — no response within timeout
```

**`tool_state` table (no global retention — tools prune their own):**

```sql
CREATE TABLE tool_state (
    tool_name   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tool_name, key)
);
```

### Narrative state

Written by the LLM at the end of each cycle. Read at the start of the
next cycle. Single row, overwritten each cycle.

```sql
CREATE TABLE agent_narrative (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    narrative   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What goes where:**

| Information | Where | Why |
|-------------|-------|-----|
| "Zach seemed nervous about Q3" | Narrative | Emotional context for LLM |
| Event ID abc123 actions taken | Structured (tool_state) | Prevents duplicate actions |
| "John's email is an opportunity" | Narrative | LLM's interpretation |
| Proposal #7 pending since 08:47 | Structured (agent_actions) | Tracks approval flow |
| Message ID xyz seen at 08:32 | Structured (tool_state) | Diff detection |

Rule: if code reads it for logic → structured. If LLM reads it for
judgment → narrative.

---

## Reasoning Log

Every agent cycle produces a reasoning trace for debugging.

**`reasoning_log` table (7-day retention):**

```sql
CREATE TABLE reasoning_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id        TEXT NOT NULL,
    trigger_id      INTEGER,
    trigger_purpose TEXT,
    tool_contexts   TEXT,
    narrative_in    TEXT,
    llm_response    TEXT,
    actions_taken   TEXT,
    schedule_changes TEXT,
    narrative_out   TEXT,
    skipped         BOOLEAN DEFAULT FALSE,
    skip_reason     TEXT,
    provider        TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Retention cleanup runs at the start of each cycle:**

```python
def _cleanup_old_data(memory: PersonaMemory):
    """Enforce retention policies."""
    now = datetime.now()

    # Reasoning logs: 7 days
    reasoning_cutoff = (now - timedelta(days=7)).strftime(...)
    # DELETE FROM reasoning_log WHERE created_at < reasoning_cutoff

    # Agent actions: 30 days
    actions_cutoff = (now - timedelta(days=30)).strftime(...)
    # DELETE FROM agent_actions WHERE created_at < actions_cutoff
```

---

## Action Validation

The LLM proposes actions as a JSON array. The code validates every
action before executing. This is a hard security boundary.

**Two layers of validation:**

**Layer 1: Generic gate (agent loop).** Applied uniformly to all
tool calls, including schedule operations:

1. **JSON parse succeeds.** The `<actions>` content must be a valid
   JSON array of objects with `tool`, `method`, and `parameters`.

2. **Tool exists.** Must match a registered tool.

3. **Tool is enabled.** Disabled tools can't be used.

4. **Method exists.** Must be in the tool's `get_methods()` list.

5. **Tier is permitted.** Execute-tier without approval → route to
   proposal flow. Disallowed tiers → skip.

6. **Daily budget not exhausted.** For message/draft/execute tiers,
   check the count of today's completed actions in `agent_actions`.
   If at the limit → log "budget exhausted", skip.

If any generic check fails, the action is logged in `agent_actions`
with `status='failed'` and a reason. One bad action doesn't abort
the cycle.

**Layer 2: Tool-specific validation (inside each tool's execute()).**
Each tool validates its own parameters and business rules:

- **ScheduleTool** validates: time is in the future, time is within
  operating hours, trigger ID exists and hasn't fired, tool names in
  the wake-up are known. Raises `ValueError` on failure.
- **TelegramTool** validates: text is not empty.
- **GoogleCalendarTool** validates: event IDs exist, etc.

Tool-specific validation failures are caught by the agent loop and
logged as failed actions — they don't crash the cycle.

---

## The Daily Bootstrap

When the persona's service starts (or when no future triggers exist),
the system seeds a planning cycle at the next occurrence of `start_time`
from `schedule_config`. This is the agent's alarm clock — it wakes up
at the user's configured time every day.

If the current time is already past `start_time` but before `end_time`,
the bootstrap seeds an immediate planning cycle so the agent catches up.
If it's past `end_time`, it seeds for tomorrow's `start_time`.

The agent takes it from there — the planning cycle discovers the day's
events and emails, plans targeted wake-ups, and manages itself forward.

If the user hasn't configured a schedule via `/schedule` yet, no
triggers are seeded and no agent cycles run. The persona operates
in its current mode (user-initiated conversations only) until
a schedule is set.

**Bootstrap logic:**

```python
def ensure_agent_has_plan(memory: PersonaMemory):
    """
    Called at service startup and after every trigger fires.
    If no future triggers exist, seed a planning cycle.
    """
    active = memory.get_active_triggers()
    future_triggers = [t for t in active if t["fire_at"] > now_str]

    if future_triggers:
        return  # Agent already has a plan

    schedule = memory.get_schedule_config()
    if not schedule:
        return  # No schedule configured, agent is passive

    # Determine next wake-up time
    start_h, start_m = map(int, schedule["start_time"].split(":"))
    end_h, end_m = map(int, schedule["end_time"].split(":"))
    now = datetime.now()

    today_start = now.replace(hour=start_h, minute=start_m, second=0)
    today_end = now.replace(hour=end_h, minute=end_m, second=0)

    if now < today_start:
        next_time = today_start           # Before wake time → wake at start
    elif now < today_end:
        next_time = now + timedelta(minutes=1)  # During hours → wake now
    else:
        next_time = today_start + timedelta(days=1)  # After hours → tomorrow

    memory.add_trigger(
        trigger_type="agent_cycle",
        fire_at=next_time.strftime("%Y-%m-%d %H:%M:%S"),
        context=json.dumps({
            "purpose": "Planning cycle — review all tools and plan the day",
            "tools": [],
            "planning_cycle": True,
        }),
        recurring=None,
    )
```

---

## Integration with Existing System

### What stays the same

- **Persona system** — unchanged. Personas are still markdown files.
- **Memory system** — messages, summaries, embeddings all unchanged.
- **Brain module** — unchanged. Agent uses brain.ask() like everything.
- **Telegram bot handle_message** — extended slightly (schedule updates
  in responses), but core conversation flow is preserved.
- **Summarization** — unchanged. Runs after user messages.
- **Schedule config** — /schedule command is preserved. It controls
  the time window and minimum interval for the agent's self-scheduling.
  The agent plans within these bounds.

### What changes

- **proactive.py** — `_process_trigger` replaced by the agent cycle.
  Scheduler and trigger infrastructure preserved. `seed_triggers`
  replaced by `ensure_agent_has_plan`.

- **memory.py** — gains new tables (tool_state, agent_actions,
  agent_narrative, reasoning_log) and corresponding methods.

- **context.py** — gains a "SCHEDULED PLAN" section in the system
  prompt for both agent cycles and user conversations.

- **telegram_bot.py** — response handler strips and applies
  `<schedule_updates>` tags before sending to user.

### New files

| File | Purpose |
|------|---------|
| `tools/base.py` | Tool and ToolMethod base classes |
| `tools/google_calendar.py` | Google Calendar integration |
| `tools/gmail.py` | Gmail integration |
| `tools/telegram_tool.py` | Telegram send wrapper |
| `tools/schedule_tool.py` | Self-scheduling interface |
| `tools/__init__.py` | Tool registry and discovery |
| `agent.py` | The agent cycle — perceive, reason, act, plan |

### New dependencies

```
google-api-python-client>=2.0.0
google-auth-oauthlib>=1.0.0
google-auth-httplib2>=0.2.0
```

### Google API authentication

OAuth2 with offline refresh tokens. Initial auth runs once from the
terminal. Credentials saved to `data/<persona>/google_credentials.json`
(gitignored).

Scopes (read-only to start):
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`

Write scopes added when execute-tier methods are enabled:
- `https://www.googleapis.com/auth/calendar.events`
- `https://www.googleapis.com/auth/gmail.send`

---

## Implementation Plan

Build in this order. Each step is independently testable.

### Step 1: Database schema + Tool base class + ScheduleTool
- Add new tables to memory.py
- Add PersonaMemory methods for tool_state, agent_actions,
  agent_narrative, reasoning_log
- Create tools/base.py with Tool and ToolMethod
- Create tools/schedule_tool.py (wraps trigger operations)
- Write tests for all new database operations

### Step 2: Agent cycle (core loop)
- Implement the 8-step cycle in agent.py
- Wire into proactive.py (replace _process_trigger)
- Reasoning prompt assembly
- Response parsing (JSON actions, XML envelope)
- Unified validation gate (all tools including schedule)
- State updates + reasoning log
- Bootstrap logic (ensure_agent_has_plan)
- Test with ScheduleTool + TelegramTool only (no Google yet)

### Step 3: User message integration
- Add scheduled plan to conversation context
- Add <schedule_updates> JSON parsing to telegram_bot.py response handler
- Test: user message causes plan update

### Step 4: Google API authentication
- Set up Google Cloud project
- Implement OAuth2 flow (terminal-based)
- Credential storage and refresh
- Test: read calendar events, read emails

### Step 5: GoogleCalendarTool
- Implement get_context() with diffing and event action history
- Implement get_methods()
- State tracking via tool_state
- Test: tool identifies new/changed/imminent events

### Step 6: GmailTool
- Implement get_context() with API-level and Python-level filtering
- Implement get_methods()
- State tracking via tool_state
- Test: tool surfaces important emails, filters noise

### Step 7: End-to-end integration
- Full agent cycle with all tools
- Morning planning → targeted wake-ups → user interaction → replanning
- Reasoning log inspection via terminal
- Cost monitoring over a typical day

### Step 8: Execute tier (future)
- Add write scopes to Google auth
- Implement gmail.send_email, calendar.create_event
- Approval flow via Telegram
- Test: full proposal → approve → execute cycle

---

## Implementation Status

### Completed

- [x] Database schema: tool_state, agent_actions, agent_narrative,
  reasoning_log tables with full CRUD methods
- [x] Tool base class (Tool, ToolMethod) with interface contract
- [x] ScheduleTool: get_plan, add_wakeup, modify_wakeup, cancel_wakeup
  with internal validation (operating hours, future time, trigger
  existence, tool name normalization)
- [x] TelegramTool: wraps send_fn with pending message queue
- [x] Tool registry (tools/__init__.py) with create_tools() factory
- [x] Agent cycle: full perceive → reason → act → plan loop
- [x] Unified JSON action format: all tool calls (including schedule)
  in a single <actions> JSON array — no separate <schedule> tag
- [x] Three-section LLM output: <reasoning> (freeform), <actions>
  (JSON), <narrative_state> (freeform)
- [x] JSON response parsing with _parse_actions_json()
- [x] Generic action validation gate: 5 code-level checks
- [x] Tool-specific validation inside each tool's execute() method
- [x] Bootstrap logic (ensure_agent_has_plan) with safety net
- [x] User message integration: <schedule_updates> with JSON format
- [x] Scheduled plan in conversation context
- [x] Telegram chat ID persistence across restarts
- [x] Narrative state as append-only log (30-day retention)
- [x] 70 passing tests covering all components
- [x] Google Calendar integration (read-only, multi-calendar)

### Next steps

- [ ] Google API authentication for Gmail (OAuth2 flow)
- [ ] GmailTool with API-level and Python-level filtering
- [ ] Execute-tier approval flow via Telegram
- [ ] Investigate trigger deletion issue (triggers disappearing
  between cycles — need to trace whether the agent is cancelling
  them or if there's a code path deleting them)

---

## Cost Estimate

**Targeted wake-up (purpose-driven):**
- 1 LLM call, ~4K-8K tokens input, ~500 tokens output
- ~$0.01-0.03 per call (Claude Sonnet)

**Planning cycle (discovery):**
- 1 LLM call if changes detected, 0 if nothing new
- Same cost as targeted when reasoning fires

**Typical day (busy, 8 events, moderate email):**
- 1 morning planning cycle
- ~6 targeted wake-ups (pre/post for key events)
- 1-2 midday planning cycles
- 1 afternoon review
- Total: ~10-12 LLM calls/day
- ~$0.10-0.36/day → ~$3-11/month

**Quiet day (no events, few emails):**
- 1 morning planning cycle (finds nothing, sends brief greeting)
- 1-2 check-in cycles (find nothing, skip reasoning)
- Total: 1-2 LLM calls/day

Well within experimental budget. First cost lever: agent naturally
plans fewer cycles on quiet days. Second lever: swap to local model.

---

## Schedule Configuration

The `/schedule` command is extended to configure the agent's guardrails.
The `schedule_config` table stores:

| Field | Description | Default |
|-------|-------------|---------|
| `start_time` | Agent's daily wake-up time (HH:MM) | 06:00 |
| `end_time` | Agent's daily sleep time (HH:MM) | 23:00 |
| `interval_minutes` | Preserved for backward compat; not used by self-scheduling agent | 30 |
| `max_actions_per_day` | Cap on message/draft/execute actions per day | 25 |

The `/schedule` command prompts for wake time, sleep time, and daily
action limit.

---

## Open Questions

1. **Proposal timeout.** How long should an execute-tier proposal wait
   for approval before expiring? Starting suggestion: 24 hours.

2. **Rate limiting on Google APIs.** At our polling frequency (a few
   times per day per tool), we're nowhere near limits.

3. **Multiple personas sharing Google auth.** Deferred. One persona
   gets tools. Others stay conversational.

4. **Google API error handling.** First pass: log error, skip tool this
   cycle, retry next cycle. Surface persistent failures after N
   consecutive failures.

5. **Evening background work.** Zach mentioned wanting the agent to do
   behind-the-scenes work in the evening (research, drafting, etc.).
   This is a natural extension — the agent schedules work cycles
   outside of messaging hours. Deferred to a future stage.

6. **Disappearing triggers.** Some agent-scheduled triggers have been
   found deleted between cycles. The agent's reasoning log shows no
   cancel commands. Needs investigation.

### Resolved

7. **~~LLM output format.~~** RESOLVED: Actions use JSON arrays inside
   XML `<actions>` tags. Reasoning and narrative stay as freeform text.
   The principle: schema-constrain the interface between LLM and code,
   leave freeform the interface between LLM and LLM. Inspired by the
   AutoBE framework's "compiler strategy" — the LLM fills structured
   forms, compilers (validators) verify correctness.

8. **~~Schedule as separate output channel.~~** RESOLVED: Schedule
   operations are just tool calls to the ScheduleTool, unified with
   all other actions in a single `<actions>` JSON array. The separate
   `<schedule>` tag was eliminated. Tool-specific validation (operating
   hours, future time checks) moved into ScheduleTool.execute().
