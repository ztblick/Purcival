"""
Main entry point for the assistant.

Run with:
    python main.py                          — pick persona interactively
    python main.py --persona purcival       — start as Purcival
    python main.py --provider claude        — use Claude instead of Ollama
    python main.py -m "hello" --persona jocelyn  — single message, then exit
"""

import argparse
import brain
import config
import personas

from datetime import datetime
from pathlib import Path
from memory import PersonaMemory
from context import assemble_context
from summarizer import check_and_summarize
from proactive import ensure_agent_has_plan
from tokens import get_token_count
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.theme import Theme
from rich.table import Table

# --- Terminal UI Setup ---

_theme = Theme({
    "user": "bold cyan",
    "assistant": "bold green",
    "persona": "bold magenta",
    "system": "dim italic",
    "error": "bold red",
    "status": "dim",
})

console = Console(theme=_theme)

# Debug output directory
DEBUG_DIR = Path(__file__).parent / "debug"


def _dump_prompt(
    system_prompt: str,
    messages: list[dict],
    provider: str,
    persona_name: str,
):
    """
    Write the full assembled prompt to a timestamped file in debug/.

    The file shows exactly what the LLM receives: the complete system
    prompt (with all sections) and the full messages array. Useful for
    inspecting which summaries were selected, how many messages are in
    the window, and the total token count.
    """
    DEBUG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"prompt_{persona_name}_{timestamp}.txt"
    path = DEBUG_DIR / filename

    system_tokens = get_token_count(system_prompt)
    messages_tokens = sum(get_token_count(m["content"]) for m in messages)
    total_tokens = system_tokens + messages_tokens

    lines = [
        f"=== PROMPT DEBUG — {persona_name} via {provider} ===",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"System prompt: ~{system_tokens} tokens",
        f"Messages: {len(messages)} messages, ~{messages_tokens} tokens",
        f"Total: ~{total_tokens} tokens",
        "",
        "=" * 60,
        "SYSTEM PROMPT",
        "=" * 60,
        "",
        system_prompt,
        "",
        "=" * 60,
        f"MESSAGES ({len(messages)})",
        "=" * 60,
        "",
    ]

    for i, msg in enumerate(messages):
        tokens = get_token_count(msg["content"])
        lines.append(f"--- [{i+1}] {msg['role']} (~{tokens} tokens) ---")
        lines.append(msg["content"])
        lines.append("")

    path.write_text("\n".join(lines))
    return path


def _print_response(text: str, provider: str, persona_name: str):
    """Render an assistant response with markdown formatting."""
    md = Markdown(text)
    title = f"[persona]{persona_name}[/persona] [status]via {provider}[/status]"
    panel = Panel(
        md,
        title=title,
        title_align="left",
        border_style="magenta",
        padding=(1, 2),
    )
    console.print(panel)


def _pick_persona() -> str:
    """
    Show available personas and let the user pick one.

    This runs at startup if no --persona flag was provided.
    """
    available = personas.list_personas()

    if not available:
        console.print("[error]No personas found in personas/ directory.[/error]")
        console.print("Create one: personas/default.md")
        raise SystemExit(1)

    console.print()
    table = Table(
        title="[bold]Choose a Persona[/bold]",
        show_header=True,
        header_style="bold",
        border_style="dim",
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="persona")
    table.add_column("Preview", style="status")

    for i, name in enumerate(available, 1):
        # Show first line of the persona file as a preview
        prompt_text = personas.load_persona(name)
        # Skip the markdown header line if it starts with #
        lines = [l for l in prompt_text.split("\n") if l.strip() and not l.startswith("#")]
        preview = lines[0][:60] + "..." if lines else "(empty)"
        table.add_row(str(i), name, preview)

    console.print(table)
    console.print()

    while True:
        choice = console.input("[status]Enter name or number:[/status] ").strip()

        # Accept by number
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                return available[idx]
            console.print(f"[error]Pick 1–{len(available)}[/error]")
            continue

        # Accept by name
        if choice.lower() in available:
            return choice.lower()

        console.print(f"[error]Unknown persona: {choice}[/error]")


