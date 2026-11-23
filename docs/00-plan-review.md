# 00 — Critical review of the "Institutional-grade F&O platform" plan

> **SUPERSEDED (2026-11-23).** The project moved from F&O to a private equity portfolio-intelligence tool. See [architecture.md](architecture.md). This file is kept for history only.

Date: 2026-11-21 · Status: draft for discussion
Confirmed constraints (2026-11-21): **solo builder, own account, capital ≈ ₹20,000 (raised from ₹5k), wants the system to run on its own.**

---

## 0. What ₹5,000 means. Read this first.

**Live F&O is not feasible at this size.**
- NIFTY lot ≈ 65 units (verify the current lot). An ATM weekly option at ₹100–200 premium costs **₹6,500–13,000 for one lot**, more than the whole account.
- The only things ₹5k can buy are far-OTM "lottery ticket" options. That is the exact pattern behind most of the retail losses in SEBI's studies.
- Any position with a short leg (spreads, iron fly) has brokers block margin far above ₹5k. Check on the broker's margin calculator.
- Round-trip costs (brokerage + STT + charges) are ~₹50+ per trade, **~1% of capital per trade** before any slippage.
- Sizing rule: risk ≤ 1–2% of capital per trade. At ₹5k that is a max loss of ₹50–100 per trade, which no F&O position can satisfy. **Live F&O needs roughly ₹1–2 lakh+** before position sizing works.

**"Leave it open and let it decide" is fine, but only in paper mode.**
- Fully unattended *live* trading is ruled out three ways:
  - by the plan's own governance (AI can't hold financial authority);
  - by regulation (daily 2FA login; API sessions logged out every day);
  - by the size of the account.
- Autonomy goes where it's safe: research runs and paper trading.

**So ₹5k is spent like this:**

| Track | What runs autonomously | Money at risk |
|---|---|---|
| **A. Research** | Daily download of NSE bhavcopy / VIX, feature updates, H1a/H1b tests, experiment ledger | ₹0 (free data) |
| **B. Paper trading** | The system makes its own daily decisions on live data with simulated fills and logs everything. You review weekly | ₹0 |
| **C. Real plumbing test** | Real orders for 1 unit of a NIFTY ETF via broker API to prove order lifecycle, reconciliation and the cost model end to end | A few hundred ₹ |
| **Reserve** | Not traded. API fees and a static-IP VM may cost a few hundred ₹ a month, so check this before spending | — |

**Unlock rule for live F&O, frozen now:** live F&O starts only when **all three** are true:
1. A strategy has passed the H1b gates.
2. **≥ 12 weeks** of paper trading track the backtest.
3. Capital is enough that the strategy's worst modelled loss per trade is ≤ 2% of the account.

Until then, paper results are the "profit" we are building toward.

**Update at ₹20,000:** the 2% rule allows a worst loss of **₹400 per trade**.
- The smallest defined-risk NIFTY trade (1 lot, narrow debit spread) typically has a worst loss of **₹1,000–3,000**, about 5–15% of the account.
- A single ATM option lot (₹6.5–13k) would be 30–65% of the account in one trade.
- So live F&O stays locked. The extra capital only adds headroom for Track C and the reserve.
- **The bottleneck is evidence, not capital.** Adding money doesn't create an edge. Any capital added later should be money you can afford to lose entirely.

---

## 1. Fix the goal first

"An AI that makes sure of profits" can't be built. No architecture can deliver it, and the source doc says so itself in its final paragraph. The context makes this worse:

- SEBI's FY25 study: **~91% of individual equity-derivative traders lost money**, with net losses of ~₹1.06 lakh crore (later revised to ~₹1.12 lakh crore).
- SEBI's FY26 study: **87.7% still lost money**, even after the 2024–25 curbs. Before costs, 82.1% were already losing. Costs then turn more of the marginal traders into losers.

**Reframed objective:**

> Find out, as cheaply and quickly as possible, whether we have a **repeatable, after-cost, capacity-feasible edge** in NSE index F&O. Trade it small if we do. **Stop** if we don't.

A "no edge found, stop" result in month 3 counts as a success, not a failure. It is far cheaper than finding out in month 15 with live capital.

---

## 2. What the doc gets right (keep these)

