# ADR-0016: Alert sources must pass a terms check; no NSE RSS polling

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
- NSE publishes official RSS feeds (announcements, board meetings, results, corporate actions, insider trading).
- NSE's terms of use ban "any systematic or automated data collection" and make **no RSS exception**.
- v0.1 planned to poll them, which contradicted ADR-0004.
- For BSE, no automated-access clause was found, but that is not confirmed.
- Pledges (SAST Reg 31) and surveillance moves are published on separate pages from company announcements (LODR Reg 30).

## Decision
- No automated polling of NSE-hosted feeds.
- Before Phase 2, each candidate source is checked against its terms and the result is recorded here. In order of preference:
  1. BSE corporate-announcement RSS, if its terms allow;
  2. broker alerts or notifications;
  3. a manual check of the filing pages for holdings, plus the surveillance list download, in the daily inbox routine.
- Results-ahead alerts are triggered by board-meeting notices. Reg 29 requires only 2 working days' notice.

## Options considered
| Option | For | Against |
|---|---|---|
| **Terms-checked source** | Compliant | May end up manual |
| NSE RSS | Official, structured | Conflicts with NSE's terms of use |
| Scraping news sites | Broad | Terms and copyright risk; injection surface |

## Consequences
- **Good:** consistent compliance.
- **Bad:** urgent filing alerts may be slower, or need you to check by hand.

## Reversibility
High.
