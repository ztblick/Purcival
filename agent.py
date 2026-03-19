"""
Agent cycle — the self-scheduling agent loop.

This module implements the 9-step cycle that runs whenever a scheduled
trigger fires. It replaces the old _process_trigger function in
proactive.py.

The cycle:
    0. Housekeeping (cleanup old data)
    1. Load state (trigger context, narrative, plan, proposals)
    2. Perceive (run tools, no LLM)
    3. Check pending proposals
    4. Reason (LLM call — always)
    5. Validate + Act
    6. Apply schedule changes
    7. Update state
    8. Safety net
    9. Done

The agent manages its own schedule — it decides when to wake up next
and writes itself notes about what to do. Discovery of new information
happens through planning cycles that scan all tools.
"""

import json
import logging
import re
import uuid
from datetime import datetime

import brain
from memory import PersonaMemory
from context import assemble_context, DEVICE_TELEGRAM
from tools.base import Tool, ToolMethod
from tools.telegram_tool import TelegramTool

logger = logging.getLogger(__name__)

# --- Configuration ---

# Provider for the agent's reasoning calls. Claude for quality and
# reliable structured output. Swap to "ollama" for local experiments.
AGENT_REASONING_PROVIDER = "claude"

# All tool names that exist in the system, even if not instantiated
# in the current process. Used by apply_schedule_updates to validate
# tool names in scheduled wake-ups. The CLI doesn't have TelegramTool
# (no send_fn) but should still allow scheduling wake-ups that use it.
# Add new tool names here when creating new tools.
KNOWN_TOOL_NAMES = {"schedule", "telegram", "google_calendar"}


# --- Response Parsing ---

def _extract_tag(text: str, tag: str) -> str | None:
    """
    Extract content between XML-style tags from the LLM response.

    Returns None if the tag is not found. Returns empty string if
    the tag is present but empty.
    """
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _parse_action_line(line: str) -> dict | None:
    """
    Parse a single action line like:
        telegram.send_message(text="Hello world")
        telegram.send_message("Hello world")

    Handles both keyword and positional arguments.

    Returns a dict with 'tool', 'method', and 'kwargs', or None
    if parsing fails.
    """
    line = line.strip()
    if not line or line.lower() == "none":
        return None

    # Match: tool_name.method_name(...)
    match = re.match(r'^(\w+)\.(\w+)\((.*)\)$', line, re.DOTALL)
    if not match:
        logger.warning(f"Failed to parse action line: {line[:100]}")
        return None

    tool_name = match.group(1)
    method_name = match.group(2)
    args_str = match.group(3).strip()

    # First try keyword parsing
    kwargs = _parse_kwargs(args_str)

    # If keyword parsing found nothing but there's content, try positional.
    # Extract positional args and store them in order — the agent loop
    # will map them to parameter names using the tool's method schema.
    if not kwargs and args_str:
        positional = _split_positional_args(args_str)
        if positional:
            kwargs = {"_positional": [_unquote(a) for a in positional]}

    return {
        "tool": tool_name,
        "method": method_name,
        "kwargs": kwargs,
        "raw": line,
    }


def _parse_kwargs(args_str: str) -> dict:
    """
    Parse keyword arguments from a string like:
        text="Hello world", count=5

    Handles quoted strings (with escaped quotes), numbers, booleans,
    and simple lists. Returns a dict of parsed values.
    """
    if not args_str:
        return {}

    kwargs = {}
    # Use a state machine to handle quoted strings properly
    i = 0
    while i < len(args_str):
        # Skip whitespace and commas
        while i < len(args_str) and args_str[i] in ' ,\n\t':
            i += 1
        if i >= len(args_str):
            break

        # Find key — accept both "key=" and "key:" syntax
        key_match = re.match(r'(\w+)\s*[:=]\s*', args_str[i:])
        if not key_match:
            break
        key = key_match.group(1)
        i += key_match.end()

        # Find value
        if i >= len(args_str):
            break

        if args_str[i] == '"':
            # Quoted string — find the matching close quote
            i += 1  # skip opening quote
            value_chars = []
            while i < len(args_str):
                if args_str[i] == '\\' and i + 1 < len(args_str):
                    next_ch = args_str[i + 1]
                    # Convert escape sequences
                    if next_ch == 'n':
                        value_chars.append('\n')
                    elif next_ch == 't':
                        value_chars.append('\t')
                    else:
                        value_chars.append(next_ch)
                    i += 2
                elif args_str[i] == '"':
                    i += 1  # skip closing quote
                    break
                else:
                    value_chars.append(args_str[i])
                    i += 1
            kwargs[key] = ''.join(value_chars)

        elif args_str[i] == '[':
            # List — find matching bracket
            bracket_depth = 1
            start = i
            i += 1
            while i < len(args_str) and bracket_depth > 0:
                if args_str[i] == '[':
                    bracket_depth += 1
                elif args_str[i] == ']':
                    bracket_depth -= 1
                i += 1
            try:
                kwargs[key] = json.loads(args_str[start:i])
            except json.JSONDecodeError:
                kwargs[key] = args_str[start:i]

        else:
            # Unquoted value — read until comma or end
            value_match = re.match(r'([^,\)]+)', args_str[i:])
            if value_match:
                raw_value = value_match.group(1).strip()
                i += value_match.end()
                # Try to parse as number or boolean
                if raw_value.lower() == 'true':
                    kwargs[key] = True
                elif raw_value.lower() == 'false':
                    kwargs[key] = False
                else:
                    try:
                        kwargs[key] = int(raw_value)
                    except ValueError:
                        try:
                            kwargs[key] = float(raw_value)
                        except ValueError:
                            kwargs[key] = raw_value

    return kwargs