def _handle_schedule(memory: PersonaMemory, persona_name: str):
    """
    Interactive schedule configuration for the agent.

    Prompts the user for wake time (first planning cycle), sleep time
    (last allowed wake-up), and daily action limit. Saves to the
    database and bootstraps the agent's first planning cycle.

    The running Telegram service will pick up the new config on its
    next trigger cycle — no restart needed for schedule changes.
    Changing the schedule also clears old recurring triggers.
    """
    # Show current config if one exists
    current = memory.get_schedule_config()
    if current:
        max_actions = current.get("max_actions_per_day", 25)
        actions_today = memory.get_today_action_count()
        console.print(
            f"  [status]Current schedule for[/status] [persona]{persona_name}[/persona][status]:[/status]\n"
            f"    Wake time:       {current['start_time']}\n"
            f"    Sleep time:      {current['end_time']}\n"
            f"    Daily actions:   {max_actions} (used today: {actions_today})\n"
            f"    Last updated:    {current['updated_at']}\n"
        )
    else:
        console.print(
            f"  [status]No schedule configured for[/status] "
            f"[persona]{persona_name}[/persona]\n"
        )

    # --- Collect wake time ---
    while True:
        start_input = console.input(
            "[status]Wake time — first planning cycle (HH:MM, 24hr) or 'cancel':[/status] "
        ).strip()

        if start_input.lower() == "cancel":
            console.print("[system]— schedule unchanged —[/system]\n")
            return

        try:
            start_h, start_m = map(int, start_input.split(":"))
            if not (0 <= start_h <= 23 and 0 <= start_m <= 59):
                raise ValueError
            start_time = f"{start_h:02d}:{start_m:02d}"
            break
        except (ValueError, AttributeError):
            console.print("[error]Enter time as HH:MM (e.g. 06:00, 07:30)[/error]")

    # --- Collect sleep time ---
    while True:
        end_input = console.input(
            "[status]Sleep time — no wake-ups after (HH:MM, 24hr):[/status] "
        ).strip()

        try:
            end_h, end_m = map(int, end_input.split(":"))
            if not (0 <= end_h <= 23 and 0 <= end_m <= 59):
                raise ValueError
            end_time = f"{end_h:02d}:{end_m:02d}"

            # Validate end is after start
            if (end_h * 60 + end_m) <= (start_h * 60 + start_m):
                console.print("[error]Sleep time must be after wake time[/error]")
                continue
            break
        except (ValueError, AttributeError):
            console.print("[error]Enter time as HH:MM (e.g. 22:00, 23:00)[/error]")

    # --- Collect daily action limit ---
    while True:
        limit_input = console.input(
            "[status]Max actions per day (default 25):[/status] "
        ).strip()

        if not limit_input:
            max_actions = 25
            break

        try:
            max_actions = int(limit_input)
            if max_actions < 1:
                raise ValueError
            break
        except ValueError:
            console.print("[error]Enter a positive number (e.g. 25, 50)[/error]")

    # --- Save and apply ---
    # interval_minutes is preserved for backward compat but not used
    # by the self-scheduling agent
    memory.set_schedule_config(start_time, end_time, 30, max_actions)
    memory.clear_agent_triggers()

    # Bootstrap the agent's first planning cycle
    ensure_agent_has_plan(memory)

    # Count what was created
    active = memory.get_active_triggers()
    trigger_count = len([t for t in active if not t.get("fired")])

    console.print(
        f"\n  [system]Schedule updated for[/system] [persona]{persona_name}[/persona]\n"
        f"    Wake: {start_time}  Sleep: {end_time}\n"
        f"    Max actions/day: {max_actions}\n"
        f"    {trigger_count} trigger(s) seeded — agent will plan its own day\n"
    )


