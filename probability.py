"""Smart probability calculator.

Algorithm:
  1. Start with the raw market probability (from the order book).
  2. For every open position compute a directional signal weighted by:
       combined_weight = wallet_score * log_scaled_position_size
  3. Take the weighted average across all positions to get raw_signal.
  4. Blend with the market price:
       adjusted_prob = market_prob * (1 - ALPHA) + raw_signal * ALPHA
     where ALPHA = 0.25 (signal can shift odds by at most ≈ ±15%).
  5. Require at least MIN_SCORED_TRADERS positions with known scores before
     blending; otherwise return market odds with label 'INSUFFICIENT DATA'.
"""

import math
from typing import NamedTuple

ALPHA = 0.25
MIN_SCORED_TRADERS = 5


class ProbResult(NamedTuple):
    smart_prob_up: float      # adjusted UP probability (0–1)
    confidence: str           # 'HIGH' | 'MEDIUM' | 'LOW' | 'INSUFFICIENT DATA'
    scored_count: int         # number of wallets that contributed
    signal_label: str         # human-readable signal description


def compute_smart_probability(
    market_prob_up: float,
    positions: list[dict],
    wallet_scores: dict[str, tuple[float, str]],
) -> ProbResult:
    """Return a ProbResult for the given market snapshot.

    Args:
        market_prob_up:  Raw UP probability from the order book (0–1).
        positions:       List of {user, outcome ('UP'|'DOWN'), size} dicts.
        wallet_scores:   Mapping of address → (score, label).
    """
    market_prob_up = max(0.01, min(0.99, market_prob_up))

    if not positions:
        return ProbResult(market_prob_up, "NO DATA", 0, "INSUFFICIENT DATA")

    max_size = max((abs(_f(p.get("size", 0))) for p in positions), default=1.0)
    max_size = max(max_size, 1.0)

    scored: list[dict] = []
    for pos in positions:
        address = pos.get("user", "")
        if not address:
            continue
        score, label = wallet_scores.get(address, (0.5, "NEW"))

        size = abs(_f(pos.get("size", 0)))
        # Log-scale position weight: big whale matters more, but not linearly
        log_w = math.log10(size + 1) / math.log10(max_size + 1) if max_size > 1 else 0.5

        combined_w = score * log_w
        direction  = 1.0 if pos.get("outcome", "").upper() == "UP" else 0.0

        scored.append({
            "address":   address,
            "score":     score,
            "label":     label,
            "direction": direction,
            "size":      size,
            "weight":    combined_w,
        })

    if len(scored) < MIN_SCORED_TRADERS:
        return ProbResult(
            market_prob_up,
            "INSUFFICIENT DATA",
            len(scored),
            "INSUFFICIENT DATA",
        )

    total_w = sum(p["weight"] for p in scored)
    if total_w == 0:
        raw_signal = 0.5
    else:
        raw_signal = sum(p["direction"] * p["weight"] for p in scored) / total_w

    smart_prob_up = market_prob_up * (1 - ALPHA) + raw_signal * ALPHA
    smart_prob_up = round(max(0.01, min(0.99, smart_prob_up)), 4)

    n = len(scored)
    if n >= 20:
        confidence = "HIGH"
    elif n >= 10:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    diff = smart_prob_up - market_prob_up
    if abs(diff) < 0.02:
        signal_label = "NEUTRAL"
    elif diff >= 0.10:
        signal_label = "STRONG BULL"
    elif diff >= 0.04:
        signal_label = "MODERATE BULL"
    elif diff > 0:
        signal_label = "SLIGHT BULL"
    elif diff <= -0.10:
        signal_label = "STRONG BEAR"
    elif diff <= -0.04:
        signal_label = "MODERATE BEAR"
    else:
        signal_label = "SLIGHT BEAR"

    return ProbResult(smart_prob_up, confidence, n, signal_label)


def _f(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
