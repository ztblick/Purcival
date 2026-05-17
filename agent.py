"""
Agent cycle — the self-scheduling agent loop.

This module implements the agent cycle that runs whenever a scheduled
trigger fires. It replaces the old _process_trigger function in
proactive.py.

The cycle:
    0. Housekeeping (cleanup old data)
    1. Load state (trigger context, narrative, plan, proposals)
    2. Perceive (run tools, no LLM)
    3. Check pending proposals
    4. Reason (LLM call — always)
    5. Validate + Act (all tool calls, including schedule)
    6. Update state
    7. Safety net
    8. Done

All tool calls — including schedule management — use a unified JSON
format in the <actions> tag. There is no separate <schedule> tag.
Schedule operations are just tool calls to the "schedule" tool, validated
and executed through the same pipeline as every other tool.

The LLM outputs three sections:
    <reasoning>  — freeform text (LLM thinking, not parsed by code)
    <actions>    — JSON array of tool calls (parsed and validated by code)
    <narrative_state> — freeform text (LLM state for next cycle)
"""

import json
import logging
import re
import uuid
from datetime import datetime

import brain
import config
from memory import PersonaMemory
from context import assemble_context, DEVICE_TELEGRAM
from tools.base import Tool, ToolMethod
from tools.telegram_tool import TelegramTool

logger = logging.getLogger(__name__)

# --- Configuration ---

AGENT_REASONING_MAX_TOKENS = 4096
PLANNING_CYCLE_MAX_RETRIES = 3


# --- JSON Response Parsing ---

def _extract_tag(text: str, tag: str) -> str | None:
    """
    Extract content between XML-style tags from the LLM response.
    Returns None if the tag is not found.
    """
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _parse_actions_json(text: str) -> tuple[list[dict], str | None]:
    """
    Parse the <actions> tag content as a JSON array of tool calls.

    Expected format:
        [
          {"tool": "telegram", "method": "send_message", "parameters": {"text": "Hello"}},
          {"tool": "schedule", "method": "add_wakeup", "parameters": {"time": "...", "purpose": "...", "tools": []}}
        ]

    Returns (parsed_actions, error_message).
    If parsing succeeds, error_message is None.
    If parsing fails, parsed_actions is [] and error_message describes the problem.
    """
    if not text or text.strip() == "[]":
        return [], None

    # Try to parse as JSON
    try:
        actions = json.loads(text)
    except json.JSONDecodeError as e:
        return [], f"JSON parse error in <actions>: {e}"

    if not isinstance(actions, list):
        return [], f"<actions> must be a JSON array, got {type(actions).__name__}"

    # Validate each action has required fields
    validated = []
    errors = []
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"Action {i}: expected object, got {type(action).__name__}")
            continue
        if "tool" not in action:
            errors.append(f"Action {i}: missing 'tool' field")
            continue
        if "method" not in action:
            errors.append(f"Action {i}: missing 'method' field")
            continue
        validated.append({
            "tool": str(action["tool"]),
            "method": str(action["method"]),
            "parameters": action.get("parameters", {}),
        })

    if errors:
        return validated, "; ".join(errors)
    return validated, None


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

    # 5. Why the agent is awake
    wake_section = trigger_purpose
    if trigger_time:
        try:
            fire_dt = datetime.strptime(trigger_time, "%Y-%m-%d %H:%M:%S")
            formatted_time = fire_dt.strftime("%I:%M %p").lstrip("0")
            wake_section = f"Scheduled wake-up time: {formatted_time}\n\n{trigger_purpose}"
        except ValueError:
            pass
    sections.append(f"## WHY YOU ARE AWAKE\n\n{wake_section}")

    # 6. Tool perceptions
    if tool_contexts:
        perception_parts = []
        for tool_name, context in tool_contexts.items():
            perception_parts.append(f"### {tool_name.upper()}\n{context}")
        sections.append("## WHAT YOU PERCEIVE\n\n" + "\n\n".join(perception_parts))

    # 7. Scheduled plan
    if scheduled_plan:
        sections.append(f"## YOUR SCHEDULED PLAN\n\n{scheduled_plan}")
    else:
        sections.append(
            "## YOUR SCHEDULED PLAN\n\nYou have no upcoming wake-ups scheduled. "
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
        sections.append("## PENDING PROPOSALS\n\n" + "\n".join(proposal_lines))

    # 9. Available actions + budget + JSON format instructions
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
        f"If nothing warrants action, use an empty array [] for actions.\n\n"
        f"{budget_info}"
        f"{available_actions}\n\n"
        f"Respond with exactly these three sections:\n\n"
        f"<reasoning>Your thinking about what to do and why</reasoning>\n\n"
        f"<actions>\n"
        f"A JSON array of tool calls. Each object has \"tool\", \"method\", and \"parameters\".\n"
        f"Use [] if no actions are needed. Examples:\n"
        f'[{{"tool": "telegram", "method": "send_message", "parameters": {{"text": "Hello!"}}}},\n'
        f' {{"tool": "schedule", "method": "add_wakeup", "parameters": {{"time": "2026-03-16 11:05", "purpose": "Check in after meeting", "tools": ["telegram"]}}}}]\n'
        f"</actions>\n\n"
        f"<narrative_state>Updated summary of your current situation</narrative_state>"
    )

    return "\n\n---\n\n".join(sections)


