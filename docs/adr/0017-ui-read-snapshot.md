# ADR-0017: The UI reads a snapshot; single writer

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
DuckDB allows either **one** read-write process **or** several read-only processes on a file, not both at once. If Streamlit had the database open, the evening job could not write to it.

## Decision
- The evening job is the only writer to `pi.duckdb`.
- At the end of each run it exports `pi_read.duckdb`: it writes a temp file, then renames it, so the swap is atomic.
- Streamlit opens only `pi_read.duckdb`, in read-only mode.
- CLI commands that write (`pi run`, `pi reconcile`) take a file lock.

## Options considered
| Option | For | Against |
|---|---|---|
| **Snapshot for the UI** | No lock conflicts; the UI can't corrupt data | UI is one run behind (fine) |
| UI connects read-only to the live file | No copy | Blocks the writer |
| Client-server database | Concurrency | A server to run |

## Consequences
- **Good:** the evening job never fails because the UI is open.
- **Bad:** the snapshot doubles disk use (about 40 MB a year, trivial).

## Reversibility
High.
