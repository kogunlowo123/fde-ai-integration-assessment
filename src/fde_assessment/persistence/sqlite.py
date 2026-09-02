"""On-disk SQLite access layer.

WHAT
    Connection management, PRAGMA configuration and schema creation for the
    rate limiter (Task 4) and the RAG vector store (Production Enhancement).

WHY
    Task 4 mandates on-disk SQLite. SQLite is genuinely the right call at this
    scale, zero infrastructure, transactional, trivially reproducible in CI,
    but only if its concurrency model is respected rather than ignored.

HOW
    Three settings do the heavy lifting:

    * ``journal_mode=WAL``, readers do not block the writer and the writer
      does not block readers. Without it, every concurrent read during a write
      returns ``database is locked``.
    * ``busy_timeout``, a writer that finds the database locked waits instead
      of failing immediately. This is what makes multi-process access survive.
    * ``synchronous=NORMAL``, with WAL this is durable across application
      crashes (only a machine-level crash can lose the last transactions),
      which is the right trade for rate-limit accounting.

    Writes additionally take ``BEGIN IMMEDIATE`` so the read-check-insert
    sequence in the limiter is one atomic step rather than three racy ones.

WHEN
    One ``Database`` per process. Tests get one per temporary directory.

LIMITATIONS (and the production path)
    * One writer at a time, process-wide and machine-wide. Fine for a single
      gateway node; a bottleneck above roughly a few thousand writes/second.
    * No horizontal scaling: two gateway pods cannot share a SQLite file over
      a network filesystem safely.
    * Therefore the rate limit is enforced *per node*. Two nodes each admit up
      to the limit. Documented in ADR-004 and ADR-006.

    The evolution is PostgreSQL for durable state and Redis for distributed
    rate limiting; neither is added here, because neither is needed here.

SECURITY
    API keys are stored only as keyed hashes (see ``models.fingerprint``).
    Every statement is parameterised, no string interpolation into SQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from fde_assessment.common.logging import get_logger

log = get_logger(__name__)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS rate_limit_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id   TEXT    NOT NULL,
        api_key_hash TEXT   NOT NULL,
        timestamp   REAL    NOT NULL,
        token_count INTEGER NOT NULL,
        request_id  TEXT    NOT NULL,
        kind        TEXT    NOT NULL DEFAULT 'admission'
    )
    """,
    # The limiter's hot query is "sum tokens for this key since T", so the
    # index leads with the key and then the timestamp.
    """
    CREATE INDEX IF NOT EXISTS idx_rate_limit_key_time
        ON rate_limit_events (api_key_hash, timestamp)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rate_limit_time
        ON rate_limit_events (timestamp)
    """,
    """
    CREATE TABLE IF NOT EXISTS rag_documents (
        document_id   TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        title         TEXT NOT NULL,
        source        TEXT NOT NULL,
        document_type TEXT NOT NULL DEFAULT 'general',
        classification TEXT NOT NULL DEFAULT 'internal',
        content_hash  TEXT NOT NULL,
        created_at    REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rag_documents_tenant
        ON rag_documents (tenant_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS rag_chunks (
        chunk_id      TEXT PRIMARY KEY,
        document_id   TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        chunk_index   INTEGER NOT NULL,
        text          TEXT NOT NULL,
        embedding     BLOB NOT NULL,
        document_type TEXT NOT NULL DEFAULT 'general',
        classification TEXT NOT NULL DEFAULT 'internal',
        title         TEXT NOT NULL DEFAULT '',
        source        TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL,
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE
    )
    """,
    # Tenant isolation is enforced in SQL, so the leading index column is the
    # tenant: a query that forgets the predicate is also a slow query, which
    # tends to get noticed.
    """
    CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant
        ON rag_chunks (tenant_id, document_type)
    """,
)


class Database:
    """A single aiosqlite connection with the right PRAGMAs and a write lock."""

    def __init__(self, path: Path, busy_timeout_ms: int = 5_000) -> None:
        self.path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        # Serialises writers inside this process. SQLite would serialise them
        # anyway (and busy_timeout would absorb the contention), but taking the
        # lock in Python keeps the read-check-write sequence atomic without
        # relying on retry-after-failure.
        self._write_lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        log.info("sqlite_connected", path=str(self.path))
        return conn

    async def initialize(self) -> None:
        """Create the schema if it does not exist."""
        conn = await self.connect()
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a write under ``BEGIN IMMEDIATE`` and the in-process lock.

        ``BEGIN IMMEDIATE`` takes the write lock at statement one rather than
        on first write, which is what removes the classic SQLite upgrade
        deadlock: two transactions that both read first and then try to write.
        """
        conn = await self.connect()
        async with self._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return list(rows)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = await self.connect()
        async with conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
