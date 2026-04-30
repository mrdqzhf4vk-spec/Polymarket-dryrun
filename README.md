# Polymarket BTC 5-min Dashboard

A live terminal dashboard that monitors Polymarket's Bitcoin UP/DOWN 5-minute
markets and displays **smart probability estimates** derived from trader analysis.

```
┌──────────────────────────────────────────────────────┐
│  ₿ BTC 5-MIN MARKET              Closes in: 03:42   │
│                                                      │
│  Market odds:   UP 58%  ░░░░░░████  DOWN 42%        │
│  Smart  odds:   UP 63%  ░░░░████░░  DOWN 37% ▲      │
│  Signal:        ████████░░░░  MODERATE BULL          │
│  Confidence:    ██░░░░░░░░░░  LOW (8 traders)        │
├──────────────────────────────────────────────────────┤
│ TRADERS IN THIS MARKET                               │
│ Wallet       │Side │    $  │Score│ W/L  │ Status    │
│ 0xabc…def   │ UP  │  4.2k │0.82 │ 14/3 │    ✓     │
│ 0x123…456   │DOWN │   800 │0.61 │  8/5 │    ✓     │
│ 0x789…abc   │ UP  │   150 │0.50 │   —  │   NEW    │
├──────────────────────────────────────────────────────┤
│ LAST 5 RESOLVED MARKETS                              │
│  UP ✓   DOWN ✗   UP ✓   UP ✓   DOWN ✗              │
│  Smart odds accuracy this session: 3/4 (75%)        │
└──────────────────────────────────────────────────────┘
```

---

## Setup

### Requirements

- Python 3.10+
- Libraries listed in `requirements.txt`

### Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure (optional)

```bash
cp .env.example .env
# Edit .env if you need a custom DB path
```

### Run

```bash
python main.py
```

Press **Ctrl-C** to stop. A session summary is printed on exit.

---

## How scoring works

Each trader wallet is assigned a score in **0.0 – 1.0** based on its resolved
BTC 5-min trade history stored in `polymarket.db`.

```
score = 0.70 × decay_weighted_win_rate
      + 0.30 × normalised_average_ROI
```

**Recency decay** – older trades have lower influence:
```
trade_weight *= 0.95 ^ days_since_trade
```

**Position-size influence** – log-scaled so a $10k whale matters more than a
$100 retail trader, but not 100× more:
```
size_weight = log10(position_usd + 1) / log10(max_position_usd + 1)
```

**Labels:**

| Label   | Meaning                              |
|---------|--------------------------------------|
| NEW     | No history found → score fixed at 0.5 |
| LIMITED | Fewer than 5 resolved trades → provisional 0.5 |
| ✓       | Fully scored wallet                  |

Scores are persisted in SQLite and survive restarts. They are recalculated
after every market resolution and cached for 1 hour otherwise.

---

## Smart probability

```
raw_signal    = Σ(wallet_score × log_size_weight × direction) / Σ(weights)
smart_prob_up = market_prob × 0.75 + raw_signal × 0.25
```

`alpha = 0.25` caps the signal's influence so smart odds never stray more than
~±15 pp from the raw market price.

Requires **at least 5 scored wallets**; displays "INSUFFICIENT DATA" otherwise.

---

## File structure

| File                    | Purpose                                      |
|-------------------------|----------------------------------------------|
| `main.py`               | Entry point; async polling and lifecycle loop |
| `polymarket_client.py`  | All API calls, retry logic, source fallbacks  |
| `trader_engine.py`      | Wallet scoring; DB read/write helpers         |
| `probability.py`        | Bayesian signal blending (pure function)      |
| `db.py`                 | SQLite schema and CRUD                        |
| `dashboard.py`          | Rich terminal layout and rendering            |
| `bot.log`               | Debug log (created on first run)              |
| `polymarket.db`         | SQLite DB (created on first run)              |

---

## Known API limitations

| Limitation | Mitigation |
|------------|------------|
| Polymarket subgraph sometimes returns partial history | Falls back to `data-api.polymarket.com/activity`; detects shallow results (>20 trades but <5 returned) and switches sources automatically |
| CLOB positions endpoint may be empty for new markets | Falls back to subgraph, then infers positions from recent CLOB trades |
| 429 rate limits on any endpoint | Exponential backoff: 1 s, 2 s, 4 s, 8 s; STALE indicator shown if all retries fail |
| 5-min markets rotate every ~5 minutes | Polls every 8 s; detects resolution via price snap (≥ 0.98 or ≤ 0.02) and metadata `closed` flag |
| Some wallets trade across hundreds of markets | History lookback limited to last 90 days |