| Principle | Why it stays |
|---|---|
| Data integrity before models | Bad option data produces fake edges |
| Point-in-time / no look-ahead | The #1 silent killer of backtests |
| Baselines (logit, HAR-RV, GARCH) before deep learning | Shows whether there is any signal at all |
| Predict distributions or targets, not "BUY/SELL" | Separates the forecast from the trade decision |
| Target = **executable net return**, not mid-price | The mid-price is not tradable |
| Independent evaluation and fixed promotion gates | Stops self-grading |
| Multiple-testing ledger, DSR, PBO | Corrects for the number of things we tried |
| Risk gateway the strategy code can't bypass | Bugs and bad models will happen |
| "Research can propose, risk can veto" | Correct separation of authority |
| Capacity curves, strategy diversity | Real-world constraints |

The *ideas* are sound. The *scale* is the problem.

---

## 3. Where the plan is wrong for us

### 3.1 It's a 15-month, multi-team bank build

Kafka/MSK, Iceberg, EKS, Terraform, a Java control plane, Rust, Feast, a LangGraph + Qwen multi-agent system, TFT/TimesFM, four AWS environments. That is several engineer-years before the first answer to the only question that matters: *is there an edge?*

The doc contradicts itself: it says "data first, baselines first, delay AI", then specifies the full distributed stack up front.

**Right-size the stack until an edge is proven:**

| Doc says | We use now | Upgrade trigger |
|---|---|---|
| Kafka / Redpanda | Python scripts + cron | Live multi-strategy feed handling |
| S3 + Iceberg | Local Parquet, partitioned by date/underlying | Data > laptop disk or several people |
| EKS / Terraform / 4 envs | One machine (laptop or one small VM) | Paper trading needs 24×5 uptime |
| Java control plane, Rust | None | Never, unless latency is measured as the bottleneck |
| Feast feature store | A `features/` Python module with versioned functions | Several live models sharing features |
| NautilusTrader | A hand-rolled, vectorised daily/intraday backtester | Intraday execution simulation is needed |
| MLflow + 4 registries | A git commit + `experiments.parquet` ledger | Over ~50 experiments, or a team |
| LangGraph + Qwen agent factory | **Deferred** (see 3.2) | After the ledger and gates exist and one edge is manually validated |

What we keep: **Python, Polars, DuckDB, Parquet, statsmodels/arch, LightGBM.**

### 3.2 The LLM research factory makes overfitting *worse* if built early

Every auto-generated hypothesis is another trial. An agent that runs 500 backtests a week is a false-discovery machine unless all three of these already exist:

1. A **trial ledger** that counts every attempt, including abandoned ones.
2. **Pre-registered pass/fail thresholds**, frozen before the search runs.
3. A **sealed holdout** that the agent cannot touch.

DSR and PBO only work if the trial count is honest. An LLM sweeping indicator combinations quickly makes the effective trial count enormous, which deflates every result toward zero.

→ AI orchestration is the **last** thing to build, not Phase 4 of 6. Its first useful role is narrower anyway: literature summarisation, writing test code, and adversarially reviewing *our* backtests for leakage.

### 3.3 Cut microstructure, L2/L3 and HFT ideas

We trade through a retail broker API: no colocation, network latency in the tens to hundreds of milliseconds, and an order-per-second threshold (see §5). Order-book-imbalance signals at that latency belong to the colocated firms.

- Drop: L3/tick-by-tick purchase, queue-position modelling, order-book simulator.
- Realistic horizons: **15 minutes → multi-day**, with daily as the first pass.
- This also removes the most expensive data products.

**The doc's own "first research question" depends on order-book imbalance.** Replace it with the question in §6.

---

## 4. The constraint the doc underweights: the market changed recently

The F&O rule changes of 2024–2026 mean older history describes a *different market*:

- **Weekly expiries cut to one benchmark per exchange.** On NSE only NIFTY keeps weeklies, and BANKNIFTY weeklies are gone. This undercuts the doc's "NIFTY + BANKNIFTY" universe for anything expiry-driven.
- **Expiry moved Thursday → Tuesday** (effective ~Aug–Sep 2025).
- **Lot sizes revised more than once.** For example, NIFTY went 75 → 65 with a staggered roll-in across Dec 2025–Jan 2026, and a further 2026 revision notice exists.
- Higher minimum contract value, upfront premium collection, extra expiry-day margin, and higher STT on options.

**Implications:**

