# ADR-0006: HOLD-default decision rules with a cost and tax hurdle

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
At ₹20k, costs and tax outweigh any realistic edge:
- Fees run about 2.5% a year at 25% monthly turnover.
- STCG drag is about 2.5% a year.
- Barber and Odean found that the most active traders underperform.
- Defaults have a strong effect on what people do.

## Decision
- Deterministic rules choose HOLD, REVIEW, TRIM, ADD or AVOID. **HOLD is the default.**
- TRIM and ADD require all of the following:
  - the model is VISIBLE;
  - `guard` passes;
  - `expected_edge_inr > hurdle_inr`.
- The hurdle covers sell-side charges, the DP charge, the cost of buying the replacement, and STCG at 20% plus cess (no 87A rebate) or LTCG above the shared headroom.
- Limits:
  - minimum sell of ₹5,000, **or the whole holding if it is smaller** (the broker is Groww, and its DP charge is a fixed ₹23.60 per stock sold);
  - **at most one TRIM per calendar quarter** across the portfolio;
  - a 30-day cooldown per stock;
  - a sale is deferred when a lot is within 60 days of becoming long-term.
- At ₹20k with 4–6 holdings, a TRIM in practice means **selling one whole holding**. Partial sales cost too much in fixed charges.
- *Revised 2026-11-23:* the original limits were a ₹2,500 minimum and 10% monthly turnover. They could not both be met: 10% of ₹20k is ₹2,000, which is below the minimum sell. The trade-count cap replaced the turnover cap.
- AVOID applies only to watchlist names.

## Options considered
| Option | For | Against |
|---|---|---|
| **HOLD default + hurdle** | Protects against costs and overtrading | The system feels quiet |
| Action whenever probability > threshold | Feels responsive | Ignores costs; overtrades |
| An LLM decides | Flexible | Unauditable; hallucination risk |

## Consequences
- **Good:** realistic net returns; fewer trades.
- **Bad:** sticking with HOLD can lock in losers (the disposition effect). Mitigations: the drawdown REVIEW alert, and the TRIM screen shows tax consequences rather than P&L.

## Challenges raised
"A high bar keeps you in decaying positions." Accepted. The drawdown alert addresses it.

## Reversibility
High. Thresholds live in `rules.yaml`.
