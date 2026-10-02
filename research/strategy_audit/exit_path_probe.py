"""Offline probe: real Position + real trailing overlay after a 50% partial.

Research-only; never imported by production. Run:
    PAPER_TRADE=true python -S research/strategy_audit/exit_path_probe.py
"""
import sys, sysconfig
sys.path.insert(0, "."); sys.path.append(sysconfig.get_paths()["purelib"])
from types import SimpleNamespace
from bot import engine as core
from bot import trailing_safety_hardening as ts
from bot.config import cfg
from bot.strategy import Signal

class P(core.Position):
    pass

ts.install(P, cfg, SimpleNamespace(warning=lambda *a, **k: None))
entry, risk = 100.0, 1.0                     # 1R = 1.0 price unit
sig = Signal("BTCUSDT", "LONG", entry, entry - 2 * risk / 2 * 2 / 2, entry + 2 * risk, 0.8)
pos = P(sig, 1.0)
print(f"cfg TRAILING_TRIGGER={cfg.TRAILING_TRIGGER} TRAILING_LOCK={cfg.TRAILING_LOCK} sl={pos.sl} tp={pos.tp}")
pos.update_pnl(entry + 1.0 * risk)           # price reaches +1R
print("at +1R before partial: peak_pnl", pos.peak_pnl, "trail", pos.calc_trailing_sl())
# durable_partial_exit: pos.qty = remaining (half); pos.sl = entry (BE)
pos.qty = 0.5; pos.sl = entry; pos.trailing_sl = entry
pos.update_pnl(entry + 1.0 * risk)
new_sl = pos.calc_trailing_sl()
print("after partial at +1R: peak_pnl", pos.peak_pnl, "qty", pos.qty, "trail_sl", new_sl,
      "current", pos.current_price, "INVALID(stop above price)" if new_sl and new_sl >= pos.current_price else "")
# 2R exit (confirmed_rr_exit): distance = |entry - pos.sl|
print("2R exit distance after BE:", abs(entry - pos.sl), "-> skipped when 0")
