"""Ledger core (WP 1.8): ordered entries in, FIFO lots and realized gains out.

Pure and fully rebuildable. Money is Decimal, rounded to the paisa only when a lot is
split, and every split conserves cost exactly (the remainder stays on the lot).

Rules:
- Cost basis = quantity x price + allowable buy charges (brokerage, exchange and SEBI fees,
  stamp duty, GST). STT is never part of cost or of sale expenses.
- FIFO per demat by the date shares *entered* the demat (CBDT Circular 768), then entry
  order. The holding period still runs from the acquisition date; a lot keeps both dates.
- A sale first matches shares bought the same day in that demat (speculative income,
  s.43(5)); the rest is FIFO. **To confirm at the CA review.**
- The DP charge is a sale expense only if tax.yaml `dp_charge_is_transfer_expense` is true.
- Corporate actions apply before that day's trades:
  - split or consolidation: quantity x ratio, cost and dates unchanged;
  - bonus: one new lot per demat, cost nil, acquired on the allotment date;
  - demerger: a child lot per parent lot, with the parent's dates and the announced
    fraction of its cost;
  - merger: each lot converts to the new security, with cost and dates carried over;
  - buyback: regime from tax.buyback_regime (capital gains, exempt, or deemed dividend
    with the cost becoming a capital loss).
- Never guess: selling more than you hold, or a fractional entitlement, raises LedgerError.

Deferred: rights entitlements, cash in lieu of fractional shares on mergers, s.94(7).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

from pi.config import TaxConfig
from pi.ledger.tax import RealizedGain, buyback_regime, holding_kind

PAISA = Decimal("0.01")


class LedgerError(Exception):
    """The entries don't add up. Never papered over: raise a reconciliation alert."""


