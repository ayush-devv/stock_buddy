# Requirements: Portfolio Intelligence

Date: 2026-11-23. Architecture: [architecture.md](architecture.md)

## Functional requirements

| ID | Requirement | Source |
|---|---|---|
| F1 | Import holdings, the tradebook and tax P&L from the broker. Keep FIFO tax lots per demat account. | User: "analyse our portfolio" |
| F2 | Load the daily NSE bhavcopy, delivery, index/TRI, corporate-action and surveillance files from an inbox folder | Research R1 |
| F3 | Calculate costs and tax: dated STCG/LTCG rules, LTCG headroom, DP charges, buyback/bonus/demerger handling | Research R2, review E4–E6 |
| F4 | Portfolio analyser: concentration, active sector weight, beta, risk contribution, drawdown, scenario replays, after-tax comparison with an ETF shadow | Research R3 |
| F5 | One suggestion per held or watched stock: HOLD/REVIEW/TRIM/ADD/AVOID, with HOLD as the default, gated by cost, tax and tradability | User: "suggestion on each stock" |
| F6 | A calibrated probability per stock, shown **only** after the model passes the go/no-go gate | Research R5 |
| F7 | Learning loop: log predictions, resolve outcomes, attribute them, recalibrate quarterly, retrain annually, promote only with approval | User: "check prev result… learn… improve" |
| F8 | Monitor filings and news for held or watched stocks, and send alerts. These are never inputs to the model. | User: "scrape news and monitor stock" |
| F9 | Advance alerts for scheduled events (results, ex-dates, lots becoming long-term, trading-window closures) and risk changes. No predictions of direction. | User: "alert before…" |
| F10 | Guard: block actions during insider-trading restrictions, under exchange surveillance, or on shares not yet credited | Review E1, E3, E11 |

## Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| N1 | Point-in-time correctness | Zero leakage-test failures. No query for date D sees `known_at > D`. |
| N2 | Ledger accuracy | Ledger = broker holdings exactly. Contract notes match within ₹1 (excluding DP charges). |
| N3 | Freshness | Digest by 19:30 IST on trading days when the Mac is awake and files are in the inbox |
| N4 | Alert load | ≤ 3 urgent alerts a week; one digest a day |
| N5 | Reproducibility | `pi replay <suggestion_id>` reproduces any past suggestion |
| N6 | Operating cost | ₹0 a month on an ongoing basis. The one-off backfill is ≤ ₹500. |
| N7 | Operator time | ≤ 5 minutes a trading day (file downloads) |
| N8 | Privacy | Data stays on your Mac. Telegram messages carry only ticker, rule and severity. |
| N9 | Storage | < 0.5 GB for 10 years |

## Constraints

| ID | Constraint | Source |
|---|---|---|
| C1 | One person; about ₹20k capital; personal account only | User |
| C2 | No automated collection from nseindia.com (terms of use) | NSE terms of use |
| C3 | No automated order placement; you place every order yourself | ADR-0005 |
| C4 | Outputs never shared with anyone else (SEBI RA/IA framework) | SEBI RA Regs 2014 (amended Dec 2024) |
| C5 | SEBI insider-trading rules apply if you or a relative are a designated person at a listed company | SEBI PIT Regs 2015 |

## Assumptions (unconfirmed)

| ID | Assumption | Status |
|---|---|---|
| A1 | Broker is **Groww** | **Confirmed 2026-11-23** |
| A2 | Single Mac, often asleep | Unconfirmed |
| A3 | Alerts by Telegram | Unconfirmed |
| A4 | Python 3.12 + uv | Unconfirmed |
| A5 | NSE files downloaded by hand | Follows from C2 |
| A6 | One-month Kite Connect backfill (₹500), using a free Zerodha account for data only | **Approved 2026-11-23** |
| A7 | Universe: NIFTY 500 members as of each date + holdings; SME series excluded | Unconfirmed |
| A8 | Insider-trading status | **Confirmed 2026-11-23:** not a designated person; `restricted.yaml` empty |