1. The **contract-rules table** (lot size, expiry weekday, strike interval, tick size, margin regime, each with an *effective-from* date) is a **day-one artifact**, not a "missing component" for later.
2. **Comparable history is short.** Since the late-2024 regime there have been roughly **~100 NIFTY weekly expiries**. A once-a-week strategy therefore has only ~100 independent observations, which is thin for statistical confidence. Choose hypotheses with **more independent observations per year** (daily or intraday decisions), or accept wide confidence intervals and smaller sizing.
3. Pre-2024 data stays useful for *underlying* behaviour (volatility dynamics, regimes) but should be treated with suspicion for *options-flow / expiry-day* behaviour.

---

## 5. Two things that come before any signal work

### 5.1 The cost model (week 1, not month 5)

Every trade's P&L must subtract:

- Brokerage (per order)
- STT (options: on sell-side premium, and on intrinsic value if exercised; futures: on sell side)
- Exchange transaction charges
- SEBI turnover fee
- Stamp duty (buy side)
- GST (on brokerage + exchange + SEBI fees)
- **Slippage / half-spread.** This is the big one for options, and it widens sharply for OTM strikes, near expiry, and in fast markets.

> Rates change with budgets and circulars. Store them in the rules table **with effective dates** and verify current values from the broker's charge sheet and NSE circulars. Never hardcode them.

Many intraday option-buying ideas die right here. Finding that out on day 5 is cheap.

Also model **margin**: a strategy that makes 2% a month on ₹X of margin is a very different product from one that makes 2% on premium.

### 5.2 Regulation for a personal setup (verify with the broker)

Under the SEBI retail-algo framework (NSE implementation, in force from 2026):

- API orders must come from a **registered static IP** mapped to your API key.
- Below **~10 orders/second per exchange**, self-built algos for your own or immediate family's account don't need full exchange registration, but orders are still tagged.
- Above that threshold, the algo must be registered through the broker.
- 2FA, API session logout at end of day, and audit trails are required.

Our design (low-frequency, daily to intraday) stays well under the OPS threshold. **Serving anyone else's money or selling signals is an entirely different regulatory category. Don't drift into it.**

---

## 6. Proposed first hypothesis (replaces the doc's order-book question)

**H1: The NIFTY volatility risk premium.** It is split into two steps so we don't build a trade on a thin sample.

**H1a: statistical check, no trade structure.** Does daily India VIX (or NIFTY ATM IV) systematically exceed the *subsequent* realised vol of NIFTY over the matching horizon?
- Full long history (10+ years of daily data), so there are thousands of observations.
- The windows overlap, so use **Newey-West / HAC standard errors**.
- Also report the result by regime (pre/post Nov 2024, VIX terciles).
- **Gate:** if there's no statistically and economically meaningful premium, stop here. Don't build H1b.

**H1b: tradable test, only if H1a passes.** Is the premium large enough to pay for costs and tail losses in a **defined-risk** structure?
- **Caveat:** H1b's post-2024 sample is only ~100 weekly expiries. Short-vol payoffs are negatively skewed, so a sample with no big gap day can look profitable by luck. Treat any result as provisional and size small.

- **Why this one:** it is economically motivated (the insurance-selling premium), well documented globally, testable on **daily** data, and needs no order book.
- **Why it might fail:** it is crowded in India, the tails are brutal (gap days), costs and spread on the short legs are significant, and margin ties up capital.
- **Structure tested:** defined-risk only (for example an iron fly or iron condor), never naked short options. We test the tail risk, not just the average.
- **Data needed:** NSE F&O bhavcopy (free daily per-contract OHLC/settle/OI, long history), NIFTY spot/futures daily, India VIX. Intraday 1-min options data comes later and only if H1 survives.
- **Bhavcopy prices are not executable.** Close and settle carry no bid/ask, and OTM wing closes can come from a handful of trades. Assume an explicit **half-spread that varies by moneyness × days-to-expiry**, and make that assumption the main target of the 1.5× cost stress. Otherwise the free-data test overstates fills on exactly the wing legs an iron fly or condor depends on.
- **Pre-registered pass rule (frozen before running):**
  1. Net expectancy > 0 after full costs **and** after a 1.5× cost/slippage stress.
  2. Positive in a majority of walk-forward folds, including the post-Nov-2024 regime on its own.
  3. Worst single-expiry loss and max drawdown within a bound we set *before* looking.
  4. Survives removal of the single best month.
  4b. **Tail replay:** replay historical gap shocks from pre-2024 data against *current* lot, margin and cost rules, for example Mar 2020 and the 4 Jun 2024 election-result day. Losses must stay within the pre-set bound. This is the event most likely to be *missing* from the post-2024 sample.
  5. Beats the "do nothing" baseline and a naive always-on version of itself.