def _parse_schedule_line(line: str) -> dict | None:
    """
    Parse a schedule command line. Handles both keyword and positional args:
        schedule.add_wakeup(time="2026-03-16 09:52", purpose="...", tools=["calendar"])
        schedule.add_wakeup("2026-03-16 09:52", "...", ["calendar"])

    Returns a dict with 'method' and 'kwargs', or None if parsing fails.
    """
    line = line.strip()
    if not line or line.lower() == "none":
        return None

    # Match: schedule.method_name(...)
    match = re.match(r'^schedule\.(\w+)\((.*)\)$', line, re.DOTALL)
    if not match:
        logger.warning(f"Failed to parse schedule line: {line[:100]}")
        return None

    method_name = match.group(1)
    args_str = match.group(2).strip()

    # First try keyword parsing
    kwargs = _parse_kwargs(args_str)

    # If keyword parsing found nothing useful, try positional parsing.
    # The LLM often outputs positional args like:
    #   schedule.add_wakeup("2026-03-17 15:30", "purpose text", ["telegram"])
    if not kwargs and args_str:
        kwargs = _parse_positional_schedule_args(method_name, args_str)

    return {
        "method": method_name,
        "kwargs": kwargs,
        "raw": line,
    }


def _parse_positional_schedule_args(method_name: str, args_str: str) -> dict:
    """
    Parse positional arguments for schedule methods.

    Known signatures:
        add_wakeup(time, purpose, tools)
        modify_wakeup(id, time?, purpose?, tools?)
        cancel_wakeup(id)
    """
    # Extract all top-level arguments by tracking quote/bracket depth
    args = _split_positional_args(args_str)

    if method_name == "add_wakeup" and len(args) >= 2:
        kwargs = {
            "time": _unquote(args[0]),
            "purpose": _unquote(args[1]),
        }
        if len(args) >= 3:
            try:
                kwargs["tools"] = json.loads(args[2])
            except (json.JSONDecodeError, TypeError):
                kwargs["tools"] = []
        return kwargs

    elif method_name == "modify_wakeup" and len(args) >= 1:
        kwargs = {"id": _try_int(_unquote(args[0]))}
        if len(args) >= 2:
            kwargs["time"] = _unquote(args[1])
        if len(args) >= 3:
            kwargs["purpose"] = _unquote(args[2])
        if len(args) >= 4:
            try:
                kwargs["tools"] = json.loads(args[3])
            except (json.JSONDecodeError, TypeError):
                pass
        return kwargs

    elif method_name == "cancel_wakeup" and len(args) >= 1:
        return {"id": _try_int(_unquote(args[0]))}

    return {}


def _split_positional_args(args_str: str) -> list[str]:
    """
    Split a positional argument string respecting quotes and brackets.
    Returns a list of raw argument strings.
    """
    args = []
    current = []
    depth = 0  # bracket depth
    in_quote = False
    escape_next = False

    for ch in args_str:
        if escape_next:
            current.append(ch)
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            current.append(ch)
            continue
        if ch == '"' and depth == 0:
            in_quote = not in_quote
            current.append(ch)
            continue
        if ch in '([':
            depth += 1
            current.append(ch)
            continue
        if ch in ')]':
            depth -= 1
            current.append(ch)
            continue
        if ch == ',' and not in_quote and depth == 0:
            args.append(''.join(current).strip())
            current = []
            continue
        current.append(ch)

    if current:
        args.append(''.join(current).strip())

    return [a for a in args if a]


def _unquote(s: str) -> str:
    """Remove surrounding quotes and process escape sequences."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    elif len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1]
    # Process common escape sequences
    s = s.replace("\\n", "\n")
    s = s.replace("\\t", "\t")
    s = s.replace('\\"', '"')
    s = s.replace("\\'", "'")
    return s


def _try_int(s: str) -> int | str:
    """Try to convert to int, return as-is if it fails."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return s


# --- Prompt Assembly ---

