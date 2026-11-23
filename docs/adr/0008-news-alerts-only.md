# ADR-0008: News and filings are used for alerts only

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
Using news as a model input has four problems:
- there is no free point-in-time news archive;
- LLMs have already seen historical outcomes, which builds look-ahead bias into any backtest (FinCAD; Sarkar and Vafa);
- prompt injection (OWASP LLM01) and RAG poisoning;
- prices react to filings within minutes, faster than an evening batch.

## Decision
- Filings and news feed **only** the `alerts` module.
- There is no path from news into `analytics` or `model`.
- Text is treated as untrusted data: shown as plain text, and never given to an LLM that has tools.

## Options considered
| Option | For | Against |
|---|---|---|
| **Alerts only** | Useful awareness without leakage | Leaves any news signal unused |
| News as features (sentiment) | Possible signal | Leakage, injection, no point-in-time archive |

## Consequences
- **Good:** no news leakage; the model stays auditable.
- **Bad:** misses any genuine news signal.

## Reversibility
Medium. Revisit only after collecting about 2 years of your own timestamped news archive.
