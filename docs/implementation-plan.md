# Implementation plan

**Version:** 0.2 (2026-11-23). **Status:** draft, waiting for your review. v0.2 adds your answers: the broker is Groww, the backfill is approved, and you are not a designated person.
**Builds:** [architecture v0.2](architecture.md). This plan breaks §17 of the architecture into work packages (WPs). It changes the order of work in two places, and §17 now links here:
1. The daily ingest runs end to end before the ledger is built.
2. The controls on coding agents are built in Phase 0, not Phase 3.

## 1. The main constraint is waiting time, not coding time

Three phase exits require the system to run for a fixed number of real trading days. More coding does not shorten them:

| Wait | Length | Starts when |
|---|---|---|
| Phase 1 exit: 60 consecutive trading days with no unresolved quarantine | about 12–13 calendar weeks | the daily run works end to end (WP 1a) |
| Phase 2 exit: 20 trading days of digests, with 🟡 rules in shadow mode | about 4 calendar weeks | the alert engine is live (WP 2.4) |
| Phase 4 exit: 3 months of paper trading with no pipeline bugs | about 13 calendar weeks | the gate verdict is PAPER (end of Phase 3) |

So the first goal is a **thin daily slice**: one bhavcopy file and one delivery file go into `inbox/`, are parsed, are stored bitemporally, can be queried as of any date, and are logged. `launchd` triggers it, and the heartbeat confirms it ran. As soon as that works, the 60-day count starts. The ledger, tax and guard work then happens during those 60 days.

> **What you have to do every day:** the count only advances if **you download that day's NSE files into `data/inbox/` every trading day**. NSE's terms rule out automated downloading (ADR-0004). This habit starts at the end of WP 1a. A missed day is not lost if you download it later, but a day that is never filled resets the count.

## 2. What is blocked, and what can start now

| Question | Answer (2026-11-23) | What follows |
|---|---|---|
| **A1:** which broker | **Resolved: Groww.** | WP 1.7 starts with a 2-hour spike to find which reports Groww exports, and in what format. |
| **A6:** ₹500 one-off Kite backfill | **Approved.** Phase 3 is unblocked. | You need a free Zerodha account used only for data, because Kite Connect logs in with Zerodha credentials. Open it any time before WP 3.1. **Groww's own API was rejected** because its historical data reportedly goes back only about 3 months, far too short for training. |
| **A8:** insider-trading status | **Resolved: not a designated person.** | `restricted.yaml` ships empty. `guard` is still built and tested, because Reg 4 applies to anyone and your status can change. |
| Filing source (ADR-0016) | WP 2.5 | Fall back to checking filings by hand |

**Unblocked today** (once you allow writes outside `docs/`):
- all of Phase 0;
- refdata, store, calendar, and the bhavcopy and delivery parsers;
- run orchestration, launchd and the heartbeat;
- the cost and tax engines;
- the guard rules.

## 3. Files you need to supply

I can't download from NSE, and the parsers can't be called done without real sample files. Put each one in `tests/fixtures/` once Phase 0 exists. These files never leave your machine.

| # | File | Needed by | Notes |
|---|---|---|---|
| F1 | Legacy-format bhavcopy: one day before 8 Jul 2024 | WP 1.2 | Tests the format changeover |
| F2 | UDiFF bhavcopy: 3 consecutive recent days | WP 1.2 | |
| F3 | Security-wise delivery file (MTO) for the same days | WP 1.2 | |
| F4 | A bhavcopy covering a split or bonus ex-date, plus the corporate-action file | WP 1.1, 1.6 | Tests that the ISIN change keeps the same `security_id` |
| F5 | NIFTY 500 index and TRI file | WP 2.1 | |
| F6 | The surveillance lists (ASM, GSM, ESM) as you can download them | WP 1.12 | Their format is unverified (open question 5) |
| F0 | NSE's Capital Market **trading-holiday circular** for 2026, plus any amendments (for example the 15 Jan 2026 closure); add 2027 once it's published | WP 1.1 | Copy the weekday dates into `config/calendar.yaml`. Until then, every holiday pauses the evening run as a "missing day". |
| F7 | From Groww: **all** order history since you opened the account, a holdings statement, the stocks P&L report for each financial year, and 2 contract notes. From CDSL: one CAS statement. | WP 1.7, 1.8 | Before saving, remove your name, client ID and PAN. The CAS is an independent check on holdings that doesn't depend on Groww. |