def _build_agent_prompt(
    persona_prompt: str,
    narrative_state: str | None,
    trigger_purpose: str,
    trigger_time: str | None,
    tool_contexts: dict[str, str],
    scheduled_plan: str | None,
    pending_proposals: list[dict],
    available_actions: str,
    schedule_config: dict | None,
    actions_today: int,
) -> str:
    """
    Assemble the full system prompt for the agent reasoning call.

    This is similar to context.py's _build_system_prompt but includes
    agent-specific sections: trigger purpose, tool perceptions, the
    scheduled plan, and available actions.
    """
    from context import _load_user_context, _truncate_to_budget, BUDGET_USER_CONTEXT

    sections = []

    # 1. Persona
    sections.append(f"## PERSONA\n\n{persona_prompt}")

    # 2. User context
    user_ctx = _load_user_context()
    if user_ctx:
        user_ctx = _truncate_to_budget(user_ctx, BUDGET_USER_CONTEXT)
        sections.append(f"## ABOUT THE USER\n\n{user_ctx}")

    # 3. Current time
    now = datetime.now()
    time_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
    sections.append(f"## CURRENT TIME\n\n{time_str}")

    # 4. Narrative state
    if narrative_state:
        sections.append(f"## YOUR CURRENT STATE\n\n{narrative_state}")
    else:
        sections.append(
            "## YOUR CURRENT STATE\n\n"
            "This is your first cycle. You have no previous state."
        )

    # 5. Why the agent is awake — now includes the scheduled time
    wake_section = trigger_purpose
    if trigger_time:
        try:
            fire_dt = datetime.strptime(trigger_time, "%Y-%m-%d %H:%M:%S")
            formatted_time = fire_dt.strftime("%I:%M %p").lstrip("0")
            wake_section = (
                f"Scheduled wake-up time: {formatted_time}\n\n"
                f"{trigger_purpose}"
            )
        except ValueError:
            # Unparseable time — just use the purpose
            pass
    sections.append(f"## WHY YOU ARE AWAKE\n\n{wake_section}")

    # 6. Tool perceptions
    if tool_contexts:
        perception_parts = []
        for tool_name, context in tool_contexts.items():
            perception_parts.append(f"### {tool_name.upper()}\n{context}")
        sections.append(
            "## WHAT YOU PERCEIVE\n\n" + "\n\n".join(perception_parts)
        )

    # 7. Scheduled plan
    if scheduled_plan:
        sections.append(f"## YOUR SCHEDULED PLAN\n\n{scheduled_plan}")
    else:
        sections.append(
            "## YOUR SCHEDULED PLAN\n\n"
            "You have no upcoming wake-ups scheduled. "
            "Consider planning your day."
        )

    # 8. Pending proposals
    if pending_proposals:
        proposal_lines = ["The following actions are awaiting user approval:"]
        for p in pending_proposals:
            proposal_lines.append(
                f"  #{p['id']} ({p['created_at']}): "
                f"{p['tool_name']}.{p['method_name']}({p.get('parameters', '')})"
            )
        sections.append(
            "## PENDING PROPOSALS\n\n" + "\n".join(proposal_lines)
        )

    # 9. Available actions + budget
    budget_info = ""
    if schedule_config:
        max_actions = schedule_config.get("max_actions_per_day", 25)
        remaining = max(0, max_actions - actions_today)
        start = schedule_config.get("start_time", "06:00")
        end = schedule_config.get("end_time", "23:00")
        budget_info = (
            f"Operating hours: {start}–{end}. "
            f"Do not schedule wake-ups outside this window.\n\n"
            f"Action budget: {remaining} actions remaining today (of {max_actions}).\n"
            f"Actions that count: sending messages, drafting, executing.\n"
            f"Reading data and scheduling wake-ups are free.\n\n"
        )

    sections.append(
        f"## AVAILABLE ACTIONS\n\n"
        f"You may take these actions. Choose only what is appropriate.\n"
        f"If nothing warrants action, respond with \"none\" for actions "
        f"and \"none\" for schedule.\n\n"
        f"{budget_info}"
        f"{available_actions}\n\n"
        f"Respond with exactly these four sections:\n"
        f"<reasoning>Your thinking about what to do and why</reasoning>\n"
        f"<actions>Tool calls, one per line, or \"none\"</actions>\n"
        f"<schedule>Schedule changes, one per line, or \"none\"</schedule>\n"
        f"<narrative_state>Updated summary of your current situation</narrative_state>"
    )

    return "\n\n---\n\n".join(sections)


