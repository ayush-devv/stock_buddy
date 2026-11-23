# ADR-0014: Research harness with a trial wrapper and a lockbox

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
- Every extra backtest raises the chance of a false discovery (deflated Sharpe, PBO).
- An AI coding agent, Claude Code included, iterating on features or parameters is automated data snooping at scale.
- Trials that aren't logged make deflated-Sharpe corrections impossible.

## Decision
- `pi.research.run_trial()` is the **only** way to run a backtest. It writes a `trials` row first (config hash, `gate_spec` hash, data window, git SHA) and only then returns metrics.
- **Lockbox:**
  - The most recent ~2 years of data sit in `data/lockbox/`.
  - The research loader refuses those dates.
  - A test fails the build if any code under `research/` or `src/pi/model` reads them.
- `pi gate evaluate --lockbox` is run **once, by you**. After that the lockbox is burned, and the next one has to be new forward data.
- **Agents may:** propose ideas, write code, run trials.
- **Agents may not:**
  - edit `gate_spec.yaml` after its hash is logged;
  - delete trials;
  - run gate evaluation.

## Options considered
| Option | For | Against |
|---|---|---|
| **Harness + lockbox** | Honest out-of-sample evidence | Costs about 2 years of training data |
| Walk-forward only | More data | Repeated iteration overfits the walk-forward itself |
| Honour system | Easy | Fails silently, especially with agents |

## Consequences
- **Good:** gate results are credible.
- **Bad:** a smaller training set; one lockbox shot per spec.

## Reversibility
Medium.