## 4. How each work package is run

- **Definition of done:** the code is merged; the tests listed for that WP pass; `pi status` stays green; any new config is versioned. If behaviour changed, `architecture.md` or the relevant ADR is updated.
- **Test first** for anything financial: ledger, FIFO, costs, tax, guard and labels. Expected values come from broker statements or hand calculations, never from the code being tested.
- **One WP per branch and commit**, so each recorded `git_sha` points at a meaningful change.
- **Sizes are focused hours, and they are assumptions.** I don't know how many hours a week you have. Only the waits in §1 are fixed in calendar time.
- **Dependencies are added only when a phase needs them** (§11).

---

## 5. Phase 0: skeleton (about 16 h)

Everything in this phase writes **outside `docs/`**, so it needs your go-ahead.

| WP | Builds | Tests that prove it | Depends on |
|---|---|---|---|
| 0.1 | Set up the repo:<br>• `git init`<br>• `.gitignore` covering `data/`, `.env` and `*.duckdb`<br>• `pyproject.toml` (Python 3.12, `uv`, lockfile)<br>• `ruff` and `pytest` | `uv run pytest` passes on an empty suite. `git status` shows `data/` is ignored. | Your permission |
| 0.2 | Create the `src/pi/` package with the 12 modules. Add `import-linter` contracts:<br>• a layering contract, so lower layers (`store`, `refdata`, `ingest` and so on) never import higher ones and nothing imports `ui` or `cli`<br>• only `research` may reach the lockbox loader<br>• "`decide` must go through `guard`" can't be written as an import rule, so it becomes a runtime test in WP 4.2 | The contracts fail when they should: proven by a deliberate violation in the test | 0.1 |
| 0.3 | Config loader: pydantic models for `rules`, `costs`, `tax`, `restricted` and `gate_spec`. Each file gets a content hash, which serves as its version. | A malformed YAML file fails loudly. Changing a file changes its version hash. | 0.1 |
| 0.4 | CLI: `pi status` and `pi run --date` as no-op stubs | `pi status` prints the run state | 0.3 |
| 0.5 | **Controls on coding agents** (moved forward from §14.2 of the architecture):<br>• a PreToolUse hook, `.claude/hooks/research_guard.py`, runs on **every** tool. Plain permission rules don't cover MCP tools such as lean-ctx. The hook blocks:<br>&nbsp;&nbsp;– access to `data/lockbox/**`<br>&nbsp;&nbsp;– writes to `config/gate_spec.yaml`<br>&nbsp;&nbsp;– `pi gate evaluate`<br>&nbsp;&nbsp;– changes to the hook or `.claude/settings.json`<br>• `.claude/settings.json` adds deny rules as a second layer<br>• a test fails if any path outside the research loader refers to `lockbox`<br>• `trials` is append-only (enforced in Phase 3) | Ask an agent to read a lockbox file: it is refused. The path check fails on a planted reference. | 0.2 |

**Exit:** `pi status` runs, tests pass, and the controls in WP 0.5 are shown to work.

**Status, 2026-11-23: built.**
- All 69 tests pass; `ruff` and `lint-imports` are clean.
- The hook also protects `.claude/settings.local.json`, which could otherwise switch all hooks off. You installed that fix yourself, because the hook blocks agents from changing it.
- The hook was checked live: it blocked a read of `data/lockbox/`, an edit of `gate_spec.yaml`, and a shell command that named the gate file.
- **Still open (yours):** add `auc_ci_lower_gt: 0.5` under `criteria` in `gate_spec.yaml`, so "AUC significantly above 0.5" is stated exactly. The loader already accepts it. Do this before you freeze the spec.
- The hook is a guard rail, not a sandbox, because a deliberately disguised shell command could get past it. The import contract and the source scan are the second layer.