- **Kill rule:** if it fails rules 1–2, H1 is rejected and recorded in the ledger. We don't tune it until it passes.

Candidate H2/H3 for later, with the same discipline: (a) HAR-RV forecast vs IV as a *filter* on H1; (b) a regime filter (VIX level/slope) that switches H1 off; (c) futures basis / roll behaviour.

---

## 7. Right-sized roadmap with kill points

| Phase | Weeks | Deliverable | Exit / kill criterion |
|---|---|---|---|
| **0. Setup** | 1 | Broker choice (API + historical data), `rules` table v0, cost model v0, experiment ledger | Can reproduce the broker's contract-note charges for a sample trade to the rupee |
| **1. Data** | 2–3 | Bhavcopy ingest → Parquet, contract master with lot/expiry history, data-quality checks (gaps, zero vs missing, expiry calendar) | A clean, point-in-time daily options panel for NIFTY |
| **2. H1 test** | 4–6 | H1a stats check (VIX vs subsequent RV, HAC errors). If it passes: vectorised daily backtester, H1b run under the pre-registered rules, report | **Pass → Phase 3. Fail → try H2/H3 (max 3 hypotheses), then stop or rethink** |
| **3. Intraday fidelity** | 7–10 | Buy 1-min options data *only now*, retest the survivor with realistic entry times and spreads | Edge survives intraday entry and spread |
| **4. Paper trading** | 10–18 | Live data, simulated fills, daily reconciliation, risk gateway v1 (max loss, max lots, stale-data kill switch) | ≥ ~8–12 weeks paper P&L consistent with backtest |
| **5. Small live** | 18+ | Minimum lot sizes, hard capital cap, kill switch, daily review | Live slippage ≈ modelled slippage |
| **6. Then scale tooling** | — | *Now* consider agents, more data, more infra | Only if ≥ 1 live edge exists |

---

## 8. Open decisions (need answers from us)

1. **Capital range.** This sets which structures are feasible given margin and lot sizes.
2. **Solo or team, and hours per week.** This sets the realistic pace.
3. **Broker.** Zerodha / Dhan / Upstox / others: compare API limits, historical options data (do they serve *expired* contracts?), static-IP handling, and the charge sheet.
4. **Data budget.** Free bhavcopy only at first, or a paid 1-min vendor from the start.
5. **Risk tolerance.** The max acceptable drawdown, set *before* any backtest is seen.

---

## Sources

- SEBI FY25 study coverage: [Business Standard](https://www.business-standard.com/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html), [Moneylife](https://moneylife.in/article/91-percentage-of-retail-traders-lost-money-in-derivatives-losses-in-fo-surged-41-percentage-to-rs105-lakh-crore-in-fy25-sebi-study/77613.html)
- SEBI FY26 study coverage: [Business Today](https://www.businesstoday.in/personal-finance/story/87-7-of-fo-traders-lost-money-in-fy26-despite-sebi-curbs-why-retail-investors-keep-losing-567419-2026-11-14), [Open Magazine](https://openthemagazine.com/business/sebi-fo-loss-study-explained-why-9-in-10-retail-traders-lost-91685-crore-in-fy26)
- Expiry move to Tuesday: [Business Standard](https://www.business-standard.com/amp/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html), [Zee Biz](https://www.zeebiz.com/market-news/news-nse-revises-expiry-day-nifty-banknifty-stock-derivatives-to-expire-on-tuesdays-370897)
- Lot size revisions: [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025), [Kotak Neo 2026 notice](https://www.kotakneo.com/news/market-news/index-derivative-lot-size-revision-2026/)
- Retail algo framework: [Zerodha explainer](https://zerodha.com/z-connect/business-updates/explaining-the-latest-sebi-algo-trading-regulations), [Kotak summary](https://www.kotaksecurities.com/investing-guide/articles/retail-algo-trading-rules-nse-circular), [Fyers](https://fyers.in/blog/sebi-algo-trading-rules-and-regulations-in-india/)

> Secondary sources were used for speed. Before relying on any rule or rate, confirm it against the primary SEBI/NSE circular.
