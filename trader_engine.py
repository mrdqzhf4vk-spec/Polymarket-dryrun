"""Wallet scoring engine.

Score formula (0.0 – 1.0):
  score = 0.70 * decay_weighted_win_rate + 0.30 * normalised_avg_roi

Position-size influence uses log10 scaling so a $10k whale matters more than
a $100 retail trader, but not 100× more.

Recency decay: each trade is multiplied by  0.95 ^ days_since_trade
so recent wins outweigh stale ones.
"""

import logging
import math
import time
from typing import TYPE_CHECKING, Optional

import db

if TYPE_CHECKING:
    from polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)

_RECENCY_DECAY = 0.95   # per day
_SCORE_CACHE_TTL = 3600  # re-score after 1 hour


# ── Public API ────────────────────────────────────────────────────────────────

def get_cached_score(address: str) -> tuple[float, str]:
    """Return (score, label) from DB, or (0.5, 'NEW') if wallet is unknown."""
    row = db.get_wallet(address)
    if row:
        return float(row["score"]), str(row["label"] or "")
    return 0.5, "NEW"


async def score_wallet(address: str, client: "PolymarketClient") -> tuple[float, str]:
    """Return (score, label) for a wallet, fetching history if the cache is stale."""
    row = db.get_wallet(address)
    if row and row["last_updated"] > int(time.time()) - _SCORE_CACHE_TTL:
        label = str(row["label"] or "")
        # Don't re-score NEW wallets for 10 min (we already tried once recently)
        if label != "NEW" or row["last_updated"] > int(time.time()) - 600:
            return float(row["score"]), label

    try:
        history = await client.get_wallet_history(address)
    except Exception as exc:
        logger.warning("Could not fetch history for %s: %s", address[:10], exc)
        history = []

    return _compute_and_persist(address, history)


def update_scores_after_resolution(
    market_id: str,
    result: str,
    positions: list[dict],
) -> None:
    """After a market resolves, mark trades resolved and recompute scores."""
    db.resolve_market_trades(market_id, result)
    for pos in positions:
        address = pos.get("user", "")
        if not address:
            continue
        resolved_trades = db.get_wallet_resolved_trades(address)
        if not resolved_trades:
            continue
        history = [
            {
                "outcome": t["outcome"],
                "amount":  t["amount_usd"],
                "timestamp": t["trade_timestamp"],
            }
            for t in resolved_trades
        ]
        _compute_and_persist(address, history)


# ── Core scoring ──────────────────────────────────────────────────────────────

def _compute_and_persist(address: str, history: list[dict]) -> tuple[float, str]:
    score, label, stats = _score_from_history(history)
    db.upsert_wallet(
        address,
        score=round(score, 4),
        win_count=stats["wins"],
        loss_count=stats["losses"],
        total_roi=round(stats["avg_roi"], 4),
        trade_count=stats["total"],
        label=label,
        last_updated=int(time.time()),
    )
    return round(score, 4), label


def _score_from_history(history: list[dict]) -> tuple[float, str, dict]:
    """Compute score from a list of resolved trade dicts.

    Each dict may contain:
      outcome   – 'WIN' | 'LOSS'
      amount    – USD position size
      timestamp – unix seconds
    """
    resolved = [h for h in history if h.get("outcome") in ("WIN", "LOSS")]
    empty_stats = {"wins": 0, "losses": 0, "avg_roi": 0.0, "total": 0}

    if not resolved:
        return 0.5, "NEW", empty_stats

    if len(resolved) < 5:
        return 0.5, "LIMITED", {**empty_stats, "total": len(resolved)}

    now = time.time()
    max_amount = max(_safe_float(h.get("amount"), 1.0) for h in resolved)
    max_amount = max(max_amount, 1.0)

    win_w = 0.0
    loss_w = 0.0
    roi_list: list[float] = []

    for trade in resolved:
        ts = _safe_float(trade.get("timestamp"), now)
        days_ago = max(0.0, (now - ts) / 86400)
        decay = _RECENCY_DECAY ** days_ago

        amount = max(_safe_float(trade.get("amount"), 1.0), 0.01)
        log_w = math.log10(amount + 1) / math.log10(max_amount + 1)
        combined_w = decay * (0.5 + 0.5 * log_w)  # blend decay and size weight

        outcome = trade.get("outcome")
        if outcome == "WIN":
            win_w += combined_w
            # ROI: assume entry price not stored, use 0.5 as neutral baseline
            entry = _safe_float(trade.get("entry_price"), 0.5)
            roi_list.append((1.0 - entry) / max(entry, 0.01))
        else:
            loss_w += combined_w
            entry = _safe_float(trade.get("entry_price"), 0.5)
            roi_list.append(-1.0)

    total_w = win_w + loss_w
    win_rate = win_w / total_w if total_w > 0 else 0.5

    avg_roi = sum(roi_list) / len(roi_list) if roi_list else 0.0
    # Normalise ROI: avg_roi in [-1, +∞]; clip to [-1, 2] then map to [0, 1]
    roi_norm = max(0.0, min(1.0, (avg_roi + 1.0) / 3.0))

    score = 0.70 * win_rate + 0.30 * roi_norm
    score = max(0.0, min(1.0, score))

    wins   = sum(1 for t in resolved if t.get("outcome") == "WIN")
    losses = len(resolved) - wins

    return score, "", {"wins": wins, "losses": losses, "avg_roi": avg_roi, "total": len(resolved)}


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
