"""Structural redundancy of score_tf components on SYNTHETIC prices (research only).

No market data is available in this environment. Synthetic regime-switching
paths measure *mechanical* coupling between score components (they are all
functions of the same OHLCV), not predictive power or edge.

    PAPER_TRADE=true python -S research/strategy_audit/score_redundancy_synthetic.py
"""
import sys
import sysconfig

sys.path.insert(0, ".")
sys.path.append(sysconfig.get_paths()["purelib"])

import numpy as np  # noqa: E402

from bot.indicators import atr  # noqa: E402
from bot.strategy import score_tf  # noqa: E402


def synthetic_path(rng, n=260):
    drift = rng.choice([-0.0015, -0.0006, 0.0, 0.0006, 0.0015])
    vol = rng.choice([0.002, 0.004, 0.008])
    rets = rng.normal(drift, vol, n)
    c = 100 * np.exp(np.cumsum(rets))
    o = np.r_[c[0], c[:-1]]
    spread = np.abs(rng.normal(0, vol, n)) * c
    h = np.maximum(o, c) + spread
    lo = np.minimum(o, c) - spread
    v = rng.lognormal(10, 0.5, n) * (1 + 3 * np.abs(rets) / vol)
    return o.tolist(), h.tolist(), lo.tolist(), c.tolist(), v.tolist()


def main(samples=1500, seed=7):
    rng = np.random.default_rng(seed)
    keys = ["trend_s", "vol_s", "momentum_s", "atr_s", "struct_s", "rsi_v", "adx_v", "total"]
    rows, flags = [], {"vwap_ok": [], "aligned": [], "adx_aligned": [], "ob_bias_NA": []}
    for _ in range(samples):
        o, h, lo, c, v = synthetic_path(rng)
        a = atr(h, lo, c)
        out = score_tf(c, h, lo, o, v, "LONG", float(a[-1]), float(np.mean(a[-20:])))
        if not out.get("ok"):
            continue
        rows.append([float(out[k]) for k in keys])
        flags["vwap_ok"].append(out["vwap_ok"])
        flags["aligned"].append(out["aligned"])
        flags["adx_aligned"].append(out["adx_aligned"])
        flags["ob_bias_NA"].append(out["ob_bias"] == "N/A")
    m = np.array(rows)
    corr = np.corrcoef(m.T)
    print(f"samples={len(m)} (synthetic; mechanical coupling only)\n")
    print("Pearson correlation of score_tf components (LONG):")
    print("            " + " ".join(f"{k[:9]:>9s}" for k in keys))
    for i, k in enumerate(keys):
        print(f"{k[:11]:11s} " + " ".join(f"{corr[i, j]:9.2f}" for j in range(len(keys))))
    print("\nShare of variance of total explained by each component (cov(x,total)/var(total)):")
    var_t = np.var(m[:, -1])
    for i, k in enumerate(keys[:5]):
        print(f"  {k:11s} {np.cov(m[:, i], m[:, -1])[0, 1] / var_t:6.2f}   range=[{m[:, i].min():.0f},{m[:, i].max():.0f}]")
    print("\nBoolean feature rates:")
    for k, vals in flags.items():
        print(f"  {k:12s} true={np.mean(vals):.2%}")
    agree = np.mean([a == b for a, b in zip(flags["aligned"], flags["adx_aligned"])])
    print(f"  EMA 'aligned' agrees with ADX direction {agree:.2%} of samples")


if __name__ == "__main__":
    main()