def _format_available_actions(tools: dict[str, Tool]) -> str:
    """
    Format tool methods into a readable list for the LLM prompt.
    All tools (including schedule) are listed uniformly.
    """
    lines = ["Tools:"]

    for tool_name, tool in tools.items():
        for method in tool.get_methods():
            # Build parameter description
            param_parts = []
            for k, v in method.parameters.items():
                req = " (required)" if v.get("required") else " (optional)"
                param_parts.append(f"{k}: {v.get('description', '')}{req}")
            params_str = "; ".join(param_parts) if param_parts else "none"

            tier_label = f"[{method.tier}]"
            if method.tier == "execute":
                tier_label = "[requires approval]"

            lines.append(
                f"  - {tool_name}.{method.name}: "
                f"{method.description} {tier_label}\n"
                f"    Parameters: {params_str}"
            )

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

    Returns (is_valid, reason). If is_valid is False, reason explains why.
    If the action needs approval, returns (False, "needs_approval").

    This is the GENERIC validation gate. Tool-specific validation
    (like operating hours for schedule, or email format for gmail)
    is handled inside each tool's execute() method.
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
    method_obj = None
    for m in tool.get_methods():
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


# --- Tool Context Caching ---

_CACHE_EXCLUDE_TOOLS = {"schedule"}


