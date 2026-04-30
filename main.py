"""Entry point – orchestrates the async polling loop and live terminal display.

Usage:
    python main.py

All debug output goes to bot.log (never to stdout) so it doesn't collide
with the Rich live display.
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from rich.live import Live

import db
from dashboard import (
    make_full_display,
    make_resolved_display,
    make_waiting_display,
)
from polymarket_client import PolymarketClient
from probability import compute_smart_probability
from trader_engine import score_wallet, update_scores_after_resolution

load_dotenv()

# ── Logging – file only, never stdout ─────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 8     # seconds between data refreshes
SEARCH_RETRY   = 15    # seconds to wait when no market is found
FLASH_DURATION = 5     # seconds to show the resolution banner

# A market is considered resolved when the YES price snaps to one of these extremes
RESOLVED_HIGH = 0.98
RESOLVED_LOW  = 0.02


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run() -> None:
    db.init_db()
    logger.info("DB initialised")

    session_start = int(time.time())
    # Track the market we last processed so we know when it changed
    current_market_id: Optional[str] = None
    # Keep the last prices/smart so we can log accuracy on resolution
    last_market_prob = 0.5
    last_smart_prob  = 0.5

    async with PolymarketClient() as client:
        with Live(
            make_waiting_display(),
            refresh_per_second=2,
            screen=False,
            transient=False,
        ) as live:

            while True:
                # ── 1. Find active market ──────────────────────────────────
                market = await client.find_active_btc_5min_market()
                if not market:
                    live.update(make_waiting_display(
                        "No active BTC 5-min market found – retrying…"
                    ))
                    await asyncio.sleep(SEARCH_RETRY)
                    continue

                market_id = market.get("conditionId", "")
                question  = market.get("question", "BTC 5-min Market")

                if market_id != current_market_id:
                    current_market_id = market_id
                    logger.info("Tracking market %s – %s", market_id[:16], question)

                # ── 2. Fetch prices ────────────────────────────────────────
                prices = await client.get_market_prices(market)
                market_prob_up = prices["yes_price"]
                last_market_prob = market_prob_up

                # ── 3. Fetch all open positions ────────────────────────────
                positions = await client.get_positions_for_market(market_id)
                logger.debug("Fetched %d positions for %s", len(positions), market_id[:16])

                # ── 4. Score every wallet (uses cache; async API fallback) ──
                wallet_scores: dict[str, tuple[float, str]] = {}
                trader_rows:   list[dict]                   = []

                score_tasks = {
                    pos["user"]: asyncio.create_task(score_wallet(pos["user"], client))
                    for pos in positions
                    if pos.get("user")
                }
                if score_tasks:
                    done = await asyncio.gather(*score_tasks.values(), return_exceptions=True)
                    for addr, result in zip(score_tasks.keys(), done):
                        if isinstance(result, Exception):
                            logger.warning("Score task failed for %s: %s", addr[:10], result)
                            wallet_scores[addr] = (0.5, "NEW")
                        else:
                            wallet_scores[addr] = result

                for pos in positions:
                    addr = pos.get("user", "")
                    if not addr:
                        continue
                    sc, label = wallet_scores.get(addr, (0.5, "NEW"))
                    row = db.get_wallet(addr)
                    trader_rows.append({
                        "address":   addr,
                        "side":      pos.get("outcome", "?"),
                        "amount":    pos.get("size", 0.0),
                        "score":     sc,
                        "label":     label,
                        "win_count":  int(row["win_count"])  if row else 0,
                        "loss_count": int(row["loss_count"]) if row else 0,
                    })

                # Sort by position size so the biggest traders appear first
                trader_rows.sort(key=lambda x: x["amount"], reverse=True)

                # ── 5. Compute smart probability ───────────────────────────
                prob = compute_smart_probability(market_prob_up, positions, wallet_scores)
                last_smart_prob = prob.smart_prob_up

                # ── 6. Persist current positions to DB ────────────────────
                ts_now = int(time.time())
                for pos in positions:
                    addr = pos.get("user", "")
                    if addr:
                        db.upsert_trade(
                            addr, market_id,
                            pos.get("outcome", "DOWN"),
                            float(pos.get("size", 0) or 0),
                            ts_now,
                        )

                # ── 7. Calculate remaining time ────────────────────────────
                end_ts     = PolymarketClient.get_market_end_time(market)
                closes_in  = max(0, int(end_ts - time.time())) if end_ts else None

                # ── 8. Session accuracy ────────────────────────────────────
                recent      = db.get_recent_markets(5)
                s_correct, s_total = db.get_session_accuracy(session_start)

                # ── 9. Refresh display ─────────────────────────────────────
                live.update(make_full_display(
                    question       = question,
                    closes_in_secs = closes_in,
                    market_prob_up = market_prob_up,
                    smart_prob_up  = prob.smart_prob_up,
                    confidence     = prob.confidence,
                    scored_count   = prob.scored_count,
                    signal_label   = prob.signal_label,
                    is_stale       = client.is_stale(),
                    traders        = trader_rows,
                    recent_markets = recent,
                    session_correct= s_correct,
                    session_total  = s_total,
                ))

                # ── 10. Resolution detection ───────────────────────────────
                resolved, result = await _detect_resolution(
                    client, market, market_id, market_prob_up
                )

                if resolved:
                    smart_right = (
                        (last_smart_prob > 0.5 and result == "UP") or
                        (last_smart_prob <= 0.5 and result == "DOWN")
                    )
                    logger.info(
                        "Market %s resolved: %s  smart_right=%s",
                        market_id[:16], result, smart_right,
                    )

                    # Flash resolution banner
                    live.update(make_resolved_display(result, smart_right))
                    await asyncio.sleep(FLASH_DURATION)

                    # Update scores and save history
                    update_scores_after_resolution(market_id, result, positions)
                    db.save_market_resolution(
                        market_id, question, result,
                        last_smart_prob, last_market_prob,
                    )

                    current_market_id = None
                    live.update(make_waiting_display(
                        "Market resolved. Searching for next market…"
                    ))
                    await asyncio.sleep(3)
                    continue

                await asyncio.sleep(POLL_INTERVAL)


async def _detect_resolution(
    client: PolymarketClient,
    market: dict,
    market_id: str,
    market_prob_up: float,
) -> tuple[bool, str]:
    """Return (is_resolved, result) where result is 'UP' or 'DOWN'."""

    # Price snap – the fastest signal
    if market_prob_up >= RESOLVED_HIGH:
        return True, "UP"
    if market_prob_up <= RESOLVED_LOW:
        return True, "DOWN"

    # Metadata check (slower – only run every other tick to reduce API calls)
    detail = await client.get_market_detail(market_id)
    if not detail:
        return False, ""

    if detail.get("closed") or detail.get("resolved"):
        winning = str(
            detail.get("winningOutcome") or detail.get("winner") or ""
        ).upper()
        if winning in ("YES", "UP", "1"):
            return True, "UP"
        if winning in ("NO", "DOWN", "0"):
            return True, "DOWN"
        # Ambiguous – fall back to price
        return True, "UP" if market_prob_up >= 0.5 else "DOWN"

    return False, ""


# ── Session summary ────────────────────────────────────────────────────────────

def _print_session_summary(session_start: int) -> None:
    correct, total = db.get_session_accuracy(session_start)
    elapsed = int(time.time()) - session_start
    h, rem  = divmod(elapsed, 3600)
    m, s    = divmod(rem, 60)
    pct     = round(correct / total * 100) if total else 0

    print("\n── Session summary ──────────────────────────────")
    print(f"  Duration  : {h:02d}h {m:02d}m {s:02d}s")
    print(f"  Markets   : {total}")
    print(f"  Accuracy  : {correct}/{total}  ({pct}%)")
    print("────────────────────────────────────────────────\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    session_start = int(time.time())

    # Graceful Ctrl-C handling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig_name: str) -> None:
        logger.info("Received %s – shutting down", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig.name)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        loop.run_until_complete(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.exception("Unhandled exception: %s", exc)
        sys.exit(1)
    finally:
        _print_session_summary(session_start)
        loop.close()


if __name__ == "__main__":
    main()
