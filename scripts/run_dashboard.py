"""Run the authenticated dashboard with validated runtime config."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.app import create_app
from dashboard.config import load_dashboard_config, validate_dashboard_config


logger = logging.getLogger(__name__)


def main() -> None:
    config = validate_dashboard_config(load_dashboard_config())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting dashboard on %s:%s (%s exposure)",
        config.host,
        config.port,
        config.exposure,
    )
    uvicorn.run(
        create_app(),
        host=config.host,
        port=config.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
