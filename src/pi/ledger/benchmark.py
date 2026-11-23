"""ETF benchmark (WP 1.11, requirement F4): what if every rupee had gone into the ETF?

A counterfactual, computed in its own world:
- It mirrors your **net cash flow per day**, not individual trades. Money you put in
  (purchases, opening holdings at cost) buys ETF units; money you take out (sale and
  buyback proceeds) sells them. A same-day switch from stock A to stock B is a zero net
  flow, so the benchmark doesn't trade.
- It pays the same Groww charges (ETF rates) and keeps leftover rupees as cash.
- Its tax is its own: its own realized gains and its own Rs 1.25 lakh limit. It never feeds
  the hurdle, the guard or your real tax.

`after_tax_value` liquidates any book on a date: sale charges, DP charge, and the extra
tax that liquidation would add to that book's own year. Use it on both your real book and
the benchmark book for the F4 comparison.

Limitations, noted rather than modelled:
- dividends on your stocks are not mirrored;
- proceeds held as cash and reinvested days later still make the benchmark sell and buy;
- whole ETF units only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pi.config import CostsConfig, TaxConfig
from pi.ledger.book import Book, Buy, Buyback, Entry, Opening, Sell, build
from pi.ledger.costs import Instrument, dp_charge, order_charges
from pi.ledger.tax import RealizedGain, compute_fy, fy_bounds, fy_start_year, holding_kind

ETF_ID = -1  # the benchmark's only security
PAISA = Decimal("0.01")


def _q(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


def _money(x: float) -> Decimal:
    return _q(Decimal(str(x)))


def net_flows(entries: list[Entry]) -> dict[date, Decimal]:
    """Cash you put in (+) or took out (-) per day, from the real ledger entries."""
    flows: dict[date, Decimal] = defaultdict(Decimal)
    for e in entries:
        if isinstance(e, Opening):
            flows[e.acquired] += _q(e.cost)
        elif isinstance(e, Buy):
            flows[e.trade_date] += _q(e.qty * e.price + e.charges + e.stt)
        elif isinstance(e, Sell):
            flows[e.trade_date] -= _q(e.qty * e.price - e.charges - e.stt - e.dp_charge)
        elif isinstance(e, Buyback):
            flows[e.bought_back] -= _q(e.qty * e.price - e.charges)
    return {d: v for d, v in sorted(flows.items()) if v != 0}


@dataclass
class Benchmark:
    entries: list[Entry]
    book: Book
    cash: Decimal
    shortfall: Decimal  # outflows the benchmark could not fund (it had less than you)


def _buy_charges(value: Decimal, costs: CostsConfig, d: date) -> tuple[Decimal, Decimal]:
    """(allowable charges, STT) for an ETF purchase."""
    c = order_charges("buy", float(value), costs.on(d), "etf")
    return _money(c.total - c.stt), _money(c.stt)


def mirror(
    entries: list[Entry],
    etf_price: Callable[[date], Decimal],
    costs: CostsConfig,
    tax: TaxConfig,
    demat: str = "benchmark",
) -> Benchmark:
    cash, shortfall, units = Decimal(0), Decimal(0), 0
    bench: list[Entry] = []
    for d, flow in net_flows(entries).items():
        price = etf_price(d)
        cash += flow
        if cash > 0:
            n = int(cash // price)
            while n > 0:
                charges, stt = _buy_charges(n * price, costs, d)
                if n * price + charges + stt <= cash:
                    break
                n -= 1
            if n > 0:
                bench.append(Buy(ETF_ID, demat, d, n, price, charges, d, stt))
                cash -= _q(n * price + charges + stt)
                units += n
        elif cash < 0:
            need, n = -cash, 0
            while n < units:
                n += 1
                s = order_charges("sell", float(n * price), costs.on(d), "etf")
                net = n * price - _money(s.total) - _money(dp_charge(float(n * price), costs.on(d)))
                if net >= need:
                    break
            if n > 0:
                s = order_charges("sell", float(n * price), costs.on(d), "etf")
                dp = _money(dp_charge(float(n * price), costs.on(d)))
                bench.append(
                    Sell(ETF_ID, demat, d, n, price, _money(s.total - s.stt), dp, _money(s.stt))
                )
                cash += _q(n * price) - _money(s.total) - dp
                units -= n
            if cash < 0:
                shortfall += -cash
                cash = Decimal(0)
    return Benchmark(bench, build(bench, tax), cash, shortfall)


def after_tax_value(
    book: Book,
    cash: Decimal,
    price: Callable[[int], Decimal],
    on: date,
    costs: CostsConfig,
    tax: TaxConfig,
    instrument_of: Callable[[int], Instrument] | None = None,
) -> Decimal:
    """What the book is worth if sold out on `on`, after charges and the extra tax that
    sale would add to this book's own financial year.

    `instrument_of` says which securities are ETFs (refdata supplies it: ETF units have
    INF... ISINs, shares INE...). Without it, only the benchmark's own ETF_ID is an ETF."""
    instrument_of = instrument_of or (lambda s: "etf" if s == ETF_ID else "equity")
    fy = fy_start_year(on)
    start, end = fy_bounds(fy)
    so_far = book.realized_gains(start, end)
    value, extra = cash, []
    by_security: dict[int, list] = defaultdict(list)
    for lot in book.open_lots():
        by_security[lot.security].append(lot)
    for security, lots in by_security.items():
        p = price(security)
        instrument = instrument_of(security)
        qty = sum(lot.qty for lot in lots)
        gross = _q(qty * p)
        charges = order_charges("sell", float(gross), costs.on(on), instrument)
        dp = _money(dp_charge(float(gross), costs.on(on)))
        value += gross - _money(charges.total) - dp
        dp_ok = bool(tax.value("dp_charge_is_transfer_expense", on))
        deductible = _money(charges.total - charges.stt) + (dp if dp_ok else Decimal(0))
        for lot in lots:
            share = _q(deductible * lot.qty / qty)
            gain = _q(lot.qty * p) - lot.cost - share
            extra.append(RealizedGain(on, holding_kind(lot.acquired, on, tax), float(gain)))
    tax_due = compute_fy(so_far + extra, tax, fy).total_tax - compute_fy(so_far, tax, fy).total_tax
    return _q(value - _money(tax_due))
