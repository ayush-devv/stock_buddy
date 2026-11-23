# ADR-0005: No automated order execution and no broker API keys

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
At about a dozen trades a year, placing orders by hand costs almost nothing. An order API at this capital level costs:
- Kite Connect at ₹500 a month, about 30% of capital a year;
- the SEBI retail-algo rules from 1 Apr 2026: a daily 2FA login, no refresh tokens, and a **static IP for placing, modifying or cancelling orders**.

Storing broker keys would also create a high-impact security risk.

**Correction to v0.1:** the static-IP rule covers order endpoints only, not read-only data calls. So it is a cost of automating execution, not a reason against reading data through an API.

## Decision
- The system never places orders.
- No broker API credentials are stored.
- Suggestions are shown to you, and you place every order yourself in the broker app.

## Options considered
| Option | For | Against |
|---|---|---|
| **Manual orders** | Zero cost; no key risk; a human checks every trade | You do the clicking |
| API execution | Hands-free | Cost, static IP, key security, overtrading risk |

## Consequences
- **Good:** no class of execution bugs; a human checks every trade.
- **Bad:** none at this trade frequency.

## Reversibility
High. Revisit when capital is ₹10L or more.
