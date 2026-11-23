# ADR-0012: Guard module for insider-trading rules and surveillance tradability

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Some suggestions would be **illegal** to act on; others **can't be executed**:
- **SEBI PIT Regulations 2015**, for designated persons (DPs) and their relatives:
  - the trading window is closed from quarter end until 48 hours after results;
  - the contra-trade restriction lasts at least 6 months;
  - pre-clearance is needed above the company's threshold;
  - Reg 4 bans trading any security while holding its unpublished price-sensitive information (UPSI);
  - since Apr 2025 the PAN freeze also covers DPs' immediate relatives.
- **Exchange surveillance:**
  - ASM, GSM and ESM stages;
  - trade-for-trade in series BE/BZ;
  - GSM stage 3 and above trades only weekly;
  - brokers block buys from GSM stage 2;
  - price bands and lower-circuit locks.
- **Corporate-action shares** arrive days or weeks after the ex/record date (T+1 settlement).

## Decision
- A `guard` module runs **before** `decide`, and its verdict overrides any action.
- Inputs:
  - `config/restricted.yaml`, which you maintain;
  - `surveillance` and `price_bands` tables;
  - `series_history`;
  - `ledger_entries.credit_date`.
- Entering or changing surveillance stage, and a restricted window opening or closing, are red alerts.
- Same-day buy and sell triggers a warning (taxed as speculative income).
- SME lot sizes are enforced.

## Options considered
| Option | For | Against |
|---|---|---|
| **Separate guard module** | Single choke point; testable | Another module |
| Rules inside `decide` | Fewer modules | Easy to bypass; mixes legality with preference |
| Ignore | — | Regulatory and practical failure |

## Consequences
- **Good:** no suggestion you can't legally or practically act on.
- **Bad:** depends on you keeping `restricted.yaml` correct. The system can't detect insider status. Confirming it (assumption A8) is a Phase 1 exit criterion.

## Reversibility
High.