def _cache_tool_contexts(memory: PersonaMemory, tool_contexts: dict[str, str]):
    """Cache tool contexts in tool_state for user conversations."""
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

    Runs the full 8-step cycle: housekeeping, load state, perceive,
    check proposals, reason, validate + act, update state, safety net.
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
    trigger_context = _parse_trigger_context(trigger)
    trigger_purpose = trigger_context.get("purpose", "Scheduled check-in")
    trigger_tools = trigger_context.get("tools", [])
    trigger_time = trigger.get("fire_at")
    is_planning = len(trigger_tools) == 0

    narrative_state = memory.get_narrative()
    schedule_config = memory.get_schedule_config()
    pending_proposals = memory.get_pending_proposals()
    actions_today = memory.get_today_action_count()
    max_actions = 25
    if schedule_config:
        max_actions = schedule_config.get("max_actions_per_day", 25)

    # --- Step 2: Perceive ---
    tool_contexts = {}
    if is_planning:
        for name, tool in tools.items():
            if tool.enabled:
                try:
                    ctx = tool.get_context()
                    if ctx is not None:
                        tool_contexts[name] = ctx
                except Exception as e:
                    logger.error(f"Tool '{name}' get_context() failed: {e}")
    else:
        for name in trigger_tools:
            if name in tools and tools[name].enabled:
                try:
                    ctx = tools[name].get_context()
                    if ctx is not None:
                        tool_contexts[name] = ctx
                except Exception as e:
                    logger.error(f"Tool '{name}' get_context() failed: {e}")

    _cache_tool_contexts(memory, tool_contexts)

    if "schedule" in tools:
        schedule_plan = tools["schedule"].get_context()
    else:
        schedule_plan = None

    # --- Step 3: Check Proposals ---
    # TODO: Scan recent messages for approval keywords.

    # --- Step 4: Reason ---
    available_actions = _format_available_actions(tools)

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

    _, messages = assemble_context(persona_prompt, memory, device=DEVICE_TELEGRAM)
    messages.append({
        "role": "user",
        "content": f"[Agent cycle: {trigger_purpose}]",
    })

    logger.info(f"Cycle {cycle_id}: reasoning about: {trigger_purpose}")

    try:
        llm_response = brain.ask(
            messages,
            system=system_prompt,
            max_tokens=AGENT_REASONING_MAX_TOKENS,
            task="reasoning",
        )
    except Exception as e:
        logger.error(f"Cycle {cycle_id}: reasoning failed: {e}")
        memory.add_reasoning_log(
            cycle_id=cycle_id, trigger_id=trigger_id,
            trigger_purpose=trigger_purpose, narrative_in=narrative_state,
            tool_contexts=json.dumps(tool_contexts),
            skipped=True, skip_reason=f"LLM call failed: {e}",
            provider=config.DEFAULT_PROVIDER,
        )
        _ensure_future_plan(memory, tools, schedule_config)
        return False

    # --- Parse the response ---
    reasoning = _extract_tag(llm_response, "reasoning") or ""
    actions_text = _extract_tag(llm_response, "actions") or "[]"
    narrative_out = _extract_tag(llm_response, "narrative_state") or ""

    # Detect truncated responses
    truncated = _is_response_truncated(llm_response)
    if truncated:
        logger.warning(
            f"Cycle {cycle_id}: response appears truncated "
            f"(missing required tags). Length: {len(llm_response)} chars"
        )
        memory.add_reasoning_log(
            cycle_id=cycle_id, trigger_id=trigger_id,
            trigger_purpose=trigger_purpose,
            tool_contexts=json.dumps(tool_contexts) if tool_contexts else None,
            narrative_in=narrative_state, llm_response=llm_response,
            skipped=True, skip_reason="Response truncated — missing required tags",
            provider=config.DEFAULT_PROVIDER,
        )
        _ensure_future_plan(memory, tools, schedule_config)
        return False

    # Parse actions as JSON
    parsed_actions, parse_error = _parse_actions_json(actions_text)
    if parse_error:
        logger.warning(f"Cycle {cycle_id}: action parse warning: {parse_error}")

    logger.info(
        f"Cycle {cycle_id}: reasoning complete, "
        f"{len(parsed_actions)} action(s) to process"
    )

    # --- Step 5: Validate + Act (unified for ALL tools including schedule) ---
    actions_taken = []

    for action in parsed_actions:
        valid, reason = _validate_action(action, tools, actions_today, max_actions)

        if valid:
            tool = tools[action["tool"]]
            kwargs = action["parameters"] if isinstance(action.get("parameters"), dict) else {}

            try:
                result = tool.execute(action["method"], **kwargs)
                memory.add_agent_action(
                    cycle_id=cycle_id,
                    tool_name=action["tool"],
                    method_name=action["method"],
                    tier=_get_method_tier(tool, action["method"]),
                    parameters=json.dumps(kwargs),
                    result=result,
                    status="completed",
                )
                actions_taken.append({
                    "tool": action["tool"],
                    "method": action["method"],
                    "status": "completed",
                    "result": result,
                })
                # Update running budget count for message/draft tiers
                tier = _get_method_tier(tool, action["method"])
                if tier in ("message", "draft"):
                    actions_today += 1

                # Handle async Telegram sends
                if isinstance(tool, TelegramTool) and send_fn:
                    msg = tool.get_pending_message()
                    if msg:
                        try:
                            await send_fn(msg)
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
                    parameters=json.dumps(kwargs),
                    result=str(e),
                    status="failed",
                )
                actions_taken.append({
                    "tool": action["tool"],
                    "method": action["method"],
                    "status": "failed",
                    "reason": str(e),
                })

        elif reason == "needs_approval":
            kwargs = action.get("parameters", {})
            memory.add_agent_action(
                cycle_id=cycle_id,
                tool_name=action["tool"],
                method_name=action["method"],
                tier="execute",
                parameters=json.dumps(kwargs),
                status="pending_approval",
            )
            actions_taken.append({
                "tool": action["tool"],
                "method": action["method"],
                "status": "pending_approval",
            })
            logger.info(f"Cycle {cycle_id}: proposal stored: {action['tool']}.{action['method']}")

        else:
            memory.add_agent_action(
                cycle_id=cycle_id,
                tool_name=action.get("tool", "unknown"),
                method_name=action.get("method", "unknown"),
                tier="unknown",
                parameters=json.dumps(action.get("parameters", {})),
                result=reason,
                status="failed",
            )
            actions_taken.append({
                "tool": action.get("tool", "unknown"),
                "method": action.get("method", "unknown"),
                "status": "failed",
                "reason": reason,
            })
            logger.warning(f"Cycle {cycle_id}: action rejected: {reason}")

    # --- Step 6: Update State ---
    if narrative_out:
        memory.set_narrative(narrative_out, cycle_id)

    memory.add_reasoning_log(
        cycle_id=cycle_id, trigger_id=trigger_id,
        trigger_purpose=trigger_purpose,
        tool_contexts=json.dumps(tool_contexts) if tool_contexts else None,
        narrative_in=narrative_state, llm_response=llm_response,
        actions_taken=json.dumps(actions_taken) if actions_taken else None,
        narrative_out=narrative_out, skipped=False,
        provider=config.DEFAULT_PROVIDER,
    )

    # --- Step 7: Ensure future plan exists ---
    _ensure_future_plan(memory, tools, schedule_config)

    logger.info(
        f"Cycle {cycle_id} complete: {len(actions_taken)} action(s)"
    )

    return True


# --- Helper Functions ---