---

## 6. Phase 1: data, refdata, ledger and guard

### 1a: the thin daily slice (about 44 h). Do this first, because it starts the 60-day count.

| WP | Builds | Tests | Depends on |
|---|---|---|---|
| 1.1 | **Refdata:**<br>• `securities`, `isin_map`, `symbol_history`, `series_history`<br>• `security_id` assigned the first time a security is seen<br>• trading calendar: a day counts as a trading day if a bhavcopy exists; the holiday list is used only to spot missing days | Golden file F4: the split keeps one `security_id`. A move from EQ to BE is not a gap. A Muhurat or Saturday session counts as a trading day. | 0.3 |
| 1.2 | **Ingest:**<br>• reads `inbox/` at run time<br>• detects the format (legacy or UDiFF)<br>• checks schema, row counts and SHA-256<br>• moves bad files to `quarantine/` with a reason, and good files to `processed/YYYY/MM/` | Golden files F1–F3. A truncated file is quarantined. The same file dropped twice is ingested once. | 1.1 |
| 1.3 | **Store:**<br>• immutable Parquet under `raw/<source>/year=YYYY/`<br>• `ingest_seq` on every row; `known_at` and `known_at_synthetic` columns<br>• DuckDB `as_of(D)` views | A re-issued file creates a higher `ingest_seq`, and the view picks it. A query for D never returns rows with `known_at > D`. Raw files are never rewritten (checked by hash). | 1.2 |
| 1.4 | **Run orchestration:**<br>• `run_log` and `system_state`<br>• processes every pending `trade_date` in order, idempotently<br>• pauses on a missing day<br>• exports the `pi_read.duckdb` snapshot by atomic rename | Running twice gives identical state. A gap day pauses later dates. A reader holding the snapshot doesn't block the writer. | 1.3 |
| 1.5 | **Operations:**<br>• launchd plist with a 19:00 calendar trigger and `RunAtLoad`<br>• heartbeat ping on success, carrying no data<br>• Telegram 🔴 DATA alert, with the token in Keychain | It fires after sleep and after login. Stopping the job produces a missed-ping email. A missing file produces a Telegram alert with only the ticker, rule and severity. | 1.4 |

**Exit 1a:** 5 consecutive real trading days processed by launchd with no manual steps besides dropping files. **The 60-day count starts here.**

**Status, 2026-11-23** (branch `phase-1a/core`):
- **WP 1.1:** built, except the golden-file test, which needs F4.
  - The calendar reads `config/calendar.yaml`, which is empty until F0 is filled in.
  - A security gets its ID once.
  - A new ISIN under an existing symbol gets a *provisional* ID and an open issue. It is never guessed.
  - `link_isin` merges the provisional ID through an alias, so the retired ID still resolves.
- **WP 1.3:** built.
  - A correction replaces the whole day for its source. The same file dropped twice creates no new version.
  - Each version records both `known_at` and the real `ingested_at`.
- **WP 1.4:** built.
  - Each day is processed as one transaction.
  - A missing day pauses the run (exit 3), and the message names both fixes.
  - The 60-day streak is recomputed from the run log on every run, so declaring a forgotten holiday repairs it.
  - Corrections that arrive for a day already completed are flagged in `pi status` until you run `pi run --date`.
  - Limitation: re-running a past date does not rewrite its security-ID history.
- **Not yet built:**
  - WP 1.2, the parsers, which need F1–F3.
  - WP 1.5, launchd, the heartbeat and Telegram.
  - Until WP 1.2, `pi run` only processes data already in the store.

### 1b: ledger, tax and guard (about 95 h). Runs during the 60-day count.

