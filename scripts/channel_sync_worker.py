"""Channel sync worker (ADR-018).

Processes due SyncJobs in a loop. Run one per deployment:

    POSTGRES_DSN=... python scripts/channel_sync_worker.py [--interval 5] [--once]

On PostgreSQL jobs are claimed with FOR UPDATE SKIP LOCKED, so several
workers are safe; on SQLite run exactly one.
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.persistence import PersistenceService  # noqa: E402
from services.channels.sync import ChannelSyncService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        logging.error("POSTGRES_DSN is required")
        return 2
    service = ChannelSyncService(PersistenceService(dsn=dsn))
    while True:
        summary = service.process_due(limit=args.batch)
        if summary["processed"]:
            logging.info("sync batch: %s", summary)
        if args.once:
            return 0
        if summary["processed"] < args.batch:
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
