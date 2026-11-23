"""The only backtest entry point (run_trial), plus the sealed holdout and the gate.

Keep this __init__ free of imports of the holdout loader, so importing pi.research does not
pull it in indirectly.
"""
