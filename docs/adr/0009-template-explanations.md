# ADR-0009: Template explanations; LLM off by default

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
- Frontier models hallucinate in about 3–11% of grounded summaries (Vectara leaderboard).
- Explanations can rationalise rather than reflect the real reason (Turpin et al.).
- A typical financial error is a number attached to the wrong label.

## Decision
- Explanations are filled in from templates using the `payload_json` the code produced.
- An optional LLM rewrite can be switched on later only with **all** of the following:
  - a deterministic validator checks that every number, ticker, date and direction word appears in the payload;
  - any mismatch triggers a regeneration;
  - no causal claims beyond the payload;
  - a pinned model version and low temperature;
  - no tools.

## Options considered
| Option | For | Against |
|---|---|---|
| **Template first** | Zero hallucination; free | Less fluent |
| LLM with a validator | Fluent | Cost; residual risk |
| LLM without a validator | Fluent | Unsafe |

## Consequences
- **Good:** explanations can always be trusted.
- **Bad:** the text reads mechanically.

## Reversibility
High.
