# ADR-0010: Annual retrain, quarterly recalibration, Diebold-Mariano promotion

- **Status:** Proposed
- **Date:** 2026-11-23

## Context
- An expanding training window beat a rolling one: 0.40% a month (t = 2.34) against 0.33% (t = 1.35), from the Real-Time ML working paper.
- A 3-month champion/challenger shadow has almost no statistical power. Separating two models takes 8–225 years with 60-day labels (derived estimate).
- Updating the model after individual outcomes means fitting noise.

## Decision
- **Recalibrate:** refit Platt scaling quarterly, or when a drift monitor fires (calibration slope, PSI). A drift alarm triggers a human review, not an automatic refit.
- **Retrain:** once a year on an expanding window, through the research harness. Each retrain is logged as a trial.
- **Promote:** a new model replaces the champion only if it wins a long purged walk-forward Diebold-Mariano test against a pre-registered threshold, and **you approve**.
- **Shadow runs** are kept only to check the pipeline for bugs.

## Options considered
| Option | For | Against |
|---|---|---|
| **Annual expanding + DM** | Backed by evidence; resists noise | Slow to adapt |
| Monthly rolling retrain | Adapts quickly | Chases noise; worse in the evidence |
| Learn from each trade | Feels like learning | Fits noise |

## Consequences
- **Good:** model changes follow evidence.
- **Bad:** slow to react to a regime change. Mitigation: SUSPENDED state on drift.

## Reversibility
High.
