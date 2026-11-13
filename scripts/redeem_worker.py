"""Run the durable redemption worker separately from Gunicorn."""
import argparse
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    app = create_app()
    integration = app.extensions["sub2api"]
    if args.healthcheck:
        if not integration.enabled:
            return 0
        with integration.transaction() as db:
            row = integration.diagnostics(db)
        return 0 if row["worker_seen"] >= time.time() - 180 else 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            handled = integration.process_once()
        except Exception as exc:
            # No repr/traceback: upstream/request objects can contain credentials.
            app.logger.error("Redemption worker failure: %s", type(exc).__name__)
            handled = False
        stop.wait(0.1 if handled else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