def _format_available_actions(tools: dict[str, Tool]) -> str:
    """
    Format tool methods into a readable list for the LLM prompt.

    Only includes methods the agent has permission to use.
    Groups by tool with method descriptions.
    """
    lines = []

    # Tool methods
    tool_lines = []
    for tool_name, tool in tools.items():
        if tool_name == "schedule":
            continue  # Schedule methods listed separately
        for method in tool.get_methods():
            params = ", ".join(
                f"{k}" for k, v in method.parameters.items()
                if v.get("required", False)
            )
            tier_label = f"[{method.tier}]"
            if method.tier == "execute":
                tier_label = "[requires approval]"
            tool_lines.append(
                f"  - {tool_name}.{method.name}({params}): "
                f"{method.description} {tier_label}"
            )

    if tool_lines:
        lines.append("Tools:")
        lines.extend(tool_lines)

    # Schedule methods (always available)
    # Build the list of valid tool names for the hint
    tool_names = [n for n in tools.keys() if n != "schedule"]
    tool_names_str = ", ".join(f'"{n}"' for n in tool_names) if tool_names else '"telegram"'

    lines.append("")
    lines.append("Schedule management (does not count toward action budget):")
    lines.append(
        f"  - schedule.add_wakeup(time, purpose, tools): Plan a future wake-up. "
        f"time is \"YYYY-MM-DD HH:MM\". tools is a list of tool names: [{tool_names_str}]. "
        f"Use [] for a planning cycle that checks all tools."
    )
    lines.append("  - schedule.modify_wakeup(id, time?, purpose?, tools?): Change a plan.")
    lines.append("  - schedule.cancel_wakeup(id): Remove a planned wake-up.")

    return "\n".join(lines)


# --- Validation ---

def _validate_action(
    action: dict,
    tools: dict[str, Tool],
    actions_today: int,
    max_actions: int,
) -> tuple[bool, str]:
    """
    Validate a parsed action against the code-level gates.

    Returns (is_valid, reason). If is_valid is False, reason explains
    why. If the action needs approval, returns (False, "needs_approval").
    """
    tool_name = action["tool"]
    method_name = action["method"]

    # 1. Tool exists
    if tool_name not in tools:
        return False, f"unknown tool '{tool_name}'"

    tool = tools[tool_name]

    # 2. Tool is enabled
    if not tool.enabled:
        return False, f"tool '{tool_name}' is disabled"

    # 3. Method exists
    method_list = tool.get_methods()
    method_obj = None
    for m in method_list:
        if m.name == method_name:
            method_obj = m
            break
    if method_obj is None:
        return False, f"unknown method '{method_name}' on tool '{tool_name}'"

    # 4. Check tier permission
    if method_obj.tier == "execute":
        return False, "needs_approval"

    # 5. Check daily budget for message/draft tiers
    if method_obj.tier in ("message", "draft"):
        if actions_today >= max_actions:
            return False, "daily action budget exhausted"

    return True, "ok"


def _validate_schedule_change(
    change: dict,
    memory: PersonaMemory,
    schedule_config: dict | None,
    registered_tools: set[str],
) -> tuple[bool, str]:
    """
    Validate a parsed schedule change against the code-level gates.

    Returns (is_valid, reason).
    """
    method = change["method"]
    kwargs = change["kwargs"]
    now = datetime.now()

    if method in ("add_wakeup", "modify_wakeup"):
        # Check time is provided for add, optional for modify
        time_str = kwargs.get("time")
        if method == "add_wakeup" and not time_str:
            return False, "add_wakeup requires a time"

        if time_str:
            # Parse and validate time
            try:
                normalized = time_str.strip()
                if len(normalized) == 16:
                    normalized += ":00"
                fire_dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return False, f"invalid time format: {time_str}"

            # Must be in the future
            if fire_dt <= now:
                return False, f"time is in the past: {time_str}"

            # Must be within operating hours
            if schedule_config:
                start_h, start_m = map(int, schedule_config["start_time"].split(":"))
                end_h, end_m = map(int, schedule_config["end_time"].split(":"))
                fire_minutes = fire_dt.hour * 60 + fire_dt.minute
                start_minutes = start_h * 60 + start_m
                end_minutes = end_h * 60 + end_m
                if fire_minutes < start_minutes or fire_minutes > end_minutes:
                    return False, (
                        f"time {time_str} is outside operating hours "
                        f"({schedule_config['start_time']}–{schedule_config['end_time']})"
                    )

        # Validate and normalize tool names if provided.
        # The LLM sometimes outputs "telegram.send_message" instead of
        # just "telegram". We extract the tool name (before the dot).
        tools_list = kwargs.get("tools", [])
        if isinstance(tools_list, list):
            normalized_tools = []
            for t in tools_list:
                # Strip method name if present: "telegram.send_message" → "telegram"
                tool_name = t.split(".")[0] if isinstance(t, str) else t
                if tool_name not in registered_tools and tool_name not in ("schedule",):
                    return False, f"unknown tool '{tool_name}' in tools list"
                normalized_tools.append(tool_name)
            # Deduplicate
            kwargs["tools"] = list(dict.fromkeys(normalized_tools))

    if method in ("modify_wakeup", "cancel_wakeup"):
        trigger_id = kwargs.get("id")
        if trigger_id is None:
            return False, f"{method} requires an id"
        trigger = memory.get_trigger(int(trigger_id))
        if not trigger:
            return False, f"trigger #{trigger_id} not found"
        if trigger.get("fired"):
            return False, f"trigger #{trigger_id} has already fired"

    if method not in ("add_wakeup", "modify_wakeup", "cancel_wakeup", "get_plan"):
        return False, f"unknown schedule method '{method}'"

    return True, "ok"