def _is_response_truncated(llm_response: str) -> bool:
    """
    Detect if an agent reasoning response was truncated.

    A complete response has all three required tags: reasoning, actions,
    and narrative_state. If actions or narrative_state closing tags are
    missing, the response was likely truncated.
    """
    has_actions = "</actions>" in llm_response
    has_narrative = "</narrative_state>" in llm_response
    return not (has_actions and has_narrative)


def _parse_trigger_context(trigger: dict) -> dict:
    """Parse a trigger's context field into a structured dict."""
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
        return {"purpose": context_str, "tools": []}


def _get_method_tier(tool: Tool | None, method_name: str) -> str:
    """Look up a method's tier on a tool. Returns 'unknown' if not found."""
    if tool is None:
        return "unknown"
    for method in tool.get_methods():
        if method.name == method_name:
            return method.tier
    return "unknown"


def _ensure_future_plan(
    memory: PersonaMemory,
    tools: dict[str, Tool],
    schedule_config: dict | None,
):
    """
    Ensure a future planning cycle exists. Seeds one for tomorrow's
    start_time if none found, and deduplicates if multiple exist.
    """
    if not schedule_config:
        return

    if not memory.has_future_planning_cycle():
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
        logger.info(f"Safety net: seeded planning cycle for {tomorrow_start}")

    _deduplicate_planning_cycles(memory)


def _deduplicate_planning_cycles(memory: PersonaMemory):
    """Remove duplicate planning cycles for the same day."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active = memory.get_active_triggers()

    by_date: dict[str, list[dict]] = {}
    for trigger in active:
        if trigger["fire_at"] <= now_str:
            continue
        try:
            ctx = json.loads(trigger["context"]) if trigger["context"] else {}
            if len(ctx.get("tools", [])) != 0:
                continue
        except (json.JSONDecodeError, TypeError):
            continue
        date = trigger["fire_at"][:10]
        if date not in by_date:
            by_date[date] = []
        by_date[date].append(trigger)

    for date, cycles in by_date.items():
        if len(cycles) <= 1:
            continue
        cycles.sort(key=lambda t: t["fire_at"])
        for duplicate in cycles[1:]:
            memory.delete_trigger(duplicate["id"])
            logger.info(
                f"Deduplicated planning cycle #{duplicate['id']} "
                f"on {date} (keeping #{cycles[0]['id']})"
            )


# --- Schedule Updates in Conversation Responses ---

def strip_schedule_updates(response: str) -> tuple[str, str | None]:
    """
    Strip <schedule_updates> tags from an LLM response.

    Returns (clean_response, actions_json_str).
    The clean_response has the tags removed and is what the user sees.
    The actions_json_str is the raw JSON string inside the tags.

    If no tags are present, returns (response, None).
    """
    pattern = r"<schedule_updates>(.*?)</schedule_updates>"
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response.strip(), None

    actions_json = match.group(1).strip()

    clean = response[:match.start()] + response[match.end():]
    clean = clean.strip()

    return clean, actions_json


def apply_schedule_updates(actions_json: str, memory: PersonaMemory) -> list[dict]:
    """
    Parse and apply schedule update actions from a user conversation response.

    Uses the same JSON action format as the agent cycle. Actions are
    validated and executed through the ScheduleTool.

    The registered tool names include all known tools, not just the ones
    instantiated in the current process. This allows the CLI to create
    wake-ups that reference Telegram even though TelegramTool isn't loaded.

    Args:
        actions_json: Raw JSON string from inside <schedule_updates> tags.
        memory: The persona's memory instance.

    Returns:
        List of result dicts with 'action', 'status', and optionally
        'result' or 'reason'.
    """
    from tools.schedule_tool import ScheduleTool

    schedule_tool = ScheduleTool(memory)
    results = []

    # Parse the JSON
    parsed, parse_error = _parse_actions_json(actions_json)
    if parse_error:
        logger.warning(f"Failed to parse schedule updates JSON: {parse_error}")
        results.append({
            "action": actions_json[:100],
            "status": "failed",
            "reason": f"parse error: {parse_error}",
        })
        return results

    for action in parsed:
        tool_name = action.get("tool", "schedule")
        method_name = action.get("method", "")
        params = action.get("parameters", {})

        # Only schedule actions are valid in <schedule_updates>
        if tool_name != "schedule":
            results.append({
                "action": f"{tool_name}.{method_name}",
                "status": "rejected",
                "reason": f"only schedule actions allowed in <schedule_updates>, got '{tool_name}'",
            })
            continue

        try:
            result = schedule_tool.execute(method_name, **params)
            logger.info(f"Schedule update applied: {result}")
            results.append({
                "action": f"schedule.{method_name}",
                "status": "applied",
                "result": result,
            })
        except (ValueError, Exception) as e:
            logger.warning(f"Schedule update failed: {e}")
            results.append({
                "action": f"schedule.{method_name}",
                "status": "failed",
                "reason": str(e),
            })

    return results