"""Adaptive machine-learning strategy (idea from freqtrade's FreqAI, our own small implementation).

A logistic-regression model predicts whether price will rise by more than `threshold_pct` within
`horizon` bars, and is retrained every `retrain_bars` bars on a rolling window of recent examples.

No look-ahead, by construction:
- features use only the closed bar and earlier bars (returns, volatility, position in the day's range,
  distance to PDH / PDL, order-flow delta, time of day);
- a bar becomes a training example only `horizon` bars later, when its outcome (the highest high of
  the bars after it) has actually happened;
- the model is trained on those finished examples only, then used on the newest bar.
Everything (examples, weights, scaling) lives in the strategy state, so paper trading resumes exactly.
"""

from __future__ import annotations

import math

import numpy as np

from .base import Context, Param, Signal, Strategy, register


def _features(ctx: Context) -> list[float] | None:
    bars = ctx.bars
    if len(bars) < 18:
        return None
    c = [b.close for b in bars]
    b = ctx.bar
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 16, len(c))]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    day = ctx.day
    span = day["high"] - day["low"]
    hour = (b.time % 86400) / 3600
    total = (b.buy_vol + b.sell_vol) if b.buy_vol is not None else 0
    flow = (b.buy_vol - b.sell_vol) / total if total else 0.0
    return [
        c[-1] / c[-2] - 1, c[-1] / c[-5] - 1, c[-1] / c[-17] - 1, vol,
        (b.high - b.low) / b.close,
        (b.close - day["low"]) / span if span else 0.5,
        b.close / ctx.pdh - 1 if ctx.pdh else 0.0,
        b.close / ctx.pdl - 1 if ctx.pdl else 0.0,
        flow, math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
    ]


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 300, lr: float = 0.5):
    """L2-regularised logistic regression on standardised features (plain gradient descent)."""
    mu, sd = x.mean(axis=0), x.std(axis=0)
    sd[sd == 0] = 1.0
    z = (x - mu) / sd
    w, b0 = np.zeros(z.shape[1]), 0.0
    n = len(y)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(z @ w + b0)))
        g = p - y
        w -= lr * (z.T @ g / n + l2 * w / n)
        b0 -= lr * g.mean()
    return w, b0, mu, sd


def predict(model: dict, f: list[float]) -> float:
    x = (np.asarray(f) - np.asarray(model["mu"])) / np.asarray(model["sd"])
    s = float(x @ np.asarray(model["w"])) + model["b"]
    return 1 / (1 + math.exp(-max(-60.0, min(60.0, s))))


@register
class MlLogit(Strategy):
    key = "ml_logit"
    name = "Adaptive ML: logistic regression (long)"
    description = (
        "Predicts the chance of a rise above a threshold within N bars from recent price action, PDH/PDL distance, "
        "order flow and time of day; retrains on a rolling window of finished examples only (no look-ahead). "
        "Buys when the predicted chance is high, exits when it falls or at the stop / target."
    )
    lookback = 50
    params = {
        "horizon": Param("int", 4, "Bars ahead the model predicts", min=1, max=200),
        "threshold_pct": Param("float", 0.3, "A 'rise' means a high at least this % above the close", min=0, max=50),
        "prob_entry": Param("float", 0.6, "Buy when the predicted chance is at least this", min=0.5, max=0.99),
        "prob_exit": Param("float", 0.4, "Sell an open position when it falls below this (0 = off)", min=0, max=0.99),
        "train_size": Param("int", 600, "Rolling window: most recent finished examples to train on", min=50, max=5000),
        "min_train": Param("int", 200, "No trading before this many finished examples", min=30, max=5000),
        "retrain_bars": Param("int", 24, "Retrain every N bars", min=1, max=10_000),
        "l2": Param("float", 1.0, "Regularisation strength (higher = simpler model)", min=0, max=1000),
        "stop_pct": Param("float", 1.0, "Stop distance below the entry bar's close, %", min=0.05, max=50),
        "target_r": Param("float", 2.0, "Target as a multiple of the stop distance (0 = none)", min=0, max=50),
        "train_days": Param("int", 14, "Warm-up days replayed to collect examples before trading", min=1, max=365),
    }

    def warmup_days(self) -> int:
        return self.p["train_days"]

    def on_bar(self, ctx: Context) -> Signal | None:
        p, st = self.p, ctx.state
        h = p["horizon"]
        queue = st.setdefault("queue", [])  # [bar index, close, features] waiting for their outcome
        xs, ys = st.setdefault("x", []), st.setdefault("y", [])
        highs = st.setdefault("highs", [])  # highs of the most recent bars, current bar last
        highs.append(ctx.bar.high)
        del highs[: -h]
        # an example made at bar i is labelled at bar i + h from the highs of bars i+1 .. i+h (all closed now)
        while queue and ctx.index - queue[0][0] >= h:
            _i, c0, f0 = queue.pop(0)
            peak = max(highs[-h:])
            xs.append(f0)
            ys.append(1.0 if peak / c0 - 1 >= p["threshold_pct"] / 100 else 0.0)
        del xs[: -p["train_size"]]
        del ys[: -p["train_size"]]
        f = _features(ctx)
        if f is not None:
            queue.append([ctx.index, ctx.bar.close, f])
        # retrain on finished examples only
        if len(ys) >= p["min_train"] and 0 < sum(ys) < len(ys) and (
                "model" not in st or ctx.index - st.get("trained_at", -10**9) >= p["retrain_bars"]):
            w, b0, mu, sd = fit_logistic(np.asarray(xs), np.asarray(ys), p["l2"])
            st["model"] = {"w": w.tolist(), "b": float(b0), "mu": mu.tolist(), "sd": sd.tolist()}
            st["trained_at"] = ctx.index
        model = st.get("model")
        if model is None or f is None:
            return None
        prob = predict(model, f)
        st["last_prob"] = prob
        if ctx.position:
            if p["prob_exit"] and prob < p["prob_exit"]:
                return Signal("sell", f"model chance fell to {prob:.0%}")
            return None
        if prob >= p["prob_entry"]:
            close = ctx.bar.close
            stop = close * (1 - p["stop_pct"] / 100)
            target = close + (close - stop) * p["target_r"] if p["target_r"] else None
            return Signal("buy", f"model chance {prob:.0%} of +{p['threshold_pct']}% within {h} bars",
                          stop=stop, target=target, tag="ml_logit")
        return None
