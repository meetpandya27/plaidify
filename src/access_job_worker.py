"""Standalone Redis-backed access job worker (the "access executor").

Runs dispatched access jobs (ACCESS_JOB_EXECUTION_MODE=redis-worker) and, in
that mode, the background services: scheduled refresh, the webhook outbox and
the stuck-job reaper. Serves /metrics and a /health liveness check on
ACCESS_WORKER_METRICS_PORT.

SIGTERM / SIGINT drain it: it stops taking jobs, gives running ones
ACCESS_JOB_DRAIN_SECONDS to finish, then cancels the rest (they are marked
cancelled and their locks freed), and closes the browser.
"""

from __future__ import annotations

import asyncio
import signal

from src.access_jobs import run_access_job_worker
from src.config import get_settings
from src.logging_config import get_logger, setup_logging

logger = get_logger("access_job_worker")
settings = get_settings()


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop(sig: signal.Signals) -> None:
        if not stop_event.is_set():
            logger.info("Stopping access job worker", extra={"extra_data": {"signal": sig.name}})
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop, sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-Unix
            pass


async def _main(stop_event: asyncio.Event | None = None) -> None:
    from src.background_services import set_process_role, start_background_services, stop_background_services
    from src.core.browser_pool import shutdown_browser_pool
    from src.metrics import start_worker_metrics_server
    from src.tracing import configure_tracer_provider

    setup_logging(level=settings.log_level, log_format=settings.log_format)
    set_process_role("executor")
    configure_tracer_provider(settings, service_name=f"{settings.app_name.lower()}-access-executor")
    start_worker_metrics_server(settings.access_worker_metrics_port)

    stop_event = stop_event or asyncio.Event()
    _install_stop_signals(stop_event)
    logger.info("Starting access job worker")
    await start_background_services(role="executor")
    try:
        await run_access_job_worker(stop_event=stop_event)
    finally:
        await stop_background_services()
        try:
            await asyncio.wait_for(shutdown_browser_pool(), timeout=30)
        except Exception as exc:  # pragma: no cover - best effort at exit
            logger.warning(
                "Browser pool did not shut down cleanly", extra={"extra_data": {"error": type(exc).__name__}}
            )
        logger.info("Access job worker stopped")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Access job worker stopped")


if __name__ == "__main__":
    main()
