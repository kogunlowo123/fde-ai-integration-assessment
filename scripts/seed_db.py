#!/usr/bin/env python
"""Create the SQLite schema and ingest the sample corpus.

    python scripts/seed_db.py                       # schema + corpus for tenant-a
    python scripts/seed_db.py --tenant acme         # a different tenant
    python scripts/seed_db.py --schema-only         # rate limiter tables only

Idempotent: re-running it re-uses the content hashes, so unchanged documents
are not re-embedded.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fde_assessment.common.config import get_settings  # noqa: E402
from fde_assessment.common.logging import configure_logging  # noqa: E402
from fde_assessment.persistence.sqlite import Database  # noqa: E402
from fde_assessment.rag.service import build_rag_service  # noqa: E402


async def run(tenant: str, corpus: Path, schema_only: bool) -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt="console")

    database = Database(settings.database_path, settings.rate_limit_busy_timeout_ms)
    try:
        await database.initialize()
        sys.stdout.write(f"schema ready at {settings.database_path}\n")

        if schema_only:
            return 0

        # The corpus directory was validated in `main`, before the event loop
        # started: a blocking stat call inside an async function is the thing
        # ruff's ASYNC rules exist to catch.
        service = await build_rag_service(settings, database)
        report = await service.ingestion_pipeline.ingest_directory(corpus, tenant)
        sys.stdout.write(
            f"tenant={tenant} seen={report.documents_seen} "
            f"embedded={report.documents_embedded} "
            f"skipped_unchanged={report.documents_skipped_unchanged} "
            f"chunks={report.chunks_written}\n"
        )
        for error in report.errors:
            sys.stderr.write(f"  error: {error}\n")
        return 1 if report.errors else 0
    finally:
        await database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="tenant-a", help="tenant id to ingest under")
    parser.add_argument("--corpus", type=Path, default=REPO_ROOT / "corpus")
    parser.add_argument("--schema-only", action="store_true")
    args = parser.parse_args()
    # Checked before the event loop starts: a blocking stat call inside an
    # async function is exactly what ruff's ASYNC rules exist to prevent.
    if not args.schema_only and not args.corpus.is_dir():
        sys.stderr.write(f"corpus directory not found: {args.corpus}\n")
        return 1
    return asyncio.run(run(args.tenant, args.corpus, args.schema_only))


if __name__ == "__main__":
    raise SystemExit(main())
