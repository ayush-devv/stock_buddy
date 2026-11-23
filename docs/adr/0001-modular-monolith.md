# ADR-0001: Modular monolith in one Python package

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
The system has one user, one Mac and about 40 MB of data a year. It runs a nightly batch and has no concurrent users.

## Decision
One Python package, `pi`, with these modules:
- ingest, refdata, store, ledger
- analytics, model, research, guard
- decide, evaluate, alerts, ui

Module boundaries are enforced by import rules. There are no services, queues or orchestrator.

## Options considered
| Option | For | Against |
|---|---|---|
| **Modular monolith** | Least to operate; one deploy; easy to test | No independent scaling (not needed) |
| Microservices | Isolation | Far too much operational work for one person |
| Notebooks only | Fast to start | No reproducibility, no tests, leakage creeps in |

## Consequences
- **Good:** simple to run, debug and back up.
- **Bad:** a bug in one module can stop the evening run. Mitigation: per-step status in `run_log`.

## Reversibility
High. Modules can be split out later.