def _q(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


def _whole(qty: int, ratio: Fraction, what: str) -> int:
    out = qty * ratio
    if out.denominator != 1:
        raise LedgerError(f"{what}: {qty} x {ratio} = {out} is fractional; record the cash in lieu")
    return int(out)


# --------------------------------------------------------------------------- entries


@dataclass(frozen=True)
class Opening:
    """A holding that predates the tradebook (from a reconciled opening-lot file)."""

    security: int
    demat: str
    qty: int
    cost: Decimal
    acquired: date
    entered: date


@dataclass(frozen=True)
class Buy:
    security: int
    demat: str
    trade_date: date
    qty: int
    price: Decimal
    charges: Decimal  # allowable charges, STT excluded
    credit_date: date
    stt: Decimal = Decimal(0)  # kept for the record; never part of cost


@dataclass(frozen=True)
class Sell:
    security: int
    demat: str
    trade_date: date
    qty: int
    price: Decimal
    charges: Decimal  # allowable charges, STT excluded
    dp_charge: Decimal = Decimal(0)
    stt: Decimal = Decimal(0)


@dataclass(frozen=True)
class Split:
    security: int
    ex_date: date
    ratio: Fraction  # new shares per old share: 5 for a 1:5 split, 1/10 for a 10:1 consolidation


@dataclass(frozen=True)
class Bonus:
    security: int
    ex_date: date
    ratio: Fraction  # bonus shares per share held: 1 for 1:1, 1/2 for 1:2
    allotted_on: date
    credited_on: date


@dataclass(frozen=True)
class Demerger:
    parent: int
    child: int
    ex_date: date
    child_per_parent: Fraction
    cost_to_child: Fraction  # share of the parent's cost moved to the child (announced)
    credited_on: date


@dataclass(frozen=True)
class Merger:
    old: int
    new: int
    ex_date: date
    new_per_old: Fraction
    credited_on: date


@dataclass(frozen=True)
class Buyback:
    security: int
    demat: str
    announced: date
    bought_back: date
    qty: int
    price: Decimal
    charges: Decimal = Decimal(0)


Entry = Opening | Buy | Sell | Split | Bonus | Demerger | Merger | Buyback


def _date_of(e: Entry) -> date:
    if isinstance(e, Opening):
        return e.acquired
    if isinstance(e, Buy | Sell):
        return e.trade_date
    if isinstance(e, Buyback):
        return e.bought_back
    return e.ex_date


_RANK = {Opening: 0, Split: 1, Bonus: 1, Demerger: 1, Merger: 1, Buy: 2, Sell: 3, Buyback: 3}


# --------------------------------------------------------------------------- state


@dataclass
class Lot:
    lot_id: int
    security: int
    demat: str
    qty: int
    cost: Decimal
    acquired: date  # holding period runs from here
    entered: date  # FIFO order and credit: when the shares reached the demat
    origin: str


Kind = Literal["short", "long", "speculative", "exempt"]


@dataclass(frozen=True)
class Slice:
    lot_id: int
    security: int
    demat: str
    qty: int
    acquired: date
    sold_on: date
    proceeds: Decimal
    cost: Decimal
    expenses: Decimal
    kind: Kind
    origin: str  # sell | buyback

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost - self.expenses


@dataclass
class Book:
    lots: list[Lot] = field(default_factory=list)
    slices: list[Slice] = field(default_factory=list)
    slab_income: list[tuple[date, str, Decimal]] = field(default_factory=list)
    trades: list[tuple[date, int, str, str, int]] = field(
        default_factory=list
    )  # d, sec, demat, side, qty
    cost_in: Decimal = Decimal(0)  # every rupee of cost ever booked (for conservation checks)

    # ----------------------------------------------------------------- queries

    def open_lots(self, security: int | None = None, demat: str | None = None) -> list[Lot]:
        return [
            lot
            for lot in self.lots
            if lot.qty > 0
            and (security is None or lot.security == security)
            and (demat is None or lot.demat == demat)
        ]

    def quantity(self, security: int, demat: str | None = None) -> int:
        return sum(lot.qty for lot in self.open_lots(security, demat))

    def credited_qty(self, security: int, demat: str, on: date) -> int:
        """Shares in the demat on `on` (guard input: a sale is limited to these)."""
        return sum(lot.qty for lot in self.open_lots(security, demat) if lot.entered <= on)

    def bought_on(self, security: int, demat: str, d: date) -> int:
        return sum(
            q
            for (td, s, dm, side, q) in self.trades
            if (td, s, dm, side) == (d, security, demat, "BUY")
        )

    def last_trade(self, security: int, side: Literal["BUY", "SELL"]) -> date | None:
        dates = [td for (td, s, _, sd, _) in self.trades if s == security and sd == side]
        return max(dates) if dates else None

    def realized_gains(self, start: date, end: date) -> list[RealizedGain]:
        """Taxable slices in [start, end] for the tax engine; exempt slices are left out."""
        return [
            RealizedGain(s.sold_on, s.kind, float(s.gain), f"lot {s.lot_id}")
            for s in self.slices
            if start <= s.sold_on <= end and s.kind != "exempt"
        ]


# --------------------------------------------------------------------------- engine


class _Engine:
    def __init__(self, tax: TaxConfig) -> None:
        self.tax = tax
        self.book = Book()
        self._next = 1

    def _new_lot(self, **kw) -> Lot:
        lot = Lot(lot_id=self._next, **kw)
        self._next += 1
        self.book.lots.append(lot)
        return lot

    def _take(self, lot: Lot, qty: int) -> Decimal:
        """Remove `qty` shares from a lot; return the cost that leaves with them."""
        cost = lot.cost if qty == lot.qty else _q(lot.cost * qty / lot.qty)
        lot.qty -= qty
        lot.cost -= cost
        return cost

    def _consume(self, security: int, demat: str, qty: int, d: date, same_day_first: bool):
        lots = self.book.open_lots(security, demat)
        held = sum(lot.qty for lot in lots)
        if qty > held:
            raise LedgerError(
                f"{d}: selling {qty} of security {security} in {demat}, but only {held} held"
            )
        today = [lot for lot in lots if lot.origin == "buy" and lot.acquired == d]
        rest = sorted(
            (lot for lot in lots if lot not in today), key=lambda x: (x.entered, x.lot_id)
        )
        order = (
            (today + rest) if same_day_first else sorted(lots, key=lambda x: (x.entered, x.lot_id))
        )
        taken: list[tuple[Lot, int, Decimal]] = []
        left = qty
        for lot in order:
            if left == 0:
                break
            q = min(lot.qty, left)
            snapshot = replace(lot)
            taken.append((snapshot, q, self._take(lot, q)))
            left -= q
        return taken

    @staticmethod
    def _allocate(total: Decimal, parts: list[int]) -> list[Decimal]:
        """Split `total` pro rata by `parts`, to the paisa, summing exactly to `total`."""
        n = sum(parts)
        out = [_q(total * p / n) for p in parts[:-1]]
        out.append(total - sum(out))
        return out

    def apply(self, e: Entry) -> None:
        b = self.book
        if isinstance(e, Opening):
            cost = _q(e.cost)
            self._new_lot(
                security=e.security,
                demat=e.demat,
                qty=e.qty,
                cost=cost,
                acquired=e.acquired,
                entered=e.entered,
                origin="opening",
            )
            b.cost_in += cost
        elif isinstance(e, Buy):
            cost = _q(e.qty * e.price + e.charges)  # amounts are booked to the paisa
            self._new_lot(
                security=e.security,
                demat=e.demat,
                qty=e.qty,
                cost=cost,
                acquired=e.trade_date,
                entered=e.credit_date,
                origin="buy",
            )
            b.cost_in += cost
            b.trades.append((e.trade_date, e.security, e.demat, "BUY", e.qty))
        elif isinstance(e, Sell):
            dp_ok = bool(self.tax.value("dp_charge_is_transfer_expense", e.trade_date))
            expenses_total = _q(e.charges + (e.dp_charge if dp_ok else Decimal(0)))
            taken = self._consume(e.security, e.demat, e.qty, e.trade_date, same_day_first=True)
            proceeds = self._allocate(_q(e.qty * e.price), [q for _, q, _ in taken])
            expenses = self._allocate(expenses_total, [q for _, q, _ in taken])
            for (lot, q, cost), p, x in zip(taken, proceeds, expenses, strict=True):
                kind = holding_kind(lot.acquired, e.trade_date, self.tax)
                b.slices.append(
                    Slice(
                        lot.lot_id,
                        e.security,
                        e.demat,
                        q,
                        lot.acquired,
                        e.trade_date,
                        p,
                        cost,
                        x,
                        kind,
                        "sell",
                    )
                )
            b.trades.append((e.trade_date, e.security, e.demat, "SELL", e.qty))
        elif isinstance(e, Buyback):
            regime = buyback_regime(e.announced, e.bought_back, self.tax)
            taken = self._consume(e.security, e.demat, e.qty, e.bought_back, same_day_first=False)
            proceeds = self._allocate(_q(e.qty * e.price), [q for _, q, _ in taken])
            expenses = self._allocate(_q(e.charges), [q for _, q, _ in taken])
            if regime == "deemed_dividend":
                b.slab_income.append((e.bought_back, "buyback deemed dividend", e.qty * e.price))
            for (lot, q, cost), p, x in zip(taken, proceeds, expenses, strict=True):
                if regime == "capital_gains":
                    kind: Kind = holding_kind(lot.acquired, e.bought_back, self.tax)
                elif regime == "deemed_dividend":
                    kind, p = holding_kind(lot.acquired, e.bought_back, self.tax), Decimal(0)
                elif regime == "exempt_s10_34a":
                    kind = "exempt"
                else:
                    raise LedgerError(f"unknown buyback regime {regime!r}")
                b.slices.append(
                    Slice(
                        lot.lot_id,
                        e.security,
                        e.demat,
                        q,
                        lot.acquired,
                        e.bought_back,
                        p,
                        cost,
                        x,
                        kind,
                        "buyback",
                    )
                )
        elif isinstance(e, Split):
            for lot in b.open_lots(e.security):
                lot.qty = _whole(lot.qty, e.ratio, f"split of lot {lot.lot_id}")
        elif isinstance(e, Bonus):
            for demat in sorted({lot.demat for lot in b.open_lots(e.security)}):
                held = b.quantity(e.security, demat)
                extra = _whole(held, e.ratio, f"bonus on {held} shares in {demat}")
                if extra:
                    self._new_lot(
                        security=e.security,
                        demat=demat,
                        qty=extra,
                        cost=Decimal(0),
                        acquired=e.allotted_on,
                        entered=e.credited_on,
                        origin="bonus",
                    )
        elif isinstance(e, Demerger):
            for lot in b.open_lots(e.parent):
                qty = _whole(lot.qty, e.child_per_parent, f"demerger of lot {lot.lot_id}")
                moved = _q(lot.cost * e.cost_to_child.numerator / e.cost_to_child.denominator)
                lot.cost -= moved
                self._new_lot(
                    security=e.child,
                    demat=lot.demat,
                    qty=qty,
                    cost=moved,
                    acquired=lot.acquired,
                    entered=max(lot.entered, e.credited_on),
                    origin="demerger",
                )
        elif isinstance(e, Merger):
            for lot in b.open_lots(e.old):
                qty = _whole(lot.qty, e.new_per_old, f"merger of lot {lot.lot_id}")
                cost = lot.cost
                lot.qty, lot.cost = 0, Decimal(0)
                self._new_lot(
                    security=e.new,
                    demat=lot.demat,
                    qty=qty,
                    cost=cost,
                    acquired=lot.acquired,
                    entered=max(lot.entered, e.credited_on),
                    origin="merger",
                )
        else:  # pragma: no cover
            raise TypeError(f"unknown entry {e!r}")


def build(entries: list[Entry], tax: TaxConfig) -> Book:
    """Replay entries (in date order; corporate actions before that day's trades)."""
    # The DP charge is billed once per stock, demat and sell day. Exactly one Sell entry may
    # carry it; two would silently double-count it in expenses and in cash flows.
    dp_days: set[tuple[int, str, date]] = set()
    for e in entries:
        if isinstance(e, Sell) and e.dp_charge:
            key = (e.security, e.demat, e.trade_date)
            if key in dp_days:
                raise LedgerError(f"DP charge recorded twice for {key}; put it on one Sell only")
            dp_days.add(key)
    engine = _Engine(tax)
    ordered = sorted(
        enumerate(entries), key=lambda ie: (_date_of(ie[1]), _RANK[type(ie[1])], ie[0])
    )
    for _, entry in ordered:
        if isinstance(entry, Buy | Sell | Opening | Buyback) and entry.qty <= 0:
            raise LedgerError(f"quantity must be positive: {entry!r}")
        engine.apply(entry)
    return engine.book
