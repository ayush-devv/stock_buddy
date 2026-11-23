# ADR-0002: Bitemporal Parquet raw layer + DuckDB

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Point-in-time correctness is the main defence against look-ahead bias. NSE sometimes re-issues a day's file, so corrections must be stored without losing what was known earlier.

## Decision
- Raw data is stored as **immutable Parquet**, partitioned by source and year.
- Every row carries `event_date`, `known_at` and `ingest_seq`. A correction is a new row with a higher `ingest_seq`.
- **DuckDB** views provide `as_of(D)` queries.
- Prices are stored unadjusted. Corporate-action adjustment is applied when the data is read.
- Polars handles transforms.

## Options considered
| Option | For | Against |
|---|---|---|
| **Parquet + DuckDB** | Serverless, fast, columnar, simple to back up | One writing process at a time (see ADR-0017) |
| PostgreSQL / TimescaleDB | Concurrent writers | A server to run, overkill |
| SQLite | Simple | Slow for analytics, no Parquet |

## Consequences
- **Good:** reproducible as-of queries; corrections are auditable.
- **Bad:** queries are more complex because they must pick the latest `ingest_seq` known as of D.

## Reversibility
High.
