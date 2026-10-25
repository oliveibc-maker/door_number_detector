"""Process-wide startup and shutdown steps for the web server.

The order below matters and is written down only here:

1. Load Paddle. Importing it installs a fatal SIGTERM handler and resets the
   root log level to WARNING.
2. Configure logging, which sets the root level back to INFO.
3. Install our SIGTERM handler, so it replaces Paddle's.

Shutdown cancels the running batch, closes the server, waits for the worker, and
closes the detector.
"""

import signal
import threading
from collections.abc import Callable
from typing import Any, Protocol

from door_number_detector.logging_config import configure_logging


class _ClosableServer(Protocol):
    def server_close(self) -> None: ...


def _handle_sigterm(_signum, _frame):
    raise KeyboardInterrupt


def start_process() -> None:
    import paddle  # noqa: F401

    configure_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)


def shutdown_process(
    server: _ClosableServer,
    cancel_and_get_worker: Callable[[], Any],
    close_detector: Callable[[], None],
) -> None:
    """Stop the batch, close the server, wait for the worker, close the detector.

    `cancel_and_get_worker` sets the running batch's cancel event, if any, and returns its
    worker thread (or None). It is called again after the server closes, because
    closing waits for in-flight requests that may have started a job.
    """
    cancel_and_get_worker()
    server.server_close()
    worker = cancel_and_get_worker()
    if worker is not None and worker is not threading.current_thread():
        worker.join()
    close_detector()
