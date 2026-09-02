# ADR-012, Brute-force vector search in SQLite

**Status:** Accepted · **Date:** 2026-09-02
**Scope:** Production Enhancement (RAG). Not part of assessment Tasks 1-4.

## Context

The retrieval enhancement needs to store chunks with embeddings and return the
most similar ones for a query, scoped to a tenant and filtered by metadata.
The assessment already mandates SQLite, and the corpus is small.

## Decision

Store embeddings as packed `float32` blobs in a `rag_chunks` table. Search
filters in SQL first (`WHERE tenant_id = ?` plus any metadata predicate), then
computes cosine similarity in Python over the surviving rows.

The filter-then-score order is a security property, not an optimisation:
another tenant's rows are never loaded into the process, so they cannot be
ranked, logged, truncated into a prompt, or leaked by a later bug.

`VectorStore` is a `Protocol` with `upsert` and `search`, so the
implementation is replaceable without touching the retriever or the pipeline.

## Alternatives considered

**`sqlite-vec` / `sqlite-vss` extension.** Real ANN indexing inside SQLite.
Rejected: loadable extensions are not available in every environment (and are
disabled by default in some Python builds), which would make the repository
fail to run somewhere for a benefit the corpus size does not need.

**pgvector.** The natural production answer, SQL, transactions and vectors in
one place. Rejected here: it contradicts the SQLite requirement and adds a
service to operate for a workload of eight chunks.

**A dedicated vector database** (Qdrant, Weaviate, Pinecone). Better at scale,
with hybrid search and mature filtering. Rejected: another service, another
cost line, another failure mode, and a network hop on the request path.

**FAISS in-process.** Fast and dependency-light for read-heavy workloads.
Rejected: it is a separate index to persist and keep consistent with the
metadata in SQLite, which is exactly the complexity SQLite was chosen to avoid.

## Consequences

- Zero infrastructure; the store is created by the same `Database` as the rate
  limiter.
- Measured: ~0.62 ms mean search over the four-document corpus, with ingestion
  of the whole corpus at ~8.2 ms.
- **Linear in the number of chunks after filtering.** Fine to roughly tens of
  thousands of chunks per tenant on commodity hardware; beyond that, latency
  grows visibly.
- Filtering by tenant first means the scan is over one tenant's data, so
  multi-tenancy improves rather than degrades the scaling picture.
- Cosine is computed in pure Python, no NumPy dependency. At 256 dimensions
  that is fast enough; a larger embedding width would justify NumPy.

## Security impact

Tenant isolation is enforced in the query, and `RetrievalFilter` makes
`tenant_id` a required constructor argument, so a retrieval that forgets the
tenant does not type-check. Verified at the store, the retriever and the
service in `tests/security/test_rag_isolation.py`.

## Cost impact

No managed vector database (roughly $70-300/month at small scale). Embeddings
are computed locally, and content hashing means unchanged documents are never
re-embedded.

## Operational impact

Chunk text is stored in plaintext; disk encryption is a deployment control.
Re-ingestion after changing embedding models is mandatory, mixing embedders in
one corpus produces meaningless similarities.

## Trigger to revisit

Retrieval p95 above ~50 ms, or more than ~50k chunks for a single tenant.
Migration: implement `VectorStore` against pgvector, backfill by re-ingesting,
and switch by configuration. Nothing above the store changes.
