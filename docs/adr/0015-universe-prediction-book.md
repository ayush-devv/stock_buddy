# ADR-0015: Universe-wide monthly prediction book

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Scoring only the 4–6 stocks you hold gives no statistical power. Daily predictions with 60-day horizons overlap and inflate the apparent sample size.

## Decision
- On the last trading day of each month, the system predicts every NIFTY 500 member as of that date.
- These go into `predictions`, marked paper or visible.
- All model evaluation runs on this book: calibration, Brier skill CI, rank IC and the decile edge table.
- Book dates are monthly, but each resolves over 60 trading days, so consecutive books overlap by about two months. Standard errors use HAC or month-block bootstrap.
- `suggestions` remain limited to holdings and the watchlist. They are used for decisions, not model evaluation.

## Options considered
| Option | For | Against |
|---|---|---|
| **Monthly universe book** | About 6,000 predictions a year | Some compute (trivial) |
| Holdings only | Simple | No statistical power |
| Daily universe book | More rows | Overlap means little extra information |

## Consequences
- **Good:** calibration and the edge table can be measured over **years rather than decades**. The effective sample size is set by independent dates, not stocks: each month's 500 predictions share market and sector shocks, and 60-day horizons give only about 4 independent dates a year.
- **Bad:** even so, the evidence builds slowly. The gate leans on the backfilled walk-forward and lockbox first.

## Reversibility
High.
