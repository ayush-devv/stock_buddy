# ADR-0011: Internal security_id and ISIN map

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Splits, consolidations and demergers issue **new ISINs**, and the old ISIN is frozen on the record date. Symbols change too. Keying on ISIN or symbol would break tax lots and price history. v0.1 keyed on ISIN, which was wrong.

## Decision
- An internal `security_id` is assigned once.
- `isin_map(isin, valid_from, valid_to, security_id, reason)` links each ISIN to it.
- `symbol_history` and `series_history` (EQ/BE/BZ/SM/ST) track the other changes.
- On a demerger, the new company gets a new `security_id`. `corp_actions.cost_split_ratio` carries the cost apportionment.

## Options considered
| Option | For | Against |
|---|---|---|
| **Internal ID + maps** | Survives every corporate action | Maps must be kept up to date |
| ISIN as key | Simple | Breaks on splits and demergers |
| Symbol as key | Simplest | Breaks on renames |

## Consequences
- **Good:** continuous history and correct lots.
- **Bad:** each ISIN change needs an entry in the map. Mitigation: an unmapped ISIN in the bhavcopy goes to quarantine.

## Reversibility
Low once data has accumulated, so the decision is made now.
