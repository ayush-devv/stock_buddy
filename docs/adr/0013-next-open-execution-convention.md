# ADR-0013: Next-open execution and label convention

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Bhavcopy arrives around 18:00 and the digest around 19:00, so the earliest anyone can act is the next morning. v0.1 left this undefined, which risks an optimistic backtest that assumes trading at the close of D.

## Decision
- Information for day D is known at 18:00 IST on D. Backfilled rows are flagged `known_at_synthetic`.
- Execution is assumed at the **open on D+1**, with slippage taken from `costs.yaml`.
- The label is the **total return** (dividends included) from the D+1 open to the open 60 trading days later, compared with the cross-sectional median of the NIFTY 500 members as of D.
- If a stock is delisted within the horizon, the return uses the last traded price for a voluntary delisting and −100% for a forced one.
- Corporate-action adjustment as of D uses only actions with `ex_date ≤ D`.
- Close-based features carry a `close_regime` flag for the NSE Closing Auction Session. Its live date is unverified: sources say 3 Aug 2026 or 7 Dec 2026.

## Options considered
| Option | For | Against |
|---|---|---|
| **Open on D+1** | Realistic; uses data the system actually has | The open can be noisy |
| Close on D | Simple | Look-ahead: you can't trade at a price you haven't seen |
| VWAP on D+1 | Closer to real fills | Not in bhavcopy (only value/volume per day) |

## Consequences
- **Good:** honest backtests.
- **Bad:** returns slightly lower than close-to-close backtests. That gap is correct.

## Reversibility
Low after the gate (changing it invalidates trials), so it is decided now.
