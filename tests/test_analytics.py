"""WP 2.2: risk and trend analytics on made-up price series, hand-checked."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pi.analytics import portfolio as pf
from pi.analytics import series as s

approx = pytest.approx
RNG = np.random.default_rng(7)


# --------------------------------------------------------------------------- one series


def test_returns_and_bad_prices() -> None:
    assert s.returns([100, 110, 99]) == approx([0.10, -0.10])
    with pytest.raises(ValueError, match="positive"):
        s.returns([100, 0, 5])
    with pytest.raises(ValueError, match="NaN"):
        s.returns([100, float("nan")])


def test_realised_vol_hand_worked() -> None:
    r = [0.01, -0.01] * 10  # mean 0
    expected = 0.01 * math.sqrt(20 / 19) * math.sqrt(252)
    assert s.realised_vol(r, window=20) == approx(expected)
    assert s.realised_vol([0.003] * 21) == approx(0.0)


def test_ewma_hand_worked() -> None:
    v = s.ewma_vol_series([0.1, 0.0, 0.0])
    assert v[0] == approx(math.sqrt(0.01 * 252))
    assert v[1] == approx(math.sqrt(0.0094 * 252))  # 0.94 x 0.01
    assert v[2] == approx(math.sqrt(0.008836 * 252))


def test_ewma_reacts_to_todays_return() -> None:
    assert s.ewma_vol_series([0.0, 0.0, 0.1])[-1] > 0


def test_vol_spike_ratio_flags_a_shock() -> None:
    quiet = list(RNG.normal(0, 0.01, 300))
    shocked = quiet[:-5] + [0.08, -0.07, 0.09, -0.06, 0.07]
    assert s.vol_spike_ratio(quiet) < 2
    assert s.vol_spike_ratio(shocked) > 2


def test_beta_recovers_a_known_multiple() -> None:
    bench = RNG.normal(0, 0.01, 260)
    assert s.beta(2.0 * bench, bench) == approx(2.0)
    assert s.beta(0.5 * bench + 0.001, bench) == approx(0.5)


def test_drawdowns() -> None:
    assert s.drawdown([100, 120, 90]) == approx(-0.25)
    assert s.drawdown([100, 120, 130]) == 0.0
    assert s.max_drawdown([100, 120, 90, 130, 65]) == approx(-0.5)


def test_distance_from_52_week_high_ignores_older_peaks() -> None:
    p = [500.0] + [100.0] * 250 + [120.0, 108.0]  # the 500 is more than 252 days ago
    assert s.distance_from_high(p) == approx(108 / 120 - 1)


def test_momentum_skips_the_latest_month() -> None:
    p = [100.0] + [110.0] * 230 + [120.0] + [50.0] * 21  # 253 prices
    assert s.momentum_12_1(p) == approx(0.20)  # the crash in the last month is excluded


def test_trend_is_price_against_its_moving_average() -> None:
    assert s.trend([100.0] * 200) == 0.0
    assert s.trend([100.0] * 199 + [120.0]) == approx(120 / 100.1 - 1)


def test_short_history_is_refused_not_guessed() -> None:
    with pytest.raises(s.InsufficientHistory):
        s.momentum_12_1([100.0] * 100)


def test_values_after_d_cannot_change_a_value_for_d() -> None:
    p = 100 * np.cumprod(1 + RNG.normal(0, 0.01, 400))
    upto = p[:300]
    future_changed = np.concatenate([upto, p[300:] * 3])
    assert s.trend(upto) == s.trend(future_changed[:300])
    assert s.momentum_12_1(upto) == s.momentum_12_1(future_changed[:300])


# --------------------------------------------------------------------------- portfolio


def test_weights_and_concentration() -> None:
    w = pf.weights({1: 5000, 2: 5000, 3: 5000, 4: 5000})
    c = pf.concentration(w)
    assert c["top_weight"] == approx(0.25) and c["effective_n"] == approx(4.0)
    with pytest.raises(ValueError):
        pf.weights({1: 0.0})


def test_risk_shares_sum_to_one_and_split_evenly_for_twins() -> None:
    rets = RNG.normal(0, 0.01, (2000, 2))  # two independent, equally volatile stocks
    r = pf.risk_contributions([1, 2], {1: 0.5, 2: 0.5}, rets)
    assert sum(r.contribution.values()) == approx(1.0)
    assert r.contribution[1] == approx(0.5, abs=0.05)
    assert 0.0 <= r.shrinkage <= 1.0


def test_a_volatile_stock_dominates_risk_at_equal_weight() -> None:
    rets = np.column_stack([RNG.normal(0, 0.01, 1000), RNG.normal(0, 0.03, 1000)])
    r = pf.risk_contributions([1, 2], {1: 0.5, 2: 0.5}, rets)
    assert r.contribution[2] > 0.8  # 9x the variance -> about 90% of the risk


def test_single_stock_portfolio_vol_matches_its_own_vol() -> None:
    x = RNG.normal(0, 0.02, (500, 1))
    r = pf.risk_contributions([1], {1: 1.0}, x)
    assert r.vol == approx(float(np.std(x, ddof=0)) * math.sqrt(252), rel=1e-6)
    assert r.contribution == {1: approx(1.0)}


def test_covariance_needs_aligned_history() -> None:
    with pytest.raises(s.InsufficientHistory):
        pf.shrunk_covariance(np.zeros((10, 2)))
    bad = RNG.normal(0, 0.01, (60, 2))
    bad[5, 1] = np.nan
    with pytest.raises(ValueError, match="align"):
        pf.shrunk_covariance(bad)


def test_active_sector_weights() -> None:
    w = {1: 0.6, 2: 0.4}
    act = pf.active_sector_weights(w, {1: "IT", 2: "Banks"}, {"IT": 0.1, "Banks": 0.3, "FMCG": 0.1})
    assert act == {"Banks": approx(0.1), "FMCG": approx(-0.1), "IT": approx(0.5)}


def test_scenario_replay_with_a_beta_proxy() -> None:
    res = pf.replay(
        {1: 0.5, 2: 0.5}, {1: -0.40, 2: None}, index_return=-0.30, betas={1: 1.1, 2: 1.5}
    )
    assert res.by_stock == {1: approx(-0.20), 2: approx(-0.225)}
    assert res.portfolio_return == approx(-0.425)
    assert res.proxied == [2]
