"""Standalone Redis-backed access job worker."""

from __future__ import annotations

import asyncio

from src.access_jobs import run_access_job_worker
from src.config import get_settings
from src.logging_config import get_logger, setup_logging

logger = get_logger("access_job_worker")
settings = get_settings()


async def _main() -> None:
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info("Starting access job worker")
    await run_access_job_worker()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Access job worker stopped")


if __name__ == "__main__":
    main()
