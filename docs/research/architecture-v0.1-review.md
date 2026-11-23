# Review of architecture v0.1: correctness, harness, and predictive power

Status: final (2026-11-23). Section E was checked against SEBI, NSE and tax sources. Item A5 ("store as ISIN") is overruled by E2: use an internal `security_id`. All fixes are folded into [architecture v0.2](../architecture.md), §20.

## A. Correctness: data and ledger

1. **The design never says what price the system assumes you trade at.**
   - Bhavcopy is published around 18:00 and the digest goes out around 19:00, so the earliest you can act is the next day.
   - Labels, backtest and the ledger's paper P&L must therefore start the horizon at the open of the next trading day (D+1), or at its VWAP. They must not start at the close of D.
   - Without this, every backtest is optimistic by one day of drift and reversal.
2. **The append-only raw layer conflicts with NSE corrections.** NSE sometimes re-issues a day's file. Raw storage needs to be bitemporal:
   - Add an `ingest_seq` to the key (trade_date, isin, series, ingest_seq).
   - Views should take the latest version known as of the query time.
3. **The `suggestions` table is not really append-only.** `user_action` and `user_note` get filled in later. Move them to a separate append-only `user_actions` table.
4. **Labels must be total return.** The 60-day label has to include dividends from the `corp_actions` table. Two gaps remain:
   - The design doesn't say how a stock that is delisted during the horizon is handled. Use the last traded price, or −100% for a forced delisting.
   - The universe for each date has to be NIFTY 500 membership as known on that date.
5. **Series changes look like the stock vanished.** When a stock moves from EQ to BE (trade-for-trade) or BZ, keying on series breaks its price history.
   - Build price history per ISIN, across series.
   - Store the series as a feature and an alert in its own right, since a move to T2T is a surveillance signal.
6. **Corporate-action adjustment as of date D must use only actions with `ex_date ≤ D`.** The draft also misses some action types:
   - Missing types: rights issues and rights entitlements, demergers, mergers or schemes, buybacks, and changes in face value.
   - Each needs tax-side handling in the ledger, not just price adjustment: cost apportionment for demergers, and the bonus/buyback rules (being verified).
7. **Opening lots are a real gap.**
   - The broker's holdings CSV gives only an average cost, not lot-level FIFO.
   - Opening tax lots must come from the full tradebook history, or from a manual opening-lot file reconciled against the broker's tax P&L.
8. **Two processes writing to DuckDB will collide.** DuckDB allows only one read-write process.
   - If the Streamlit UI holds a read-write connection, the evening job fails while the UI is open.
   - Fix: the UI opens the database read-only; only the job writes.
9. **The backfilled `known_at` is made up.** For the Kite backfill, `known_at` is synthetic (assumed 18:00 on the trade date).
   - Add a `known_at_synthetic` flag.
   - Fundamentals typed in by hand need their filing timestamp taken from the exchange filing, not the date they were typed.
10. **The trading calendar has special cases.** Muhurat sessions and special Saturday sessions (for example Budget day) exist.
    - Treat the presence of a bhavcopy as the source of truth for "was this a trading day".
    - Keep a holiday list only to detect missing days.

## B. Correctness: decision and evaluation logic

1. **The TRIM/ADD gate needs a number the model doesn't produce.**
   - The gate compares expected edge, in rupees, against cost plus tax.
   - A calibrated P(beat median) gives a probability, not a size.
   - Fix: map probability decile → mean realised excess return from the walk-forward test (a decile lookup table), with a shrinkage factor. Expected edge = that decile mean × position value.
2. **The predictions are scored on too few stocks.** Suggestions exist only for the 4–6 held stocks, so evaluating them has almost no power.
   - Add a universe-wide `predictions` table: all ~500 stocks, one snapshot a month, non-overlapping.
   - Calibration and Brier skill are measured on this table. Suggestions stay limited to holdings and the watchlist.
