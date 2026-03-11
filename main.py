"""
Main entry point for the assistant.

Run with:
    python main.py                          — pick persona interactively
    python main.py --persona purcival       — start as Purcival
    python main.py --provider claude        — use Claude instead of Ollama
    python main.py -m "hello"               — single message, then exit
"""

import argparse
import brain
import config
import personas

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


def _print_banner(provider: str, persona_name: str):
    """Print the startup banner."""
    banner = (
        "[bold]Personal Assistant — Stage 1[/bold]\n"
        f"Persona:  [persona]{persona_name}[/persona]\n"
        f"Provider: [assistant]{provider}[/assistant]\n"
        "\n"
        "[status]Commands:[/status]\n"
        "  /claude   — switch to Claude\n"
        "  /ollama   — switch to Ollama\n"
        "  /persona  — switch persona (clears history)\n"
        "  /status   — show current state\n"
        "  clear     — reset conversation\n"
        "  quit      — exit"
    )
    console.print(Panel(banner, border_style="dim"))
    console.print()


def chat_loop(provider: str, persona_name: str):
    """
    Interactive conversation in the terminal.

    Maintains a running message history and the active persona's
    system prompt. Switching personas clears history, since a new
    personality shouldn't inherit a conversation that was shaped
    by a different one.
    """
    system_prompt = personas.load_persona(persona_name)
    _print_banner(provider, persona_name)

    history: list[dict] = []

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
            history.clear()
            console.print("[system]— conversation cleared —[/system]\n")
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
            system_prompt = personas.load_persona(persona_name)
            history.clear()
            console.print(
                f"[system]— now talking to [persona]{persona_name}[/persona] "
                f"(history cleared) —[/system]\n"
            )
            continue

        # --- Status ---
        if user_input.lower() == "/status":
            model = (config.CLAUDE_MODEL if provider == "claude"
                     else config.OLLAMA_MODEL)
            console.print(f"  [status]Persona:[/status]  [persona]{persona_name}[/persona]")
            console.print(f"  [status]Provider:[/status] [assistant]{provider}[/assistant]")
            console.print(f"  [status]Model:[/status]    {model}")
            console.print(f"  [status]History:[/status]  {len(history)} messages\n")
            continue

        # --- Send message ---
        history.append({"role": "user", "content": user_input})

        with console.status("[status]Thinking...[/status]", spinner="dots"):
            try:
                response = brain.ask(
                    history,
                    system=system_prompt,
                    provider=provider,
                )
            except Exception as e:
                console.print(f"\n[error]✗ Error ({provider}): {e}[/error]\n")
                history.pop()
                continue

        history.append({"role": "assistant", "content": response})

        console.print()
        _print_response(response, provider, persona_name)
        console.print()


def single_message(message: str, provider: str, persona_name: str):
    """Send one message and print the response."""
    system_prompt = personas.load_persona(persona_name)
    with console.status("[status]Thinking...[/status]", spinner="dots"):
        response = brain.ask(
            [{"role": "user", "content": message}],
            system=system_prompt,
            provider=provider,
        )
    _print_response(response, provider, persona_name)


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
        help="Persona to use (e.g. percival, jocelyn, default)",
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
        chat_loop(args.provider, persona_name)
