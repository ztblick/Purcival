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


def _print_banner(provider: str, persona_name: str, memory: PersonaMemory, debug: bool = False):
    """Print the startup banner."""
    total = memory.get_message_count()
    memory_status = f"{total} messages in memory" if total > 0 else "fresh start"

    banner = (
        "[bold]Personal Assistant — Stage 3[/bold]\n"
        f"Persona:  [persona]{persona_name}[/persona]\n"
        f"Provider: [assistant]{provider}[/assistant]\n"
        f"Memory:   [status]{memory_status}[/status]\n"
        f"Debug:    [status]{'ON' if debug else 'off'}[/status]\n"
        "\n"
        "[status]Commands:[/status]\n"
        "  /claude   — switch to Claude\n"
        "  /ollama   — switch to Ollama\n"
        "  /persona  — switch persona\n"
        "  /status   — show current state\n"
        "  /debug    — toggle prompt dumping to debug/\n"
        "  clear     — reset conversation (erases memory!)\n"
        "  quit      — exit"
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

        # --- Status ---
        if user_input.lower() == "/status":
            model = (config.CLAUDE_MODEL if provider == "claude"
                     else config.OLLAMA_MODEL)
            total = memory.get_message_count()
            summaries = len(memory.get_all_summaries())
            console.print(f"  [status]Persona:[/status]   [persona]{persona_name}[/persona]")
            console.print(f"  [status]Provider:[/status]  [assistant]{provider}[/assistant]")
            console.print(f"  [status]Model:[/status]     {model}")
            console.print(f"  [status]Messages:[/status]  {total} stored")
            console.print(f"  [status]Summaries:[/status] {summaries}")
            console.print(f"  [status]Debug:[/status]     {'ON' if debug else 'off'}\n")
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