3. **Overlapping horizons inflate the sample size.** Daily predictions with a 60-day horizon mean consecutive outcomes share most of their days. Evaluate only monthly non-overlapping snapshots, or use HAC standard errors.
4. **"Brier skill > 0" is too weak a gate.** Require all three:
   - The lower bound of a date-block bootstrap confidence interval is above 0.
   - The test is pre-registered before it is run.
   - AUC is significantly above 0.5, as well as below the leakage alarm.
5. **There is a mismatch between the label and the decision.**
   - The model predicts against the median stock.
   - TRIM and ADD decisions are made against holding the ETF instead.
   - Both are fine together only if the decile lookup in point B1 measures excess return against the ETF, beta-adjusted.
6. **The 35% risk-share threshold will fire constantly.** With 4–6 stocks, one high-volatility name almost always exceeds it. Calibrate thresholds on your actual portfolio during Phase 2, before switching on REVIEW.
7. **Alerts are never scored.**
   - Log the outcome of each alert too: after a REVIEW or drawdown alert, did the stock underperform over the next 20 or 60 days?
   - This makes alert rules subject to the same evidence test as the model.

## C. Harness: operations and agents

1. **Instant alerts are only instant while the Mac is awake.** A filing at 11:00 on a day the laptop is shut reaches you whenever the laptop opens.
   - State that latency limit honestly.
   - Option: run the RSS poller alone on a free scheduled cloud job (no portfolio data, just the holdings list as ISINs), or accept the delay.
2. **There's no dead-man's switch.** If the job silently doesn't run, nothing tells you.
   - Add an external heartbeat that pings a free monitor on success.
   - A missed ping emails you.
3. **Reproducibility gaps.** Each suggestion should record:
   - the git SHA;
   - the versions of `rules.yaml`, `costs.yaml` and `tax.yaml`;
   - the feature-set version;
   - the hash of the raw data snapshot.

   Without these, a past suggestion can't be re-derived.
4. **The biggest harness risk is an AI coding agent working on the research.** If Claude Code, or any agent, iterates on features or hyperparameters against the backtest, that is automated data-snooping on a massive scale.
   - Every backtest call goes through one wrapper that writes to `trials` automatically. The agent can't skip the log.
   - Keep a sealed lockbox: the most recent about 2 years, stored separately.
   - Evaluate on the lockbox once, by you, at the gate.
   - Any agent-run code that touches the lockbox fails the build.
5. **LLM triage of RSS items, if added later:**
   - Structured output only, from a fixed category list.
   - No tools, no memory.
   - A pinned model version.
   - A golden set of about 100 labelled filings with a regression score.
   - Item text treated as data, never as instructions.
6. **Telegram is not end-to-end encrypted for bots.** Keep messages minimal: tickers and alert type, with no rupee amounts or holdings totals. Let the details live in the local UI.
7. **Keychain access.** A LaunchAgent can read the login Keychain only while you are logged in. That's fine, but document it.

## D. Predictive power: what it can and can't call

