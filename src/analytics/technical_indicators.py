from __future__ import annotations

from collections.abc import Sequence


def compute_weekly_ma30(adjusted_close: Sequence[float | int]) -> float:
    """Compute a 30-period rolling average from the adjusted-close series."""
    values = [float(value) for value in adjusted_close]
    if not values:
        raise ValueError("adjusted_close cannot be empty.")
    if len(values) < 30:
        return sum(values) / len(values)
    return sum(values[-30:]) / 30.0


def compute_weekly_rsi14(adjusted_close: Sequence[float | int], period: int = 14) -> float:
    """Compute the 14-period RSI using the adjusted-close series."""
    values = [float(value) for value in adjusted_close]
    if len(values) < 2:
        raise ValueError("adjusted_close must include at least two values to compute RSI.")

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    if len(gains) < period:
        gains_window = gains
        losses_window = losses
    else:
        gains_window = gains[-period:]
        losses_window = losses[-period:]

    avg_gain = sum(gains_window) / len(gains_window)
    avg_loss = sum(losses_window) / len(losses_window)

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_close_vs_weekly_ma30(adjusted_close: Sequence[float | int]) -> float:
    """Return the latest close relative to the trailing 30-period moving average."""
    if not adjusted_close:
        raise ValueError("adjusted_close cannot be empty.")

    latest_close = float(adjusted_close[-1])
    rolling_ma = compute_weekly_ma30(adjusted_close)
    if rolling_ma == 0:
        return 0.0
    return latest_close / rolling_ma
