"""Async Polymarket API client with exponential-backoff retry and graceful fallbacks.

Primary data sources (in order of preference):
  1. Polymarket CLOB API  – clob.polymarket.com
  2. Gamma metadata API   – gamma-api.polymarket.com
  3. Goldsky subgraph     – api.goldsky.com  (GraphQL)
  4. Data REST API        – data-api.polymarket.com
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
SUBGRAPH_URL = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw"
    "/subgraphs/polymarket-orderbook-v2/prod/gn"
)

_MAX_RETRIES = 4
_BASE_BACKOFF = 1.0  # seconds


class PolymarketClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._stale = False

    async def __aenter__(self) -> "PolymarketClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    def is_stale(self) -> bool:
        return self._stale

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    async def _get(
        self,
        url: str,
        params: Optional[dict] = None,
    ) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    wait = _BASE_BACKOFF * (2 ** attempt)
                    logger.warning("Rate-limited %s; waiting %.1fs", url, wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                self._stale = False
                return resp.json()
            except httpx.TimeoutException:
                logger.warning("Timeout %s (attempt %d)", url, attempt + 1)
            except httpx.HTTPStatusError as exc:
                logger.warning("HTTP %d for %s (attempt %d)", exc.response.status_code, url, attempt + 1)
            except Exception as exc:
                logger.warning("Error %s: %s (attempt %d)", url, exc, attempt + 1)
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
        self._stale = True
        logger.error("All retries exhausted for %s – marking STALE", url)
        return None

    async def _post_graphql(self, query: str, variables: dict) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(
                    SUBGRAPH_URL,
                    json={"query": query, "variables": variables},
                )
                if resp.status_code == 429:
                    wait = _BASE_BACKOFF * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    logger.warning("GraphQL errors: %s", data["errors"])
                    return None
                return data.get("data")
            except Exception as exc:
                logger.warning("GraphQL error (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(_BASE_BACKOFF * (2 ** attempt))
        return None

    # ── Market discovery ──────────────────────────────────────────────────────

    async def find_active_btc_5min_market(self) -> Optional[dict]:
        """Return the soonest-closing active BTC UP/DOWN 5-min market, or None."""
        candidates: list[dict] = []

        # Pass 1 – tag-scoped searches (fast)
        for tag in ("bitcoin", "crypto"):
            data = await self._get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 200, "tag_slug": tag},
            )
            markets = _extract_list(data)
            candidates.extend(m for m in markets if self._is_btc_5min(m))
            if candidates:
                break

        # Pass 2 – broad sweep if nothing found
        if not candidates:
            data = await self._get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 300},
            )
            markets = _extract_list(data)
            candidates = [m for m in markets if self._is_btc_5min(m)]

        if not candidates:
            return None

        # Pick the one that closes soonest (smallest positive seconds_to_close)
        now = time.time()
        def _sort_key(m: dict) -> float:
            end = _parse_end_time(m)
            if end is None:
                return float("inf")
            remaining = end - now
            return remaining if remaining > 0 else float("inf")

        candidates.sort(key=_sort_key)
        return candidates[0]

    @staticmethod
    def _is_btc_5min(market: dict) -> bool:
        if market.get("closed") or not market.get("active", True):
            return False
        text = " ".join([
            market.get("question", ""),
            market.get("slug", ""),
            market.get("description", ""),
        ]).lower()
        has_btc = "btc" in text or "bitcoin" in text
        has_5min = any(kw in text for kw in ("5 min", "5-min", "5min", "5 minute"))
        has_dir = any(kw in text for kw in ("up", "down", "higher", "lower", "rise", "fall"))
        return has_btc and has_5min and has_dir

    async def get_market_detail(self, condition_id: str) -> Optional[dict]:
        return await self._get(f"{GAMMA_API}/markets/{condition_id}")

    # ── Prices ────────────────────────────────────────────────────────────────

    async def get_market_prices(self, market: dict) -> dict:
        """Return {'yes_price', 'no_price', 'market_id'} for a market."""
        yes_price = await self._resolve_yes_price(market)
        yes_price = max(0.01, min(0.99, yes_price))
        return {
            "yes_price": yes_price,
            "no_price": round(1.0 - yes_price, 4),
            "market_id": market.get("conditionId", ""),
        }

    async def _resolve_yes_price(self, market: dict) -> float:
        # Gamma outcomePrices (index 0 = YES/UP)
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    pass
            if isinstance(outcome_prices, list) and outcome_prices:
                try:
                    return float(outcome_prices[0])
                except (ValueError, TypeError):
                    pass

        # Gamma bestBid / bestAsk mid
        try:
            bid = float(market.get("bestBid") or 0)
            ask = float(market.get("bestAsk") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            if bid > 0:
                return bid
            if ask > 0:
                return ask
        except (ValueError, TypeError):
            pass

        # CLOB order-book mid for YES token
        token_ids = _extract_token_ids(market)
        if token_ids:
            mid = await self._get_book_mid(token_ids[0])
            if mid is not None:
                return mid

        return 0.5

    async def _get_book_mid(self, token_id: str) -> Optional[float]:
        data = await self._get(f"{CLOB_API}/book", params={"token_id": token_id})
        if not data:
            return None
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        try:
            if bids and asks:
                return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            if bids:
                return float(bids[0]["price"])
            if asks:
                return float(asks[0]["price"])
        except (KeyError, ValueError, TypeError):
            pass
        return None

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions_for_market(self, condition_id: str) -> list[dict]:
        """Return list of {user, outcome, size} dicts for all open positions."""

        # Try data-api first
        data = await self._get(
            f"{DATA_API}/positions",
            params={"market": condition_id, "limit": 500},
        )
        positions = _extract_list(data)
        if positions:
            return _normalise_positions(positions)

        # Try subgraph
        positions = await self._subgraph_positions(condition_id)
        if positions:
            return _normalise_positions(positions)

        # Fallback: infer from CLOB trades
        trades = await self._get(
            f"{CLOB_API}/trades",
            params={"market": condition_id, "limit": 200},
        )
        return _positions_from_trades(_extract_list(trades))

    async def _subgraph_positions(self, condition_id: str) -> list[dict]:
        query = """
        query($conditionId: String!, $skip: Int!) {
          positions(
            where: { conditionId: $conditionId }
            first: 100
            skip: $skip
            orderBy: size
            orderDirection: desc
          ) {
            id
            conditionId
            outcomeIndex
            size
            user { id }
          }
        }
        """
        all_pos: list[dict] = []
        skip = 0
        while True:
            data = await self._post_graphql(query, {"conditionId": condition_id, "skip": skip})
            if not data:
                break
            batch = data.get("positions", [])
            if not batch:
                break
            all_pos.extend(batch)
            if len(batch) < 100:
                break
            skip += 100
        return all_pos

    # ── Wallet history ────────────────────────────────────────────────────────

    async def get_wallet_history(self, address: str) -> list[dict]:
        """Fetch resolved BTC 5-min trade history for a wallet.

        Tries data-api first; falls back to subgraph when <5 results returned.
        Detects shallow history (wallet has >20 trades but API shows <5) and
        automatically switches to subgraph.
        """
        history: list[dict] = []

        # data-api activity endpoint
        data = await self._get(
            f"{DATA_API}/activity",
            params={"user": address, "limit": 200},
        )
        if data:
            raw = data if isinstance(data, list) else data.get("history", data.get("data", []))
            history = [h for h in _extract_list(raw) if _is_btc_5min_trade(h)]

        # Subgraph fallback when data-api returns suspiciously few results
        if len(history) < 5:
            sub = await self._subgraph_wallet_history(address)
            sub_btc = [h for h in sub if _is_btc_5min_trade(h)]
            if len(sub_btc) > len(history):
                logger.info(
                    "Subgraph returned richer history for %s (%d vs %d)",
                    address[:10],
                    len(sub_btc),
                    len(history),
                )
                history = sub_btc

        return history

    async def _subgraph_wallet_history(self, address: str) -> list[dict]:
        query = """
        query($user: String!, $skip: Int!) {
          positions(
            where: { user: $user }
            first: 100
            skip: $skip
            orderBy: timestamp
            orderDirection: desc
          ) {
            id
            conditionId
            outcomeIndex
            size
            user { id }
            condition {
              id
              question
            }
          }
        }
        """
        all_pos: list[dict] = []
        skip = 0
        while len(all_pos) < 500:
            data = await self._post_graphql(
                query, {"user": address.lower(), "skip": skip}
            )
            if not data:
                break
            batch = data.get("positions", [])
            if not batch:
                break
            all_pos.extend(batch)
            if len(batch) < 100:
                break
            skip += 100
        return all_pos

    # ── Recent trades ─────────────────────────────────────────────────────────

    async def get_recent_trades(self, condition_id: str, limit: int = 50) -> list[dict]:
        data = await self._get(
            f"{CLOB_API}/trades",
            params={"market": condition_id, "limit": limit},
        )
        return _extract_list(data)

    # ── End-time helper ───────────────────────────────────────────────────────

    @staticmethod
    def get_market_end_time(market: dict) -> Optional[int]:
        return _parse_end_time(market)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "markets", "positions", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _extract_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds") or market.get("tokenIds") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [str(t) for t in raw if t] if isinstance(raw, list) else []


def _parse_end_time(market: dict) -> Optional[int]:
    for key in ("endDate", "end_date_iso", "endDateIso", "endTimestamp", "end_time"):
        val = market.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except ValueError:
                try:
                    return int(float(val))
                except ValueError:
                    pass
    return None


def _normalise_positions(raw: list[dict]) -> list[dict]:
    """Normalise position dicts from various sources to {user, outcome, size}."""
    out: list[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        user = (
            p.get("user")
            or p.get("proxyWallet")
            or p.get("maker")
            or (p.get("user", {}) or {}).get("id", "")
        )
        if isinstance(user, dict):
            user = user.get("id", "")
        if not user or not str(user).startswith("0x"):
            continue

        # Outcome: prefer string label; fall back to outcomeIndex
        outcome_raw = p.get("outcome") or p.get("side") or ""
        if not outcome_raw:
            idx = p.get("outcomeIndex")
            outcome_raw = "YES" if idx in (0, "0") else "NO"
        outcome = str(outcome_raw).upper()
        if outcome in ("YES", "UP", "0", "LONG", "BUY"):
            outcome = "UP"
        else:
            outcome = "DOWN"

        size_raw = p.get("size") or p.get("amount") or p.get("netQuantity") or 0
        try:
            size = float(size_raw)
        except (ValueError, TypeError):
            size = 0.0

        if size <= 0:
            continue

        out.append({"user": str(user), "outcome": outcome, "size": size})
    return out


def _positions_from_trades(trades: list[dict]) -> list[dict]:
    """Derive approximate positions from a list of CLOB trade events."""
    wallets: dict[str, dict] = {}
    for t in trades:
        for addr_key in ("maker", "maker_address", "taker", "taker_address"):
            addr = t.get(addr_key, "")
            if not addr or not str(addr).startswith("0x"):
                continue
            outcome = str(t.get("outcome", t.get("side", "YES"))).upper()
            outcome = "UP" if outcome in ("YES", "UP", "0", "BUY") else "DOWN"
            try:
                size = float(t.get("size", 0) or 0) * float(t.get("price", 0.5) or 0.5)
            except (ValueError, TypeError):
                size = 0.0
            if addr not in wallets:
                wallets[addr] = {"user": addr, "outcome": outcome, "size": size}
            else:
                wallets[addr]["size"] += size
    return [v for v in wallets.values() if v["size"] > 0]


def _is_btc_5min_trade(trade: Any) -> bool:
    if not isinstance(trade, dict):
        return False
    text = " ".join([
        str(trade.get("title", "")),
        str(trade.get("question", "")),
        str(trade.get("slug", "")),
        str((trade.get("market") or {}).get("question", "")),
        str((trade.get("condition") or {}).get("question", "")),
    ]).lower()
    has_btc = "btc" in text or "bitcoin" in text
    has_5min = any(kw in text for kw in ("5 min", "5-min", "5min", "5 minute"))
    return has_btc and has_5min
