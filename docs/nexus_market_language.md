# NEXUS native market-language forecasting

This subsystem is owned by NEXUS. It has no runtime integration with Kronos, no model downloads and no external inference dependency.

Ideas adopted and reimplemented natively:
1. hierarchical coarse/fine market representation;
2. explicit temporal context;
3. autoregressive multi-step state generation;
4. probabilistic multi-path/top-p sampling;
5. forecast distributions instead of single-point predictions;
6. strictly causal walk-forward evaluation.

The implementation intentionally starts as research evidence. Promotion into NEXUS scoring requires a separate change and measured out-of-sample improvement after costs. Existing execution and risk controls remain authoritative.
