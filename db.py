"""SQLite persistence layer for wallet scores, trade history, and market outcomes."""
import sqlite3
import time
import os
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "polymarket.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address      TEXT PRIMARY KEY,
    score        REAL    NOT NULL DEFAULT 0.5,
    win_count    INTEGER NOT NULL DEFAULT 0,
    loss_count   INTEGER NOT NULL DEFAULT 0,
    total_roi    REAL    NOT NULL DEFAULT 0.0,
    trade_count  INTEGER NOT NULL DEFAULT 0,
    last_updated INTEGER NOT NULL DEFAULT 0,
    label        TEXT    NOT NULL DEFAULT 'NEW'
);

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address   TEXT    NOT NULL,
    market_id        TEXT    NOT NULL,
    direction        TEXT    NOT NULL,
    amount_usd       REAL    NOT NULL DEFAULT 0.0,
    resolved         INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT,
    trade_timestamp  INTEGER NOT NULL,
    UNIQUE(wallet_address, market_id)
);

CREATE TABLE IF NOT EXISTS market_history (
    market_id      TEXT    PRIMARY KEY,
    question       TEXT,
    result         TEXT,
    smart_prob_up  REAL    NOT NULL DEFAULT 0.5,
    market_prob_up REAL    NOT NULL DEFAULT 0.5,
    resolved_at    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet_address);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_ts     ON trades(trade_timestamp);
CREATE INDEX IF NOT EXISTS idx_mh_resolved   ON market_history(resolved_at);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


# ── Wallet ────────────────────────────────────────────────────────────────────

def upsert_wallet(address: str, **kwargs) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wallets (address) VALUES (?)", (address,))
        if kwargs:
            sets = ", ".join(f"{k} = :{k}" for k in kwargs)
            conn.execute(
                f"UPDATE wallets SET {sets} WHERE address = :address",
                {**kwargs, "address": address},
            )


def get_wallet(address: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM wallets WHERE address = ?", (address,)
        ).fetchone()


# ── Trades ────────────────────────────────────────────────────────────────────

def upsert_trade(
    wallet_address: str,
    market_id: str,
    direction: str,
    amount_usd: float,
    timestamp: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO trades
               (wallet_address, market_id, direction, amount_usd, trade_timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (wallet_address, market_id, direction, amount_usd, timestamp),
        )


def resolve_market_trades(market_id: str, result: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE trades
               SET resolved = 1,
                   outcome  = CASE WHEN direction = ? THEN 'WIN' ELSE 'LOSS' END
               WHERE market_id = ? AND resolved = 0""",
            (result, market_id),
        )


def get_wallet_resolved_trades(address: str, days: int = 90) -> list:
    cutoff = int(time.time()) - days * 86400
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM trades
               WHERE wallet_address = ? AND resolved = 1 AND trade_timestamp > ?
               ORDER BY trade_timestamp DESC""",
            (address, cutoff),
        ).fetchall()


# ── Market history ────────────────────────────────────────────────────────────

def save_market_resolution(
    market_id: str,
    question: str,
    result: str,
    smart_prob_up: float,
    market_prob_up: float,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_history
               (market_id, question, result, smart_prob_up, market_prob_up, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (market_id, question, result, smart_prob_up, market_prob_up, int(time.time())),
        )


def get_recent_markets(limit: int = 5) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM market_history ORDER BY resolved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_session_accuracy(session_start: int) -> tuple[int, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT result, smart_prob_up FROM market_history WHERE resolved_at >= ?",
            (session_start,),
        ).fetchall()
    correct = sum(
        1 for r in rows
        if (r["smart_prob_up"] > 0.5 and r["result"] == "UP")
        or (r["smart_prob_up"] <= 0.5 and r["result"] == "DOWN")
    )
    return correct, len(rows)
