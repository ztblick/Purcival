"""Run Jo's background scheduler loop without Telegram."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import personas
from memory import PersonaMemory
from proactive import ensure_agent_has_plan, start_scheduler


logger = logging.getLogger(__name__)


async def log_send(text: str) -> None:
    """Log background agent messages instead of routing them to Telegram."""
    logger.info("Background agent message: %s", text)


async def run_loop(persona_name: str) -> None:
    if not personas.persona_exists(persona_name):
        raise RuntimeError(f"Persona '{persona_name}' does not exist")

    memory = PersonaMemory(persona_name)
    persona_prompt = personas.load_persona(persona_name)
    ensure_agent_has_plan(memory)
    scheduler = start_scheduler(memory, log_send, persona_prompt)
    logger.info("Agent loop started for persona '%s'", persona_name)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Agent loop stopped for persona '%s'", persona_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Purcival agent loop")
    parser.add_argument("--persona", default="jo", help="Persona to schedule")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_loop(args.persona))


if __name__ == "__main__":
    main()
