"""Streamlit dashboard (WP 2.6). Start it with `pi ui`; it serves on localhost only.

Reads only data/pi_read.duckdb, the snapshot the evening run exports (ADR-0017). Nothing
here can write. No page predicts direction; the model stays hidden until the gate (§14).
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from pi.ui import queries as q

ROOT = Path(os.environ.get("PI_HOME", Path.cwd())).resolve()
SNAPSHOT = ROOT / "data" / "pi_read.duckdb"
SEVERITY_ICON = {"red": "🔴", "yellow": "🟡"}

st.set_page_config(page_title="Portfolio intelligence", layout="wide")
st.title("Portfolio intelligence")

con = q.open_snapshot(SNAPSHOT)
if con is None:
    st.info("No data yet. Run `pi run` (or install the 19:00 schedule) to create the snapshot.")
    st.stop()

try:
    status_tab, alerts_tab, data_tab, holdings_tab = st.tabs(
        ["Status", "Alerts", "Data issues", "Holdings & tax"]
    )

    with status_tab:
        s = q.status(con)
        c1, c2, c3 = st.columns(3)
        c1.metric("Last run", s.last_run_status or "-", help=s.last_run_at or None)
        c2.metric("Complete through", s.complete_through or "-")
        c3.metric("Clean streak", f"{s.streak} / {q.STREAK_TARGET}")
        st.progress(min(s.streak / q.STREAK_TARGET, 1.0))
        if s.corrections_pending:
            st.warning(
                "NSE re-issued files for days already processed: "
                + ", ".join(s.corrections_pending)
                + ". Acknowledge with `pi run --date <day>`."
            )
        st.caption(f"Model gate: {s.gate_state}. No predictions are shown before the gate.")
        st.subheader("Recent runs")
        st.dataframe(q.recent_runs(con), hide_index=True, width="stretch")

    with alerts_tab:
        live = q.alerts(con, mode="live")
        st.subheader("Live alerts")
        if not live:
            st.write("None yet.")
        for a in live:
            icon = SEVERITY_ICON.get(a["severity"], "")
            sent = "sent" if a["delivered"] else f"not sent ({a['error']})"
            with st.expander(f"{icon} {a['rule']} {a['subject'] or ''} — {a['fired_at']} — {sent}"):
                st.json(a["details"])
        st.subheader("Shadow mode (recorded, not sent)")
        st.caption("How often each new 🟡 rule fired during its first 20 trading days.")
        st.dataframe(
            [{"rule": k, "times": v} for k, v in q.shadow_counts(con).items()],
            hide_index=True,
        )

    with data_tab:
        issues = q.refdata_issues(con)
        st.subheader("New ISINs waiting for a link or confirmation")
        if not issues:
            st.write("None.")
        else:
            st.dataframe(issues, hide_index=True, width="stretch")

    with holdings_tab:
        st.info(
            "Holdings, risk and tax lots appear once your Groww statements are imported "
            "(work package 1.7). The engines behind them are already built."
        )
finally:
    con.close()
