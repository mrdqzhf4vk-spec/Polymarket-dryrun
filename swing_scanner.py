"""
Swing Trading Scanner
Analyzes a broad universe of stocks for short-to-medium term swing setups (days to weeks).
Scoring based on: RSI, MACD, Bollinger Bands, Moving Averages, Volume, and Momentum.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# ── Universe: S&P 500 large caps + high-volume mid caps ──────────────────────
UNIVERSE = [
    # Mega-cap tech
    "AAPL","MSFT","NVDA","GOOGL","GOOG","AMZN","META","TSLA","AVGO","ORCL",
    # Financials
    "JPM","BAC","WFC","GS","MS","C","BLK","AXP","COF","USB",
    # Healthcare
    "UNH","JNJ","LLY","ABBV","MRK","PFE","TMO","ABT","DHR","BMY",
    # Energy
    "XOM","CVX","COP","EOG","SLB","OXY","PSX","MPC","VLO","HAL",
    # Consumer
    "WMT","COST","HD","LOW","TGT","MCD","SBUX","NKE","CMG","YUM",
    # Industrials
    "CAT","DE","HON","GE","RTX","LMT","NOC","BA","UPS","FDX",
    # Semiconductors
    "AMD","INTC","QCOM","MU","AMAT","LRCX","KLAC","MRVL","SMCI","ON",
    # Cloud / SaaS
    "CRM","NOW","SNOW","PLTR","PANW","CRWD","ZS","NET","DDOG","MDB",
    # Communication
    "NFLX","DIS","CMCSA","T","VZ","TMUS","CHTR","PARA","WBD","SPOT",
    # ETFs (sector proxies)
    "SPY","QQQ","IWM","XLF","XLE","XLK","XLV","XBI","ARKK","SMH",
    # High-beta / momentum favourites
    "COIN","MSTR","RBLX","HOOD","SOFI","UPST","AFRM","LCID","RIVN","NIO",
    # Biotech / speculative
    "MRNA","BNTX","GILD","REGN","VRTX","BIIB","IONS","SRPT","NBIX","EXAS",
    # REITs & Utilities
    "AMT","PLD","EQIX","O","SPG","DLR","PSA","CCI","WEC","NEE",
    # Materials & Chemicals
    "LIN","APD","ECL","SHW","NEM","FCX","NUE","CF","MOS","ALB",
]

# Remove duplicates
UNIVERSE = list(dict.fromkeys(UNIVERSE))

# ── Technical Indicator Helpers ───────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def bollinger(series: pd.Series, period=20, std_dev=2):
    sma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    pct_b = (series - lower) / (upper - lower)
    return upper, sma, lower, pct_b

def atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── Score a single ticker ─────────────────────────────────────────────────────

def score_ticker(ticker: str, df: pd.DataFrame) -> dict:
    if df is None or len(df) < 60:
        return None

    close = df["Close"].squeeze()
    high  = df["High"].squeeze()
    low   = df["Low"].squeeze()
    volume= df["Volume"].squeeze()

    # ── Indicators ──
    rsi14      = rsi(close, 14)
    rsi_val    = rsi14.iloc[-1]
    rsi_prev   = rsi14.iloc[-2]

    macd_line, sig_line, hist = macd(close)
    macd_val   = macd_line.iloc[-1]
    sig_val    = sig_line.iloc[-1]
    hist_curr  = hist.iloc[-1]
    hist_prev  = hist.iloc[-2]

    bb_up, bb_mid, bb_low, pct_b = bollinger(close)
    pct_b_val  = pct_b.iloc[-1]

    sma20  = close.rolling(20).mean()
    sma50  = close.rolling(50).mean()
    ema9   = close.ewm(span=9, adjust=False).mean()

    sma20_val = sma20.iloc[-1]
    sma50_val = sma50.iloc[-1]
    ema9_val  = ema9.iloc[-1]
    price     = close.iloc[-1]
    prev_price= close.iloc[-2]

    atr_val   = atr(high, low, close).iloc[-1]
    atr_pct   = (atr_val / price) * 100

    vol_avg20 = volume.rolling(20).mean().iloc[-1]
    vol_today = volume.iloc[-1]
    vol_ratio = vol_today / vol_avg20 if vol_avg20 > 0 else 1

    # 5-day and 10-day momentum
    mom5   = (price / close.iloc[-6] - 1) * 100  if len(close) >= 6  else 0
    mom10  = (price / close.iloc[-11] - 1) * 100 if len(close) >= 11 else 0
    mom20  = (price / close.iloc[-21] - 1) * 100 if len(close) >= 21 else 0

    # 52-week range
    high52 = close.rolling(252).max().iloc[-1]
    low52  = close.rolling(252).min().iloc[-1]
    rng52  = (price - low52) / (high52 - low52) * 100 if high52 > low52 else 50

    # ── Scoring Logic ──────────────────────────────────────────────────────
    score  = 0
    signals = []
    direction = "NEUTRAL"

    # RSI signals (0-30 pts)
    if rsi_val < 30:
        score += 30
        signals.append(f"RSI oversold ({rsi_val:.1f})")
        direction = "LONG"
    elif rsi_val < 40:
        score += 20
        signals.append(f"RSI approaching oversold ({rsi_val:.1f})")
        direction = "LONG"
    elif rsi_val > 70:
        score += 25
        signals.append(f"RSI overbought ({rsi_val:.1f})")
        direction = "SHORT"
    elif rsi_val > 60:
        score += 15
        signals.append(f"RSI elevated ({rsi_val:.1f})")
        direction = "SHORT"

    # RSI turning up from oversold / turning down from overbought
    if rsi_prev < 30 and rsi_val > rsi_prev:
        score += 15
        signals.append("RSI bouncing from oversold")
        direction = "LONG"

    # MACD signals (0-25 pts)
    if macd_val > sig_val and hist_curr > hist_prev and hist_curr > 0:
        score += 20
        signals.append("MACD bullish + expanding histogram")
        direction = "LONG"
    elif macd_val < 0 and hist_curr > hist_prev:
        score += 12
        signals.append("MACD bearish but histogram improving")
        if direction == "NEUTRAL": direction = "LONG"
    elif macd_val < sig_val and hist_curr < hist_prev and hist_curr < 0:
        score += 18
        signals.append("MACD bearish + expanding neg histogram")
        direction = "SHORT"

    # Bollinger Band signals (0-20 pts)
    if pct_b_val < 0.05:
        score += 20
        signals.append(f"%B={pct_b_val:.2f} (near/below lower BB)")
        if direction != "SHORT": direction = "LONG"
    elif pct_b_val < 0.20:
        score += 12
        signals.append(f"%B={pct_b_val:.2f} (lower BB zone)")
        if direction != "SHORT": direction = "LONG"
    elif pct_b_val > 0.95:
        score += 18
        signals.append(f"%B={pct_b_val:.2f} (near/above upper BB)")
        if direction != "LONG": direction = "SHORT"

    # Moving average alignment (0-15 pts)
    if price > sma20_val > sma50_val and ema9_val > sma20_val:
        score += 15
        signals.append("Price > EMA9 > SMA20 > SMA50 (bullish stack)")
        if direction != "SHORT": direction = "LONG"
    elif price < sma20_val < sma50_val and ema9_val < sma20_val:
        score += 12
        signals.append("Bearish MA stack")
        if direction != "LONG": direction = "SHORT"
    elif price > sma20_val and sma20_val > sma50_val:
        score += 8
        signals.append("Above SMA20 & SMA50")
        if direction != "SHORT": direction = "LONG"

    # Pullback to MA support (high value setup)
    ma_diff_pct = abs(price - sma20_val) / sma20_val * 100
    if direction == "LONG" and ma_diff_pct < 2.0 and price > sma50_val:
        score += 10
        signals.append(f"Tight pullback to SMA20 ({ma_diff_pct:.1f}%)")

    # Volume confirmation (0-10 pts)
    if vol_ratio > 2.0:
        score += 10
        signals.append(f"Volume surge {vol_ratio:.1f}x avg")
    elif vol_ratio > 1.5:
        score += 6
        signals.append(f"Above-avg volume {vol_ratio:.1f}x")

    # Momentum (0-10 pts)
    if direction == "LONG":
        if mom5 > 3 and mom10 > 5:
            score += 8
            signals.append(f"Strong momentum: +{mom5:.1f}% (5d), +{mom10:.1f}% (10d)")
        elif mom5 > 1:
            score += 4
            signals.append(f"Positive momentum: +{mom5:.1f}% (5d)")
        elif mom5 < -5 and rsi_val < 40:
            score += 6
            signals.append(f"Oversold pullback: {mom5:.1f}% (5d)")
    elif direction == "SHORT":
        if mom5 < -3 and mom10 < -5:
            score += 8
            signals.append(f"Bearish momentum: {mom5:.1f}% (5d), {mom10:.1f}% (10d)")

    # 52-week range context
    if direction == "LONG" and rng52 < 25:
        score += 5
        signals.append(f"Near 52-wk low ({rng52:.0f}% of range)")
    elif direction == "LONG" and rng52 > 75:
        score += 3
        signals.append(f"Near 52-wk high ({rng52:.0f}% of range) — breakout zone")

    # ATR / volatility filter (need enough movement to be worth trading)
    if atr_pct < 0.8:
        score = int(score * 0.7)  # penalise low-volatility names

    return {
        "ticker":    ticker,
        "price":     round(price, 2),
        "direction": direction,
        "score":     score,
        "rsi":       round(rsi_val, 1),
        "macd_hist": round(hist_curr, 4),
        "pct_b":     round(pct_b_val, 3),
        "vol_ratio": round(vol_ratio, 2),
        "mom5d":     round(mom5, 1),
        "mom10d":    round(mom10, 1),
        "atr_pct":   round(atr_pct, 2),
        "sma20":     round(sma20_val, 2),
        "sma50":     round(sma50_val, 2),
        "52wk_pos":  round(rng52, 1),
        "signals":   signals,
    }


# ── Main scanner ──────────────────────────────────────────────────────────────

def run_scanner():
    print(f"\n{'='*70}")
    print(f"  SWING TRADING SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Universe: {len(UNIVERSE)} symbols  |  Lookback: 12 months")
    print(f"{'='*70}\n")

    end   = datetime.today()
    start = end - timedelta(days=365)

    results = []
    failed  = []

    print(f"Downloading data in batches...\n")

    # Download in batches of 20 to avoid rate limits
    batch_size = 20
    all_data = {}

    for i in range(0, len(UNIVERSE), batch_size):
        batch = UNIVERSE[i:i+batch_size]
        print(f"  Batch {i//batch_size + 1}/{(len(UNIVERSE)+batch_size-1)//batch_size}: {', '.join(batch[:5])}...")
        try:
            raw = yf.download(
                batch,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            for t in batch:
                try:
                    if len(batch) == 1:
                        all_data[t] = raw
                    else:
                        if t in raw.columns.get_level_values(0):
                            all_data[t] = raw[t]
                        else:
                            all_data[t] = None
                except Exception:
                    all_data[t] = None
        except Exception as e:
            print(f"    Batch error: {e}")
            for t in batch:
                all_data[t] = None

    print(f"\nScoring {len(UNIVERSE)} symbols...\n")

    for ticker in UNIVERSE:
        df = all_data.get(ticker)
        try:
            result = score_ticker(ticker, df)
            if result and result["score"] >= 40 and result["direction"] != "NEUTRAL":
                results.append(result)
            elif result is None or (df is not None and len(df) < 60):
                failed.append(ticker)
        except Exception as e:
            failed.append(ticker)

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # ── LONG setups ───────────────────────────────────────────────────────
    longs  = [r for r in results if r["direction"] == "LONG"]
    shorts = [r for r in results if r["direction"] == "SHORT"]

    def print_table(rows, title, emoji):
        print(f"\n{'─'*70}")
        print(f"  {emoji}  {title}  ({len(rows)} setups)")
        print(f"{'─'*70}")
        if not rows:
            print("  No qualifying setups found.")
            return
        header = f"{'TICKER':<8} {'PRICE':>8} {'SCORE':>6} {'RSI':>6} {'%B':>6} {'5d%':>7} {'10d%':>7} {'VOL':>6} {'ATR%':>6}"
        print(header)
        print("  " + "-"*68)
        for r in rows:
            line = (
                f"{r['ticker']:<8} "
                f"${r['price']:>7.2f} "
                f"{r['score']:>6} "
                f"{r['rsi']:>6.1f} "
                f"{r['pct_b']:>6.3f} "
                f"{r['mom5d']:>+7.1f}% "
                f"{r['mom10d']:>+7.1f}% "
                f"{r['vol_ratio']:>5.1f}x "
                f"{r['atr_pct']:>5.1f}%"
            )
            print(line)
            for sig in r["signals"][:3]:
                print(f"         ↳ {sig}")

    print_table(longs[:20],  "TOP LONG / BULLISH SWING SETUPS",  "📈")
    print_table(shorts[:15], "TOP SHORT / BEARISH SWING SETUPS", "📉")

    # ── Summary by sector tags ────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  QUICK-REFERENCE WATCHLIST")
    print(f"{'─'*70}")

    top_all = (longs[:10] + shorts[:5])
    top_all.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  {'TICKER':<8} {'DIR':<6} {'SCORE':>6}  SIGNALS")
    for r in top_all:
        sigs = " | ".join(r["signals"][:2])
        print(f"  {r['ticker']:<8} {r['direction']:<6} {r['score']:>6}  {sigs}")

    if failed:
        print(f"\n  [Skipped {len(failed)} symbols with insufficient data: {', '.join(failed[:10])}{'...' if len(failed)>10 else ''}]")

    print(f"\n{'='*70}")
    print("  METHODOLOGY NOTE")
    print(f"{'─'*70}")
    print("  Score  40-59  → Weak setup (watch list)")
    print("  Score  60-79  → Moderate setup")
    print("  Score  80-99  → Strong setup")
    print("  Score 100+    → High-conviction setup")
    print()
    print("  Indicators: RSI-14, MACD(12/26/9), Bollinger Bands(20,2),")
    print("  EMA9, SMA20, SMA50, ATR-14, 5/10-day momentum, volume ratio")
    print()
    print("  ⚠  This is for informational purposes only.")
    print("     Always do your own research and manage risk.")
    print(f"{'='*70}\n")

    return results


if __name__ == "__main__":
    run_scanner()