# --- Tool Context Caching ---

# Tools whose context should NOT be cached for conversations.
# Schedule context is already shown via _load_scheduled_plan().
_CACHE_EXCLUDE_TOOLS = {"schedule"}


def _cache_tool_contexts(memory: PersonaMemory, tool_contexts: dict[str, str]):
    """
    Cache tool contexts in tool_state so they're available in
    user conversations (Telegram and terminal).

    Called after the perception step of each agent cycle. The cached
    contexts are read by context.py's _load_tool_contexts() when
    assembling prompts for user messages.

    Each tool's context is stored under the key "cached_context"
    with a corresponding "cached_context_at" timestamp. The timestamp
    lets context.py judge freshness and filter out past events.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for tool_name, context in tool_contexts.items():
        if tool_name in _CACHE_EXCLUDE_TOOLS:
            continue
        memory.set_tool_state(tool_name, "cached_context", context)
        memory.set_tool_state(tool_name, "cached_context_at", now)
        logger.debug(f"Cached context for tool '{tool_name}' ({len(context)} chars)")


# --- The Agent Cycle ---

async def run_agent_cycle(
    trigger: dict,
    memory: PersonaMemory,
    tools: dict[str, Tool],
    persona_prompt: str,
    send_fn=None,
):
    """
    Execute one complete agent cycle.

    This is the function that replaces _process_trigger in proactive.py.
    It runs the full 9-step cycle: housekeeping, load state, perceive,
    check proposals, reason, validate + act, apply schedule changes,
    update state, done.

    Args:
        trigger: The trigger dict that started this cycle.
        memory: The persona's memory instance.
        tools: Dict of tool name → tool instance.
        persona_prompt: The persona's system prompt text.
        send_fn: Async function for sending Telegram messages.
    """
    cycle_id = str(uuid.uuid4())[:8]
    trigger_id = trigger.get("id")

    logger.info(
        f"Agent cycle {cycle_id} starting "
        f"(trigger #{trigger_id}: {trigger.get('context', '')[:80]})"
    )

    # --- Step 0: Housekeeping ---
    memory.cleanup_old_data()

    # --- Step 1: Load State ---
    # Parse trigger context
    trigger_context = _parse_trigger_context(trigger)
    trigger_purpose = trigger_context.get("purpose", "Scheduled check-in")
    trigger_tools = trigger_context.get("tools", [])
    trigger_time = trigger.get("fire_at")

    # Empty tools list = planning cycle (load all tools).
    # Specific tools list = targeted cycle (load only those).
    is_planning = len(trigger_tools) == 0

    # Read narrative state
    narrative_state = memory.get_narrative()

    # Read schedule config for guardrails
    schedule_config = memory.get_schedule_config()

    # Read pending proposals
    pending_proposals = memory.get_pending_proposals()

    # Count today's actions for budget
    actions_today = memory.get_today_action_count()
    max_actions = 25  # default
    if schedule_config:
        max_actions = schedule_config.get("max_actions_per_day", 25)

    # --- Step 2: Perceive ---
    tool_contexts = {}

    if is_planning:
        # Planning cycle: run all enabled tools
        for name, tool in tools.items():
            if tool.enabled:
                try:
                    ctx = tool.get_context()
                    if ctx is not None:
                        tool_contexts[name] = ctx
                except Exception as e:
                    logger.error(f"Tool '{name}' get_context() failed: {e}")
    else:
        # Targeted cycle: run only the tools listed in the trigger
        for name in trigger_tools:
            if name in tools and tools[name].enabled:
                try:
                    ctx = tools[name].get_context()
                    if ctx is not None:
                        tool_contexts[name] = ctx
                except Exception as e:
                    logger.error(f"Tool '{name}' get_context() failed: {e}")

    # Cache tool contexts so they're available in user conversations.
    # Each tool's context is stored in tool_state with a timestamp.
    # context.py reads these cached values when assembling prompts
    # for user messages (Telegram and terminal).
    _cache_tool_contexts(memory, tool_contexts)

    # Always get the schedule plan (it's always relevant)
    if "schedule" in tools:
        schedule_plan = tools["schedule"].get_context()
    else:
        schedule_plan = None

    # --- Step 3: Check Proposals ---
    # TODO: Check if user responded to pending proposals.
    # This requires scanning recent messages for approval keywords.
    # Deferred to integration step — for now, proposals just persist.

    # --- Step 4: Reason ---
    # Build the available actions description
    available_actions = _format_available_actions(tools)

    # Build the full agent prompt
    system_prompt = _build_agent_prompt(
        persona_prompt=persona_prompt,
        narrative_state=narrative_state,
        trigger_purpose=trigger_purpose,
        trigger_time=trigger_time,
        tool_contexts=tool_contexts,
        scheduled_plan=schedule_plan,
        pending_proposals=pending_proposals,
        available_actions=available_actions,
        schedule_config=schedule_config,
        actions_today=actions_today,
    )

    # Build the messages array (recent conversation + summaries)
    _, messages = assemble_context(persona_prompt, memory, device=DEVICE_TELEGRAM)

    # Add the agent cycle prompt as the final user message
    messages.append({
        "role": "user",
        "content": f"[Agent cycle: {trigger_purpose}]",
    })

    logger.info(f"Cycle {cycle_id}: reasoning about: {trigger_purpose}")

    try:
        llm_response = brain.ask(
            messages,
            system=system_prompt,
            provider=AGENT_REASONING_PROVIDER,
        )
    except Exception as e:
        logger.error(f"Cycle {cycle_id}: reasoning failed: {e}")
        memory.add_reasoning_log(
            cycle_id=cycle_id,
            trigger_id=trigger_id,
            trigger_purpose=trigger_purpose,
            narrative_in=narrative_state,
            tool_contexts=json.dumps(tool_contexts),
            skipped=True,
            skip_reason=f"LLM call failed: {e}",
            provider=AGENT_REASONING_PROVIDER,
        )
        _ensure_future_plan(memory, tools, schedule_config)
        return

    # --- Parse the response ---
    reasoning = _extract_tag(llm_response, "reasoning") or ""
    actions_text = _extract_tag(llm_response, "actions") or "none"
    schedule_text = _extract_tag(llm_response, "schedule") or "none"
    narrative_out = _extract_tag(llm_response, "narrative_state") or ""

    logger.info(f"Cycle {cycle_id}: reasoning complete, parsing actions")

    # Parse actions
    action_lines = [
        line for line in actions_text.split("\n")
        if line.strip() and line.strip().lower() != "none"
    ]
    parsed_actions = [_parse_action_line(line) for line in action_lines]
    parsed_actions = [a for a in parsed_actions if a is not None]

    # Parse schedule changes
    schedule_lines = [
        line for line in schedule_text.split("\n")
        if line.strip() and line.strip().lower() != "none"
    ]
    parsed_schedule = [_parse_schedule_line(line) for line in schedule_lines]
    parsed_schedule = [s for s in parsed_schedule if s is not None]

    # --- Step 5: Validate + Act ---
    actions_taken = []

    for action in parsed_actions:
        valid, reason = _validate_action(action, tools, actions_today, max_actions)

        if valid:
            # Execute the action
            tool = tools[action["tool"]]
            kwargs = action["kwargs"]

            # Resolve positional args to named kwargs using the method's
            # parameter schema. The LLM sometimes outputs positional args
            # instead of keyword args.
            if "_positional" in kwargs:
                positional = kwargs.pop("_positional")
                method_obj = _get_method_obj(tool, action["method"])
                if method_obj and method_obj.parameters:
                    param_names = list(method_obj.parameters.keys())
                    for i, val in enumerate(positional):
                        if i < len(param_names):
                            kwargs[param_names[i]] = val
                elif len(positional) == 1:
                    # Single positional arg with no schema — try common
                    # parameter names
                    kwargs["text"] = positional[0]

            try:
                result = tool.execute(action["method"], **kwargs)
                memory.add_agent_action(
                    cycle_id=cycle_id,
                    tool_name=action["tool"],
                    method_name=action["method"],
                    tier=_get_method_tier(tool, action["method"]),
                    parameters=json.dumps(action["kwargs"]),
                    result=result,
                    status="completed",
                )
                actions_taken.append({
                    "action": action["raw"],
                    "status": "completed",
                    "result": result,
                })
                actions_today += 1  # Update running count

                # Handle async Telegram sends
                if isinstance(tool, TelegramTool) and send_fn:
                    msg = tool.get_pending_message()
                    if msg:
                        try:
                            await send_fn(msg)
                            # Also persist as an assistant message for conversation continuity
                            memory.add_message("assistant", msg)
                            logger.info(f"Cycle {cycle_id}: sent message: {msg[:80]}...")
                        except Exception as e:
                            logger.error(f"Cycle {cycle_id}: Telegram send failed: {e}")

            except Exception as e:
                logger.error(f"Cycle {cycle_id}: action execution failed: {e}")
                memory.add_agent_action(
                    cycle_id=cycle_id,
                    tool_name=action["tool"],
                    method_name=action["method"],
                    tier=_get_method_tier(tools.get(action["tool"]), action["method"]),
                    parameters=json.dumps(action["kwargs"]),
                    result=str(e),
                    status="failed",
                )
                actions_taken.append({
                    "action": action["raw"],
                    "status": "failed",
                    "reason": str(e),
                })

        elif reason == "needs_approval":
            # Store as pending proposal
            memory.add_agent_action(
                cycle_id=cycle_id,
                tool_name=action["tool"],
                method_name=action["method"],
                tier="execute",
                parameters=json.dumps(action["kwargs"]),
                status="pending_approval",
            )
            actions_taken.append({
                "action": action["raw"],
                "status": "pending_approval",
            })
            logger.info(f"Cycle {cycle_id}: proposal stored: {action['raw'][:80]}")

        else:
            # Validation failed
            memory.add_agent_action(
                cycle_id=cycle_id,
                tool_name=action.get("tool", "unknown"),
                method_name=action.get("method", "unknown"),
                tier="unknown",
                parameters=json.dumps(action.get("kwargs", {})),
                result=reason,
                status="failed",
            )
            actions_taken.append({
                "action": action["raw"],
                "status": "failed",
                "reason": reason,
            })
            logger.warning(f"Cycle {cycle_id}: action rejected: {reason}")

    # --- Step 6: Apply Schedule Changes ---
    schedule_changes_applied = []
    registered_tool_names = set(tools.keys())

    for change in parsed_schedule:
        valid, reason = _validate_schedule_change(
            change, memory, schedule_config, registered_tool_names
        )

        if valid:
            schedule_tool = tools.get("schedule")
            if schedule_tool:
                try:
                    result = schedule_tool.execute(change["method"], **change["kwargs"])
                    schedule_changes_applied.append({
                        "change": change["raw"],
                        "status": "applied",
                        "result": result,
                    })
                    logger.info(f"Cycle {cycle_id}: schedule change: {result}")
                except Exception as e:
                    schedule_changes_applied.append({
                        "change": change["raw"],
                        "status": "failed",
                        "reason": str(e),
                    })
                    logger.error(f"Cycle {cycle_id}: schedule change failed: {e}")
        else:
            schedule_changes_applied.append({
                "change": change["raw"],
                "status": "rejected",
                "reason": reason,
            })
            logger.warning(
                f"Cycle {cycle_id}: schedule change rejected: {reason} "
                f"({change['raw'][:80]})"
            )

    # --- Step 7: Update State ---

    # Save narrative state (use new if provided, otherwise keep old)
    if narrative_out:
        memory.set_narrative(narrative_out, cycle_id)
    elif narrative_state:
        # LLM didn't produce a new narrative — keep the old one
        pass

    # Log the full reasoning trace
    memory.add_reasoning_log(
        cycle_id=cycle_id,
        trigger_id=trigger_id,
        trigger_purpose=trigger_purpose,
        tool_contexts=json.dumps(tool_contexts) if tool_contexts else None,
        narrative_in=narrative_state,
        llm_response=llm_response,
        actions_taken=json.dumps(actions_taken) if actions_taken else None,
        schedule_changes=json.dumps(schedule_changes_applied) if schedule_changes_applied else None,
        narrative_out=narrative_out,
        skipped=False,
        provider=AGENT_REASONING_PROVIDER,
    )

    # --- Step 8: Ensure future plan exists ---
    _ensure_future_plan(memory, tools, schedule_config)

    logger.info(
        f"Cycle {cycle_id} complete: "
        f"{len(actions_taken)} actions, "
        f"{len(schedule_changes_applied)} schedule changes"
    )


# --- Helper Functions ---

def _parse_trigger_context(trigger: dict) -> dict:
    """
    Parse a trigger's context field into a structured dict.

    Agent cycle triggers store JSON with 'purpose' and 'tools'.
    Legacy triggers store plain text.

    The tools list determines perception scope:
      - Empty list → load all enabled tools (planning cycle behavior)
      - Specific names → load only those tools (targeted cycle)
    """
    context_str = trigger.get("context", "")
    if not context_str:
        return {"purpose": "Scheduled check-in", "tools": []}

    try:
        ctx = json.loads(context_str)
        return {
            "purpose": ctx.get("purpose", "Scheduled check-in"),
            "tools": ctx.get("tools", []),
        }
    except (json.JSONDecodeError, TypeError):
        # Legacy trigger — treat as planning cycle with the text as purpose
        return {
            "purpose": context_str,
            "tools": [],
        }


def _get_method_tier(tool: Tool | None, method_name: str) -> str:
    """Look up a method's tier on a tool. Returns 'unknown' if not found."""
    if tool is None:
        return "unknown"
    for method in tool.get_methods():
        if method.name == method_name:
            return method.tier
    return "unknown"