| WP | Builds | Tests | Depends on |
|---|---|---|---|
| 1.6 | Ingest of `corp_actions`, and adjustment when data is read, using only actions with `ex_date ≤ D` | Adjusted prices are continuous across a split. Data as of D is unaffected by a later announcement. | 1.3 |
| 1.7 | **Groww `BrokerAdapter`:**<br>• **Spike (2 h)** to find which reports Groww exports, in which formats, and whether the order history covers your whole account life<br>• parsers for order history, holdings, contract notes and the P&L report<br>• a CDSL CAS parser for an independent holdings check | Golden files from F7. Holdings from Groww match holdings from the CAS. | 1.1, F7 |
| 1.8 | **Ledger:**<br>• `trades` and `ledger_entries`<br>• ledger entries for corporate actions: bonus, split, demerger with cost apportionment, rights entitlement, buyback, merger<br>• `credit_date` for each entry<br>• FIFO lots per demat<br>• opening lots from the full tradebook, or from a manual opening-lot file | Property tests (`hypothesis`): after any sequence of trades and corporate actions, the lots add up to the holdings and the cost is conserved. Matches the broker holdings exactly. | 1.6, 1.7 |
| 1.9 | **Costs:** `costs.yaml` with dated entries for each charge:<br>• Groww delivery brokerage: ₹20 or 0.1% per order, whichever is lower, minimum ₹5<br>• STT, exchange and SEBI fees, stamp duty, GST<br>• Groww DP charge: ₹20 + GST per stock per sell day<br>Confirm every figure on groww.in/pricing and check whether the DP charge appears on Groww's contract note | Each contract note is matched within ₹1, **excluding DP**. DP appears on the ledger. | 1.8 |
| 1.10 | **Tax:**<br>• `tax.yaml` with dated rules (§12 of the architecture)<br>• STCG and LTCG, with the ₹1.25L exemption shared with the ETF shadow<br>• the three buyback periods, bonus, demerger and loss set-off<br>• the speculative rule for same-day sells, and s.94(7) | Hand-worked cases for every row of §12. Results match the broker's tax P&L report. | 1.8, 1.9 |
| 1.11 | **ETF shadow:** a paper portfolio of NIFTY 500 ETF lots that draws on the same LTCG headroom | Its headroom is shared with real sales | 1.10 |
| 1.12 | **Spike (2 h):** what format the surveillance lists come in. Then ingest `surveillance` and `price_bands`. | Golden file F6. A stage change creates a row with a new `effective_from`. | 1.3, F6 |
| 1.13 | **Guard:**<br>• `restricted.yaml`: trading window, 6-month contra rule, pre-clearance<br>• surveillance stage, price band and lower circuit<br>• quantity not yet credited<br>• same-day sell warning, SME lot size | One unit test per rule. The import contract shows `decide` cannot skip `guard`. | 1.8, 1.12 |
| 1.14 | Reconciliation: the ledger is compared with the broker after corporate-action entries are applied, and a 🔴 alert fires on a mismatch | A corporate-action credit that hasn't arrived yet does **not** raise a false alert | 1.8, 1.5 |

**Tax items marked [unverified]** in §12 of the architecture (the carry-forward period, the s.94(7) windows, and section numbers under the 2025 Act) go into `tax.yaml` with a `confidence: unverified` flag. Take them to the yearly CA review rather than researching them now.

**Phase 1 exit (§17, unchanged):** 60 clean days; the ledger matches the broker exactly; contract notes match within ₹1 excluding DP; tax P&L matches; A8 is already confirmed (2026-11-23).

---

## 7. Phase 2: analyser and alerts (about 62 h)