def _print_banner(provider: str, persona_name: str, memory: PersonaMemory, debug: bool = False):
    """Print the startup banner."""
    total = memory.get_message_count()
    memory_status = f"{total} messages in memory" if total > 0 else "fresh start"

    # Show schedule status
    schedule = memory.get_schedule_config()
    if schedule:
        max_actions = schedule.get("max_actions_per_day", 25)
        actions_today = memory.get_today_action_count()
        schedule_status = (
            f"{schedule['start_time']}–{schedule['end_time']}, "
            f"max {max_actions} actions/day ({actions_today} used)"
        )
    else:
        schedule_status = "not configured"

    # Show narrative state snippet
    narrative = memory.get_narrative()
    agent_status = ""
    if narrative:
        snippet = narrative[:80] + "..." if len(narrative) > 80 else narrative
        agent_status = f"\nAgent:    [status]{snippet}[/status]"

    banner = (
        "[bold]Personal Assistant — Stage 5[/bold]\n"
        f"Persona:  [persona]{persona_name}[/persona]\n"
        f"Provider: [assistant]{provider}[/assistant]\n"
        f"Memory:   [status]{memory_status}[/status]\n"
        f"Schedule: [status]{schedule_status}[/status]"
        f"{agent_status}\n"
        f"Debug:    [status]{'ON' if debug else 'off'}[/status]\n"
        "\n"
        "[status]Commands:[/status]\n"
        "  /claude    — switch to Claude\n"
        "  /ollama    — switch to Ollama\n"
        "  /persona   — switch persona\n"
        "  /schedule  — configure agent wake/sleep times & action limit\n"
        "  /status    — show current state\n"
        "  /debug     — toggle prompt dumping to debug/\n"
        "  clear      — reset conversation (erases memory!)\n"
        "  quit       — exit"
    )
    console.print(Panel(banner, border_style="dim"))
    console.print()