| Event type | Predictable? | What the system can honestly do |
|---|---|---|
| Direction of a stock vs its peers, 60 days | Barely (AUC ≈ 0.52–0.56) | A calibrated ranking with a small lift, after it passes the gate |
| Direction on results day | Effectively no | Nothing |
| Size of the results-day move | Partly (volatility clusters, and the stock's own history) | "Usual move ±X%" range |
| Volatility over the next 20 days | Yes, reasonably | EWMA/GARCH forecast; risk alerts |
| Scheduled events (results, ex-dates, LTCG dates) | Yes, deterministic | Calendar alerts |
| Unscheduled news (fraud, pledges, orders) | No: detected after it's published | Fast alert after the filing; the price usually moves first |
| A market crash | No | Scenario replays show exposure, not timing |
| Tax and cost consequences | Exact | The strongest output |

## E. SEBI, exchange and tax issues (checked against sources, 23 Nov 2026)

| # | Issue | Fact | What changes in the design |
|---|---|---|---|
| E1 | **Insider-trading rules** (applies if you, or a relative, are a designated person at any listed company) | PIT Regulations: the trading window is closed from quarter end until 48 hours after results; pre-clearance per the company's code; contra trade blocked for at least 6 months; from Apr 2025 the PAN freeze also covers immediate relatives. Reg 4 bans trading in any stock while holding its unpublished price-sensitive information. | Add `restricted.yaml`. `decide` blocks TRIM/ADD on restricted stocks during window closures and inside 6 months of an opposite trade. |
| E2 | **ISIN is not stable** (contradicts my point A5) | Splits, consolidations and demergers issue a new ISIN | Use an internal `security_id` plus an `isin_map(old, new, effective_date)` table |
| E3 | **Surveillance status decides whether you can trade at all** | ASM, GSM and ESM stages; T2T (series BE) and BZ; GSM stage 3 and above trades only weekly; brokers block buys from GSM stage 2; price bands of 2/5/10/20% | New `surveillance` table and a 🔴 alert when a held stock enters a stage. `decide` hides any action the stock can't execute. |
| E4 | **Buyback tax changed again** | Buyback proceeds were treated as dividend from 1 Oct 2024 to 31 Mar 2026. Under the Finance Act 2026 they are capital gains again from 1 Apr 2026. | `tax.yaml` needs dated rules for 3 periods |
| E5 | **No 87A rebate on STCG** under the new regime | STCG costs the full 20% plus cess even on low income (confirmed for FY25-26, FY26-27 likely) | The TRIM hurdle always uses 20.8% |
| E6 | **The ₹1.25L LTCG exemption is shared across everything you sell** | It is one limit per person per year | The ETF shadow and real lots draw on the same headroom |
| E7 | **NSE RSS polling conflicts with NSE's terms** | NSE has official RSS feeds, but its terms ban "any automated data collection" and make no exception for RSS | Inconsistent with ADR-004. Choose: BSE RSS (no clause found) or broker alerts or manual checks. NSE RSS stays unresolved. |
| E8 | **Reg 29 gives only 2 working days' notice** of results board meetings | Confirmed | Drive the "results ahead" alert from the board-meeting feed, not a fixed 3-day rule |
| E9 | **Pledge disclosures and surveillance moves come from different sources** | Pledges are filed under SAST Reg 31; surveillance moves are exchange actions, not company filings | The critical-filing rule needs 3 sources: company filings, pledge disclosures, exchange surveillance lists |
| E10 | **Closing Auction Session** | Changes how the official close is set for F&O stocks. Live date conflicts between sources (3 Aug or 7 Dec 2026). | Mark a structural break in close prices; check labels across the date |
| E11 | **Corporate actions create holdings with no trade** | Bonus, split, demerger and rights-entitlement credits arrive 1–3 days or weeks after the ex/record date (T+1 settlement) | Corporate actions write ledger entries, or the reconciliation alert fires falsely. Block TRIM on shares not yet credited. |
| E12 | **DP charge is not on the contract note** | ₹15.34 per stock per sell day is billed to the ledger | Exclude it from the contract-note match, include it in the cost hurdle |
| E13 | **The static-IP reason in ADR-005 is wrong** | Static IP applies only to placing, modifying and cancelling orders; read-only API use doesn't need it | Correct the ADR's reasoning; the decision stands on cost and security |
| E14 | SEBI RA/IA definitions | Both turn on "for consideration" and "in the business of", so personal use is outside them. Since Dec 2024, RAs/IAs are solely responsible for output from AI tools they use. | No change. Re-check this first if anything is ever shared. |
| E15 | Smaller tax rules | Same-day buy and sell is speculative income; s.94(7) dividend-stripping rule; dividend TDS threshold is ₹10,000 from FY25-26 | Rules in `tax.yaml` plus a same-day sell warning |