| WP | Builds | Tests | Depends on |
|---|---|---|---|
| 2.1 | Ingest of `index_levels` (price index and TRI). Point-in-time `index_membership` from NSE circulars, starting with the current list. | Golden file F5. Membership as of D. | 1.3 |
| 2.2 | **Analytics:**<br>• returns, EWMA and realised volatility, beta against NIFTY 500 and sector, drawdown<br>• Ledoit-Wolf shrunk correlation, risk contribution, active sector weight, scenario replays | Checked against hand-worked examples. Results don't change when future rows are added. | 1.6, 2.1 |
| 2.3 | `events_calendar`: board meetings, results and ex-dates | The "results ahead" alert fires on the notice date, not a fixed offset (Reg 29) | 1.6 |
| 2.4 | **Alert engine:**<br>• rules in `rules.yaml` with severity and cooldown<br>• 🟡 rules start in **shadow mode for 20 days**<br>• a weekly cap of 3 urgent alerts, and an evening digest | Each §11.2 rule fires on a crafted case. Shadow alerts are logged but not sent. | 2.2, 2.3, 1.14 |
| 2.5 | **Spike (3 h):** terms check on the filing source (ADR-0016). Then a poller, or a manual-check workflow. | Filing text is stored as untrusted data and never acted on | 2.4 |
| 2.6 | **Streamlit UI** on `pi_read.duckdb`, bound to `localhost` only. Pages: holdings, risk, tax lots, alerts. | The UI can run while the evening job runs | 1.4 |
| 2.7 | **Alert scoring:** each alert is resolved at 20 and 60 days against the ETF | The precision of each rule appears in the report | 2.4 |

**Exit (§17):** 20 digests delivered; shadow thresholds calibrated on your real portfolio, including the risk-share rule; no more than 3 urgent alerts a week.

---

## 8. Phase 3: research (about 95 h). Needs the Kite backfill, which is approved. It can overlap with Phase 2.

| WP | Builds | Tests | Depends on |
|---|---|---|---|
| 3.1 | **Backfill loader** from Kite Connect, using one paid month on a data-only Zerodha account:<br>• **Spike (2 h):** do Kite's daily candles already include split and bonus adjustments? If yes, store them with `price_basis = adjusted` and skip the read-time adjustment for those rows. Dividends always come from `corp_actions`.<br>• download the full daily history of all current NIFTY 500 names, plus your holdings, within the month<br>• `known_at_synthetic = true` on every row<br>• the survivorship gap is recorded in the gate report<br>• **the lockbox is sealed on load:** the most recent ~2 years go to `data/lockbox/`, and their hash goes into `system_state` | The research loader refuses lockbox dates. The seal hash is stable. | 1.3, Zerodha account |
| 3.2 | **Spike (4 h):** how much work a point-in-time NIFTY 500 membership history takes to build from circulars. Then build it. | Membership on a historical date matches the circular | 2.1 |
| 3.3 | Feature set v1 (§5 of the architecture: trend, momentum, distance from the 52-week high, volatility, delivery), versioned | Perturbing future rows leaves the features for D unchanged | 1.6, 2.2 |
| 3.4 | **Labels:**<br>• total return from the D+1 open to 60 trading days later, against the cross-sectional median<br>• the delisting rule and the `close_regime` flag<br>• **Spike (1 h):** confirm the Closing Auction Session date | Hand-worked labels across a dividend, a split and a delisting | 3.2, 3.3 |
| 3.5 | **Harness:**<br>• `pi.research.run_trial()` writes to `trials` before returning anything; `trials` is append-only<br>• `gate_spec.yaml` is hashed and logged with each trial<br>• walk-forward on an expanding window, with purging and an embargo | No code path returns metrics without a trial row. Deleting a trial fails. | 0.5, 3.4 |
| 3.6 | **Models:**<br>• a linear 3-factor baseline against LightGBM<br>• Platt calibration<br>• the decile edge table against the beta-adjusted ETF | AUC above 0.57 fails the build (the leakage alarm). The baseline is always reported alongside. | 3.5 |
| 3.7 | **Gate:**<br>• `pi gate evaluate` is run by **you** and reads the lockbox exactly once<br>• the verdict is recorded in `system_state` | A second lockbox read is refused. The verdict and spec hash are stored. | 3.6 |

