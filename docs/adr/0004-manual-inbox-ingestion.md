# ADR-0004: Manual inbox ingestion

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
NSE's terms of use prohibit "any systematic or automated data collection activities (including scraping, data mining, data extraction…)". The unofficial NSE libraries also break whenever NSE changes its site.

## Decision
You download these files by hand into `data/inbox/`:
- bhavcopy and delivery files
- index and TRI files
- corporate-action files
- surveillance lists (ASM/GSM/ESM)
- broker CSVs

`ingest` then:
- detects each file's format (legacy or UDiFF bhavcopy)
- validates it (schema, trading calendar, checksum)
- appends it to the raw layer and moves the original to `processed/`
- moves invalid files to `quarantine/`, with a reason

A missing required file sets `suggestions_paused` and raises a red DATA alert.

## Options considered
| Option | For | Against |
|---|---|---|
| **Manual inbox** | Complies with NSE's terms; robust | About 2–5 minutes a day of your time |
| nselib / jugaad-data scraping | Automatic | Breaches NSE's terms; fragile |
| Paid vendor or broker API | Automatic, licensed | Ongoing cost of about 30% of capital a year at ₹20k |

## Consequences
- **Good:** compliant, auditable and cheap.
- **Bad:** forgetting to download a day stops suggestions. That is a deliberate fail-safe.

## Reversibility
High. A licensed feed can replace the inbox later.

## Links
- Superseded in part: NSE-hosted RSS is also covered by this terms of use (see ADR-0016).
