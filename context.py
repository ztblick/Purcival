"""
Context assembly — builds the full prompt for each LLM call.

This module is the single place where all context sources are combined
into the system prompt and messages array that brain.ask() receives.

The system prompt is assembled from sections, each with a token budget:
    1. Persona prompt          (< 2,000 tokens)
    2. User context            (< 2,000 tokens)
    3. Current session         (< 500 tokens)
    4. Scheduled plan          (< 2,000 tokens)
    5. Conversation summaries  (< 8,000 tokens)
    6. Tool context            (< 8,000 tokens)

The messages array contains recent verbatim messages (< 8,000 tokens).
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from memory import PersonaMemory
from tokens import get_token_count

logger = logging.getLogger(__name__)

DEVICE_TERMINAL = "terminal"
DEVICE_TELEGRAM = "telegram"

BUDGET_PERSONA = 2_000
BUDGET_USER_CONTEXT = 2_000
BUDGET_SCHEDULED_PLAN = 2_000
BUDGET_SUMMARIES = 8_000
BUDGET_ADDITIONAL = 8_000
BUDGET_MESSAGES = 8_000

SUMMARY_TOP_K = 8
SUMMARY_MIN_SIMILARITY = 0.35
SUMMARY_ALWAYS_RECENT = 2

DATA_DIR = Path(__file__).parent / "data"
USER_CONTEXT_PATH = DATA_DIR / "user_context.md"


def _load_user_context() -> str:
    if not USER_CONTEXT_PATH.exists():
        return ""
    return USER_CONTEXT_PATH.read_text().strip()


def _load_session_context(device: str) -> str:
    now = datetime.now()
    timestamp = now.strftime("%A, %B %d, %Y at %I:%M %p")
    parts = [f"Current date and time: {timestamp}"]
    if device == DEVICE_TELEGRAM:
        parts.append(
            "The user is messaging from their phone via Telegram. "
            "Keep responses concise and scannable — short paragraphs, "
            "direct answers. Avoid long explanations unless asked."
        )
    elif device == DEVICE_TERMINAL:
        parts.append(
            "The user is at their computer using the terminal interface. "
            "Longer, more detailed responses are welcome when appropriate."
        )
    return "\n\n".join(parts)


def _load_scheduled_plan(memory: PersonaMemory) -> str:
    """
    Load the agent's upcoming scheduled wake-ups for inclusion in
    the user conversation context.

    Instructs the LLM to use JSON format inside <schedule_updates>
    tags, matching the unified action format used in agent cycles.
    """
    schedule = memory.get_schedule_config()
    if not schedule:
        return ""

    active = memory.get_active_triggers()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    future = [t for t in active if t["fire_at"] > now_str]
    if not future:
        return ""

    lines = [
        "You have an active schedule of planned wake-ups. If the user's "
        "message affects any of these plans, include schedule updates in "
        "your response using <schedule_updates> tags. Otherwise, respond "
        "normally without mentioning your schedule.\n",
        "YOUR SCHEDULED PLAN:",
    ]

    for t in future[:15]:
        purpose = ""
        try:
            ctx = json.loads(t["context"]) if t["context"] else {}
            purpose = ctx.get("purpose", t.get("context", ""))
        except (json.JSONDecodeError, TypeError):
            purpose = t.get("context", "")

        fire_time = t["fire_at"]
        try:
            fire_dt = datetime.strptime(fire_time, "%Y-%m-%d %H:%M:%S")
            if fire_dt.date() == now.date():
                time_display = f"Today {fire_dt.strftime('%H:%M')}"
            else:
                time_display = fire_dt.strftime("%a %m/%d %H:%M")
        except ValueError:
            time_display = fire_time

        lines.append(f"  #{t['id']}  {time_display}  — {purpose}")

    lines.append(
        "\nTo update your plan, append <schedule_updates> tags after your "
        "response with a JSON array of schedule actions. Examples:\n"
        "<schedule_updates>\n"
        '[{"tool": "schedule", "method": "modify_wakeup", "parameters": '
        '{"id": 42, "time": "2026-03-16 14:00", "purpose": "New purpose"}},\n'
        ' {"tool": "schedule", "method": "cancel_wakeup", "parameters": {"id": 43}},\n'
        ' {"tool": "schedule", "method": "add_wakeup", "parameters": '
        '{"time": "2026-03-16 15:00", "purpose": "New task", "tools": ["telegram"]}}]\n'
        "</schedule_updates>"
    )

    return "\n".join(lines)


def _load_summaries(memory: PersonaMemory, current_message: str) -> str:
    all_summaries = memory.get_all_summaries()
    if not all_summaries:
        return ""

    semantic_results = []
    if current_message.strip():
        try:
            from embeddings import get_embedding, EMBEDDING_DIM
            query_embedding = get_embedding(current_message)
            search_results = memory.search_summaries(
                query_embedding, top_k=SUMMARY_TOP_K, embedding_dim=EMBEDDING_DIM,
            )
            semantic_results = [r for r in search_results if r["similarity"] >= SUMMARY_MIN_SIMILARITY]
        except Exception as e:
            logger.warning(f"Summary embedding search failed: {e}")

    recent_results = []
    if SUMMARY_ALWAYS_RECENT > 0:
        for s in all_summaries[-SUMMARY_ALWAYS_RECENT:]:
            recent_results.append({
                "id": s["id"], "summary": s["summary"],
                "created_at": s["created_at"], "similarity": None,
            })

    seen_ids = set()
    merged = []
    for r in semantic_results:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            merged.append(r)
    for r in recent_results:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            merged.append(r)

    if not merged:
        return ""

    parts = []
    for r in merged:
        sim_note = f" (relevance: {r['similarity']:.2f})" if r["similarity"] is not None else ""
        date = r.get("created_at", "unknown date")
        parts.append(f"[{date}]{sim_note}\n{r['summary']}")

    return "\n\n---\n\n".join(parts)


def _load_tool_contexts(memory: PersonaMemory) -> str:
    tool_names = _get_cached_tool_names(memory)
    if not tool_names:
        return ""

    now = datetime.now()
    sections = []

    for tool_name in tool_names:
        cached = memory.get_tool_state(tool_name, "cached_context")
        cached_at = memory.get_tool_state(tool_name, "cached_context_at")
        if not cached:
            continue

        filtered = _filter_past_events(cached, now)
        if not filtered or not filtered.strip():
            continue

        freshness = ""
        if cached_at:
            try:
                cached_dt = datetime.strptime(cached_at, "%Y-%m-%d %H:%M:%S")
                age_minutes = int((now - cached_dt).total_seconds() / 60)
                if age_minutes < 2:
                    freshness = "(just updated)"
                elif age_minutes < 60:
                    freshness = f"(updated {age_minutes} minutes ago)"
                elif age_minutes < 1440:
                    hours = age_minutes // 60
                    freshness = f"(updated {hours} hour{'s' if hours != 1 else ''} ago)"
                else:
                    freshness = f"(updated at {cached_at})"
            except ValueError:
                freshness = f"(updated at {cached_at})"

        display_name = tool_name.upper().replace("_", " ")
        sections.append(f"### {display_name} {freshness}\n{filtered}")

    if not sections:
        return ""
    return "\n\n".join(sections)


def _get_cached_tool_names(memory: PersonaMemory) -> list[str]:
    conn = memory._connect()
    try:
        rows = conn.execute("SELECT DISTINCT tool_name FROM tool_state WHERE key = 'cached_context'").fetchall()
        return [row["tool_name"] for row in rows]
    finally:
        conn.close()


def _filter_past_events(context: str, now: datetime) -> str:
    if not context:
        return ""

    lines = context.split("\n")
    filtered_lines = []
    in_upcoming_section = False
    in_allday_section = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("UPCOMING"):
            in_upcoming_section = True
            in_allday_section = False
            filtered_lines.append(line)
            continue
        elif stripped.startswith("ALL DAY"):
            in_allday_section = True
            in_upcoming_section = False
            filtered_lines.append(line)
            continue
        elif stripped.startswith(("IMMINENT", "NEW", "CHANGED", "CANCELLED")):
            in_upcoming_section = False
            in_allday_section = False
            if stripped.startswith("IMMINENT"):
                continue
            filtered_lines.append(line)
            continue

        if in_allday_section and stripped:
            filtered_lines.append(line)
            continue

        if in_upcoming_section and stripped:
            end_time = _extract_end_time(stripped, now)
            if end_time is not None:
                if end_time <= now:
                    continue
            filtered_lines.append(line)
            continue

        filtered_lines.append(line)

    result = "\n".join(filtered_lines)
    result = result.replace("UPCOMING:\n\n", "").replace("UPCOMING:\n", "")
    if result.strip() == "UPCOMING:":
        return ""
    return result.strip()


def _extract_end_time(line: str, now: datetime) -> datetime | None:
    import re
    pattern = r'(\d{1,2}:\d{2}\s*[AP]M)\s*[–\-]\s*(\d{1,2}:\d{2}\s*[AP]M)'
    match = re.search(pattern, line, re.IGNORECASE)
    if not match:
        return None

    start_time_str = match.group(1).strip()
    end_time_str = match.group(2).strip()
    today = now.date()

    try:
        start_parsed = datetime.strptime(start_time_str, "%I:%M %p")
        end_parsed = datetime.strptime(end_time_str, "%I:%M %p")
        start_dt = datetime(today.year, today.month, today.day, start_parsed.hour, start_parsed.minute)
        end_dt = datetime(today.year, today.month, today.day, end_parsed.hour, end_parsed.minute)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)
        return end_dt
    except ValueError:
        return None


def _truncate_to_budget(text: str, budget: int) -> str:
    if not text:
        return text
    token_count = get_token_count(text)
    if token_count <= budget:
        return text
    char_limit = budget * 4
    truncated = text[:char_limit].rsplit(" ", 1)[0]
    return truncated + "\n\n[... content truncated to fit token budget ...]"


def _build_system_prompt(persona_prompt, memory, current_message, device):
    sections = [
        ("PERSONA", persona_prompt, BUDGET_PERSONA),
        ("ABOUT THE USER", _load_user_context(), BUDGET_USER_CONTEXT),
        ("CURRENT SESSION", _load_session_context(device), 500),
        ("YOUR SCHEDULED PLAN", _load_scheduled_plan(memory), BUDGET_SCHEDULED_PLAN),
        ("RELEVANT PAST CONVERSATIONS", _load_summaries(memory, current_message), BUDGET_SUMMARIES),
        ("TOOL CONTEXT", _load_tool_contexts(memory), BUDGET_ADDITIONAL),
    ]
    parts = []
    for label, content, budget in sections:
        if not content:
            continue
        truncated = _truncate_to_budget(content, budget)
        parts.append(f"## {label}\n\n{truncated}")
    return "\n\n---\n\n".join(parts)


def _build_messages(memory, max_tokens=BUDGET_MESSAGES):
    all_recent = memory.get_recent_messages(limit=200)
    if not all_recent:
        return []
    selected = []
    running_tokens = 0
    for msg in reversed(all_recent):
        if msg["role"] == "user" and msg.get("created_at"):
            content = f"[{msg['created_at']}] {msg['content']}"
        else:
            content = msg["content"]
        msg_tokens = get_token_count(content)
        if running_tokens + msg_tokens > max_tokens:
            break
        selected.append({"role": msg["role"], "content": content})
        running_tokens += msg_tokens
    selected.reverse()
    return selected


def assemble_context(persona_prompt, memory, device=DEVICE_TERMINAL):
    messages = _build_messages(memory)
    current_message = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            current_message = msg["content"]
            break
    system_prompt = _build_system_prompt(persona_prompt, memory, current_message, device)
    return system_prompt, messages