**Exit (§17):** the gate verdict is recorded. **FAILED is a valid outcome.** It means Phase 4 is skipped and the system stays as it is: ledger, tax, analyser and alerts, with HOLD or the ETF as the default. That is the most likely result, given that a realistic AUC is about 0.52–0.56.

---

## 9. Phase 4: paper decisions (about 42 h, then a 3-month wait). Only if the gate verdict is PAPER.

| WP | Builds | Tests | Depends on |
|---|---|---|---|
| 4.1 | A month-end book of predictions for the whole universe (ADR-0015), in `predictions` | Covers every universe member as of D | 3.7 |
| 4.2 | **Decide:**<br>• HOLD, REVIEW, TRIM, ADD and AVOID rules<br>• `expected_edge_inr` against `hurdle_inr`<br>• minimum sell of ₹5,000 or the whole holding, at most one TRIM per quarter, and a 30-day cooldown (ADR-0006)<br>• the screen shows tax and cost, never the purchase price | One test per rule. Nothing is ever emitted that `guard` would reject. | 4.1, 1.13, 1.10 |
| 4.3 | `user_actions`, and `pi replay <suggestion_id>` | Replay reproduces the suggestion byte for byte | 4.2 |
| 4.4 | **Evaluate:**<br>• resolve each prediction and attribute the result (market, sector, residual)<br>• classify misses in code<br>• calibration and Brier-skill CI using HAC or a month-block bootstrap<br>• PSI drift, and the monthly report | Hand-worked attribution cases. Each miss class is reachable. | 4.1 |

**Exit (§17):** 3 months with no pipeline bugs, then your approval → VISIBLE.

## 10. Phase 5 and 6

- **Phase 5 (about 10 h):** template explanations, plus a validator that checks every number in an explanation against the suggestion's data. An LLM stays off by default (ADR-0009).
- **Phase 6:** optional, after one year of ledger history (R9).

---

## 11. Dependencies, added per phase

| Phase | Runtime | Dev |
|---|---|---|
| 0 | pydantic, pyyaml, typer | pytest, hypothesis, ruff, import-linter |
| 1 | polars, duckdb, pyarrow (WP 1.2–1.3); keyring, httpx (WP 1.5) | — |
| 2 | streamlit, scikit-learn (Ledoit-Wolf) | — |
| 3 | lightgbm, statsmodels (HAC, Diebold-Mariano), kiteconnect (backfill only) | — |

**Pinning:** every version is pinned in `uv.lock`. A dependency upgrade is its own commit, so `git_sha` captures it.

## 12. Rough totals

| Phase | Focused hours | Fixed wait |
|---|---|---|
| 0 | ~16 | — |
| 1a | ~44 | — |
| 1b | ~95 | runs during the 60-day count |
| 2 | ~62 | 20 trading days |
| 3 | ~95 | — |
| 4 | ~42 | 3 months |
| 5 | ~10 | — |
| **Total** | **~365** | |

**Example timeline at 8 focused hours a week** (an assumption):
- Phase 0 and 1a take about 8 weeks.
- The 60-day count (about 13 weeks) mostly covers the 1b work (about 12 weeks).
- Phase 2 and 3 take about 20 more weeks.
- If the gate passes, 3 months of paper trading follow.

That puts first visible suggestions about **12–14 months** away. The ledger, tax and alerts, which are the most useful parts, arrive within about **5 months**.

## 13. Risks specific to the build

| Risk | Mitigation |
|---|---|
| You skip days of dropping files in, so the 60-day count keeps resetting | A 🔴 DATA alert every evening when a file is missing. Days can be filled in later. |
| A coding agent tunes the model against the backtest | WP 0.5 comes before any research code; the trial wrapper; the lockbox; only you run `pi gate evaluate` |
| Golden files are missing, so parsers are "done" on guesses | §3 checklist. A WP isn't done without its fixture. |
| Phase 3 effort sunk, then the gate fails | Expected and acceptable. Phases 1–2 are worth having on their own. |
| Scope creep into the model before the ledger is correct | Phase order is enforced: the Phase 1 exit criteria come first |
