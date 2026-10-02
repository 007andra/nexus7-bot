from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / "kronos_upstream"
sys.path.insert(0, str(UPSTREAM))

from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402


MODEL_NAME = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
TOKENIZER_NAME = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
DEVICE = os.environ.get("KRONOS_DEVICE") or None
MAX_CONTEXT = min(512, max(32, int(os.environ.get("KRONOS_MAX_CONTEXT", "400"))))

app = FastAPI(title="NEXUS Kronos Shadow Worker", version="1.0.0")

_predictor = None
_model_lock = asyncio.Lock()


class ForecastRequest(BaseModel):
    symbol: str
    timeframe_minutes: int = Field(default=15, ge=1, le=1440)
    horizon: int = Field(default=16, ge=1, le=120)
    sample_count: int = Field(default=16, ge=1, le=64)
    temperature: float = Field(default=1.0, gt=0.0, le=3.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    candles: List[Dict[str, Any]]


def _load_predictor():
    global _predictor
    if _predictor is None:
        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_NAME)
        model = Kronos.from_pretrained(MODEL_NAME)
        _predictor = KronosPredictor(
            model,
            tokenizer,
            device=DEVICE,
            max_context=MAX_CONTEXT,
        )
    return _predictor


def _frame(req: ForecastRequest):
    if len(req.candles) < 32:
        raise ValueError("at least 32 candles are required")
    rows = req.candles[-MAX_CONTEXT:]
    required = ("ts", "open", "high", "low", "close")
    for row in rows:
        if not all(key in row for key in required):
            raise ValueError("candle missing required field")

    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="raise")

    ts = pd.to_numeric(df["ts"], errors="raise")
    unit = "ms" if float(ts.abs().max()) > 1e11 else "s"
    x_ts = pd.Series(pd.to_datetime(ts, unit=unit, utc=True).dt.tz_convert(None))

    data = df[["open", "high", "low", "close", "volume"]].copy()
    data["amount"] = data["volume"] * data[["open", "high", "low", "close"]].mean(axis=1)

    start = x_ts.iloc[-1] + pd.Timedelta(minutes=req.timeframe_minutes)
    y_ts = pd.Series(pd.date_range(
        start=start,
        periods=req.horizon,
        freq=f"{req.timeframe_minutes}min",
    ))
    return data, x_ts, y_ts


def _forecast_sync(req: ForecastRequest):
    predictor = _load_predictor()
    data, x_ts, y_ts = _frame(req)
    paths = []
    for _ in range(req.sample_count):
        pred = predictor.predict(
            df=data,
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=req.horizon,
            T=req.temperature,
            top_k=0,
            top_p=req.top_p,
            sample_count=1,
            verbose=False,
        )
        keep = pred[["open", "high", "low", "close", "volume", "amount"]]
        paths.append([
            {key: float(value) for key, value in row.items()}
            for row in keep.to_dict(orient="records")
        ])

    return {
        "symbol": req.symbol,
        "model": MODEL_NAME,
        "tokenizer": TOKENIZER_NAME,
        "context": len(data),
        "horizon": req.horizon,
        "paths": paths,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "tokenizer": TOKENIZER_NAME,
        "loaded": _predictor is not None,
        "max_context": MAX_CONTEXT,
    }


@app.post("/forecast")
async def forecast(req: ForecastRequest):
    try:
        async with _model_lock:
            return await asyncio.to_thread(_forecast_sync, req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=type(exc).__name__) from exc
