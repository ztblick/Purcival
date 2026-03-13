"""
Persona manager — loads personality files from the personas/ directory.

Each persona is a markdown file that contains a system prompt. The filename
(without extension) becomes the persona's name.

    personas/
    ├── percival.md    →  persona name: "percival"
    ├── ada.md         →  persona name: "ada"
    └── default.md     →  persona name: "default"

To create a new persona, just drop a new .md file in the personas/ directory.
No code changes needed — the app discovers them automatically at startup.

The file format is simple: everything in the file becomes the system prompt.
Use markdown formatting for your own readability — the LLM will interpret
the structure (headers, bullet points, etc.) as part of its instructions.
"""

from pathlib import Path

# Persona files live next to this script in a personas/ directory
PERSONAS_DIR = Path(__file__).parent / "personas"


def list_personas() -> list[str]:
    """
    Return the names of all available personas.

    Scans the personas/ directory for .md files and returns their
    names (without extension), sorted alphabetically.
    """
    if not PERSONAS_DIR.exists():
        return []
    return sorted(
        p.stem for p in PERSONAS_DIR.glob("*.md")
    )


def load_persona(name: str) -> str:
    """
    Load a persona's system prompt by name.

    Args:
        name: The persona name (filename without .md extension)

    Returns:
        The full contents of the persona file as a string.

    Raises:
        FileNotFoundError: If no persona file exists with that name.
    """
    path = PERSONAS_DIR / f"{name}.md"
    if not path.exists():
        available = list_personas()
        raise FileNotFoundError(
            f"Persona '{name}' not found. "
            f"Available: {', '.join(available) or '(none)'}\n"
            f"Create one at: {PERSONAS_DIR / f'{name}.md'}"
        )
    return path.read_text().strip()


def persona_exists(name: str) -> bool:
    """Check whether a persona file exists."""
    return (PERSONAS_DIR / f"{name}.md").exists()
