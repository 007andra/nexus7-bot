# NEXUS Kronos Shadow Worker

Separate inference service for Kronos. It is deliberately isolated from the LIVE NEXUS execution process so PyTorch/model loading cannot block order management.

## Local/deploy contract

Build with repository root as Docker build context and this Dockerfile:

`services/kronos_worker/Dockerfile`

Environment:

```
KRONOS_MODEL=NeoQuasar/Kronos-small
KRONOS_TOKENIZER=NeoQuasar/Kronos-Tokenizer-base
KRONOS_DEVICE=cpu
KRONOS_MAX_CONTEXT=400
```

Expose the service internally and set the main bot's `KRONOS_SHADOW_URL` to this worker.

The first request can be slower because pretrained weights are loaded lazily. Main NEXUS does not wait for this worker to authorize a trade; requests are shadow telemetry only.