def chat_loop(provider: str, persona_name: str, debug: bool = False):
    """
    Interactive conversation in the terminal.

    Messages are persisted to the persona's database. Switching personas
    loads a different database — each persona has its own memory.
    The 'clear' command wipes the current persona's entire history.
    """
    persona_prompt = personas.load_persona(persona_name)
    memory = PersonaMemory(persona_name)
    _print_banner(provider, persona_name, memory, debug)

    while True:
        try:
            user_input = console.input("[user]You:[/user] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[system]Goodbye.[/system]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            console.print("[system]Goodbye.[/system]")
            break

        if user_input.lower() == "clear":
            total = memory.get_message_count()
            summaries = len(memory.get_all_summaries())
            console.print(
                f"[error]This will permanently delete {total} messages "
                f"and {summaries} summaries for {persona_name}.[/error]"
            )
            confirm = console.input(
                "[error]Type 'yes' to confirm:[/error] "
            ).strip().lower()
            if confirm == "yes":
                memory.clear_history()
                console.print("[system]— conversation history erased —[/system]\n")
            else:
                console.print("[system]— clear cancelled —[/system]\n")
            continue

        # --- Provider switching ---
        if user_input.lower() == "/claude":
            provider = "claude"
            console.print(
                f"[system]— switched to [assistant]{provider}[/assistant] —[/system]\n"
            )
            continue

        if user_input.lower() == "/ollama":
            provider = "ollama"
            console.print(
                f"[system]— switched to [assistant]{provider}[/assistant] —[/system]\n"
            )
            continue

        # --- Persona switching ---
        if user_input.lower() == "/persona":
            persona_name = _pick_persona()
            persona_prompt = personas.load_persona(persona_name)
            memory = PersonaMemory(persona_name)
            console.print(
                f"[system]— now talking to [persona]{persona_name}[/persona] —[/system]\n"
            )
            continue

        # --- Schedule configuration ---
        if user_input.lower() == "/schedule":
            _handle_schedule(memory, persona_name)
            continue

        # --- Status ---
        if user_input.lower() == "/status":
            model = (config.CLAUDE_MODEL if provider == "claude"
                     else config.OLLAMA_MODEL)
            total = memory.get_message_count()
            summaries = len(memory.get_all_summaries())
            active_triggers = memory.get_active_triggers()
            schedule = memory.get_schedule_config()
            narrative = memory.get_narrative()

            console.print(f"  [status]Persona:[/status]    [persona]{persona_name}[/persona]")
            console.print(f"  [status]Provider:[/status]   [assistant]{provider}[/assistant]")
            console.print(f"  [status]Model:[/status]      {model}")
            console.print(f"  [status]Messages:[/status]   {total} stored")
            console.print(f"  [status]Summaries:[/status]  {summaries}")

            if schedule:
                max_actions = schedule.get("max_actions_per_day", 25)
                actions_today = memory.get_today_action_count()
                console.print(
                    f"  [status]Schedule:[/status]   "
                    f"{schedule['start_time']}–{schedule['end_time']}, "
                    f"max {max_actions} actions/day"
                )
                console.print(
                    f"  [status]Actions:[/status]    "
                    f"{actions_today}/{max_actions} used today"
                )
            else:
                console.print(f"  [status]Schedule:[/status]   not configured")

            console.print(f"  [status]Triggers:[/status]   {len(active_triggers)} pending")

            if narrative:
                snippet = narrative[:120] + "..." if len(narrative) > 120 else narrative
                console.print(f"  [status]Agent:[/status]      {snippet}")

            console.print(f"  [status]Debug:[/status]      {'ON' if debug else 'off'}\n")
            continue

        # --- Debug toggle ---
        if user_input.lower() == "/debug":
            debug = not debug
            state = "ON — prompts will be saved to debug/" if debug else "off"
            console.print(f"[system]— debug {state} —[/system]\n")
            continue

        # --- Send message ---
        # Persist user message first
        memory.add_message("user", user_input)

        # Assemble full context: system prompt + recent messages
        system_prompt, messages = assemble_context(persona_prompt, memory)

        # Dump the full prompt to a file if debug is on
        if debug:
            path = _dump_prompt(system_prompt, messages, provider, persona_name)
            console.print(f"[status]  Debug: prompt saved to {path}[/status]")

        with console.status("[status]Thinking...[/status]", spinner="dots"):
            try:
                response = brain.ask(
                    messages,
                    system=system_prompt,
                    provider=provider,
                )
            except Exception as e:
                console.print(f"\n[error]✗ Error ({provider}): {e}[/error]\n")
                continue

        # Persist assistant response
        memory.add_message("assistant", response)

        console.print()
        _print_response(response, provider, persona_name)
        console.print()

        # Check if older messages need summarization
        try:
            with console.status(
                "[status]Committing conversation to memory...[/status]",
                spinner="dots",
            ):
                count = check_and_summarize(memory)
            if count > 0:
                label = "summary" if count == 1 else "summaries"
                console.print(
                    f"[status]— {count} {label} stored in memory —[/status]\n"
                )
        except Exception as e:
            console.print(f"[error]Summarization error: {e}[/error]\n")


def single_message(message: str, provider: str, persona_name: str):
    """Send one message and print the response. Also persisted."""
    persona_prompt = personas.load_persona(persona_name)
    memory = PersonaMemory(persona_name)

    # Persist and include history even in single-message mode —
    # this way the persona remembers past single-message interactions too.
    memory.add_message("user", message)

    # Assemble full context
    system_prompt, messages = assemble_context(persona_prompt, memory)

    with console.status("[status]Thinking...[/status]", spinner="dots"):
        response = brain.ask(
            messages,
            system=system_prompt,
            provider=provider,
        )

    memory.add_message("assistant", response)
    _print_response(response, provider, persona_name)

    # Check if summarization is needed
    try:
        check_and_summarize(memory)
    except Exception:
        pass  # Silent in single-message mode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Personal AI Assistant")
    parser.add_argument(
        "-m", "--message",
        type=str,
        help="Send a single message instead of entering chat mode",
    )
    parser.add_argument(
        "-p", "--provider",
        type=str,
        choices=["claude", "ollama"],
        default=config.DEFAULT_PROVIDER,
        help="LLM provider to use (default: from .env)",
    )
    parser.add_argument(
        "--persona",
        type=str,
        default=None,
        help="Persona to use (e.g. purcival, ada, default)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable prompt dumping — saves full prompts to debug/",
    )
    args = parser.parse_args()

    # Determine persona — from flag, env, or interactive picker
    if args.persona:
        if not personas.persona_exists(args.persona):
            console.print(f"[error]Persona '{args.persona}' not found.[/error]")
            console.print(f"Available: {', '.join(personas.list_personas())}")
            raise SystemExit(1)
        persona_name = args.persona
    elif args.message:
        # Non-interactive mode — use default persona
        persona_name = config.DEFAULT_PERSONA
    else:
        # Interactive mode — let user pick
        persona_name = _pick_persona()

    if args.message:
        single_message(args.message, args.provider, persona_name)
    else:
        chat_loop(args.provider, persona_name, debug=args.debug)