def _get_method_obj(tool: Tool | None, method_name: str):
    """Look up a ToolMethod object by name. Returns None if not found."""
    if tool is None:
        return None
    for method in tool.get_methods():
        if method.name == method_name:
            return method
    return None


def _ensure_future_plan(
    memory: PersonaMemory,
    tools: dict[str, Tool],
    schedule_config: dict | None,
):
    """
    Called at the end of every agent cycle. If no future planning cycle
    exists, seed one for tomorrow's start_time as a safety net.

    This checks specifically for planning cycles (empty tools list),
    not just any future trigger. Targeted wake-ups (pre-meeting
    reminders, etc.) don't count — the agent needs a planning cycle
    to discover new events and schedule its day.

    This is a backstop for when the LLM reasons but forgets to schedule
    its next planning cycle. It should rarely fire — the LLM is prompted
    to manage its own schedule. But if it does forget, this ensures the
    agent wakes up tomorrow rather than going silent forever.
    """
    if memory.has_future_planning_cycle():
        return  # Agent has a planning cycle scheduled

    if not schedule_config:
        return  # No schedule configured, agent is passive

    # Safety net: schedule for tomorrow's start_time
    now = datetime.now()
    start_h, start_m = map(int, schedule_config["start_time"].split(":"))

    from datetime import timedelta
    tomorrow_start = now.replace(
        hour=start_h, minute=start_m, second=0, microsecond=0
    ) + timedelta(days=1)

    memory.add_trigger(
        trigger_type="agent_cycle",
        fire_at=tomorrow_start.strftime("%Y-%m-%d %H:%M:%S"),
        context=json.dumps({
            "purpose": "Planning cycle — review all tools and plan the day",
            "tools": [],
        }),
        recurring=None,
    )
    logger.info(f"Safety net: no planning cycle found, seeded for {tomorrow_start}")


