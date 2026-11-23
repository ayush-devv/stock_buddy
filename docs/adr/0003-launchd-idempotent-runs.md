# ADR-0003: launchd with catch-up and idempotent runs

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
The Mac is often asleep at 19:00, and days can be missed.

## Decision
- A `launchd` LaunchAgent uses `StartCalendarInterval` at 19:00 IST, plus `RunAtLoad`.
  - If the Mac was **asleep** at 19:00, the job runs once on wake.
  - If it was **powered off**, `RunAtLoad` runs the job at your next login.
- `pi run` processes **every pending trade_date in order**. Each step is idempotent per date.
- A heartbeat ping goes to an external monitor on success. The ping carries no data. If the monitor misses a ping, it sends you an email.

## Options considered
| Option | For | Against |
|---|---|---|
| **launchd** | Built into macOS; catches up on wake | macOS-specific |
| cron | Familiar | No catch-up after sleep |
| Airflow / Prefect | Run history UI | A server to run; overkill |

## Consequences
- **Good:** missed days are never silently lost.
- **Bad:** alerts are only sent while the Mac is awake (architecture §11.3).

## Reversibility
High.
