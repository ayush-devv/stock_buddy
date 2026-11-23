# ADR-0007: Prediction model hidden behind a go/no-go state

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
- A realistic out-of-sample AUC is about 0.52–0.56. This is derived from Gu, Kelly and Xiu (2020): a monthly R² of about 0.4%.
- People tend to over-trust algorithmic advice (Logg et al. 2019).

## Decision
- `gate_state` moves through NOT_EVALUATED → RESEARCH → FAILED | PAPER → VISIBLE ⇄ SUSPENDED.
- No probability appears in the UI, the digest or alerts unless the state is VISIBLE.
- The pass criteria are pre-registered in `gate_spec.yaml`:
  - the lower bound of the date-block bootstrap CI for Brier skill is above 0;
  - AUC is above 0.5 and below 0.57;
  - the top-minus-bottom spread is positive after costs;
  - the lockbox passes once;
  - then 3 months of PAPER and your approval.

## Options considered
| Option | For | Against |
|---|---|---|
| **Gated** | Shows the model only once it has earned it | You may never see probabilities |
| Always show it, with caveats | Transparent | Caveats get ignored; unearned trust |

## Consequences
- **Good:** honest output. FAILED is a valid ending; the tool still works as a ledger, analyser and alert tool.
- **Bad:** it takes months before any prediction is visible.

## Reversibility
High.