# --- Schedule Updates in Conversation Responses ---

def strip_schedule_updates(response: str) -> tuple[str, list[str]]:
    """
    Strip <schedule_updates> tags from an LLM response.

    Returns (clean_response, schedule_lines).
    The clean_response has the tags removed and is what the user sees.
    The schedule_lines are the raw lines inside the tags for parsing.

    If no tags are present, returns (response, []).

    Used by both the terminal (main.py) and Telegram (telegram_bot.py)
    paths to handle schedule updates embedded in conversation responses.
    """
    pattern = r"<schedule_updates>(.*?)</schedule_updates>"
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response.strip(), []

    # Extract the schedule commands
    schedule_block = match.group(1).strip()
    schedule_lines = [
        line.strip() for line in schedule_block.split("\n")
        if line.strip()
    ]

    # Remove the tags from the response
    clean = response[:match.start()] + response[match.end():]
    clean = clean.strip()

    return clean, schedule_lines


def apply_schedule_updates(schedule_lines: list[str], memory: PersonaMemory) -> list[dict]:
    """
    Parse and apply schedule update commands from an LLM response.

    Uses the same parsing and validation logic as the agent cycle.
    Invalid commands are logged and skipped.

    The registered tool names include all known tools, not just the
    ones instantiated in the current process. This is important
    because the CLI doesn't have a Telegram send_fn, so TelegramTool
    isn't created — but the agent should still be able to schedule
    wake-ups that use Telegram. The trigger will fire in the Telegram
    service process where the tool IS available.

    Args:
        schedule_lines: Raw schedule command strings from the LLM.
        memory: The persona's memory instance.

    Returns:
        List of result dicts with 'change', 'status', and optionally
        'result' or 'reason' for each command processed.
    """
    from tools import create_tools

    tools = create_tools(memory)
    schedule_config = memory.get_schedule_config()

    # Include all known tool names — not just the ones instantiated
    # in this process. The CLI won't have TelegramTool or possibly
    # GoogleCalendarTool, but scheduled triggers that reference them
    # will fire in the Telegram service where they ARE available.
    registered_tools = set(tools.keys()) | KNOWN_TOOL_NAMES
    results = []

    for line in schedule_lines:
        parsed = _parse_schedule_line(line)
        if not parsed:
            logger.warning(f"Failed to parse schedule update: {line[:100]}")
            results.append({
                "change": line,
                "status": "failed",
                "reason": "parse error",
            })
            continue

        valid, reason = _validate_schedule_change(
            parsed, memory, schedule_config, registered_tools
        )

        if valid:
            schedule_tool = tools.get("schedule")
            if schedule_tool:
                try:
                    result = schedule_tool.execute(parsed["method"], **parsed["kwargs"])
                    logger.info(f"Schedule update applied: {result}")
                    results.append({
                        "change": line,
                        "status": "applied",
                        "result": result,
                    })
                except Exception as e:
                    logger.error(f"Schedule update failed: {e}")
                    results.append({
                        "change": line,
                        "status": "failed",
                        "reason": str(e),
                    })
        else:
            logger.warning(f"Schedule update rejected: {reason} ({line[:80]})")
            results.append({
                "change": line,
                "status": "rejected",
                "reason": reason,
            })

    return results