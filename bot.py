import requests
import json
import os
import time
from datetime import datetime, timezone

# הגדרות

BUDGET = 50.0
TRADE_SIZE_SMALL = 2.0   # מחיר 0.2-0.4 או 0.6-0.8
TRADE_SIZE_LARGE = 2.5   # מחיר 0.4-0.6
MIN_PRICE = 0.2
MAX_PRICE = 0.8
MIN_VOLUME = 1000
STATE_FILE = “state.json”

TELEGRAM_TOKEN = os.environ.get(“TELEGRAM_TOKEN”)
TELEGRAM_CHAT_ID = os.environ.get(“TELEGRAM_CHAT_ID”)

def send_telegram(message):
url = f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”
requests.post(url, json={“chat_id”: TELEGRAM_CHAT_ID, “text”: message, “parse_mode”: “HTML”})

def load_state():
if os.path.exists(STATE_FILE):
with open(STATE_FILE) as f:
return json.load(f)
return {
“balance”: BUDGET,
“positions”: {},   # conditionId -> {price, size, question, outcome, timestamp}
“closed”: [],      # עסקאות סגורות עם PnL
“total_pnl”: 0.0,
“trades_count”: 0
}

def save_state(state):
with open(STATE_FILE, “w”) as f:
json.dump(state, f, indent=2)

def get_markets():
try:
r = requests.get(“https://gamma-api.polymarket.com/markets”,
params={“active”: “true”, “closed”: “false”, “limit”: 200},
timeout=10)
return r.json()
except:
return []

def get_trade_size(price):
if 0.4 <= price <= 0.6:
return TRADE_SIZE_LARGE
return TRADE_SIZE_SMALL

def check_closed_positions(state):
“”“בדוק אם פוזיציות פתוחות נסגרו”””
closed_now = []
still_open = {}

```
for cid, pos in state["positions"].items():
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{cid}", timeout=10)
        market = r.json()
        
        is_closed = market.get("closed", False) or market.get("resolved", False)
        cur_price = float(market.get("bestBid", pos["price"]) or pos["price"])

        if is_closed:
            # בדוק אם ניצחנו
            winning_outcome = market.get("winningOutcome", "")
            won = winning_outcome.lower() == pos["outcome"].lower()
            
            if won:
                pnl = pos["size"] / pos["price"] - pos["size"]
            else:
                pnl = -pos["size"]

            state["total_pnl"] += pnl
            state["balance"] += pos["size"] + pnl if won else 0

            closed_now.append({
                "question": pos["question"],
                "outcome": pos["outcome"],
                "entry_price": pos["price"],
                "won": won,
                "pnl": round(pnl, 2)
            })
            state["closed"].append(closed_now[-1])
        else:
            still_open[cid] = pos

    except:
        still_open[cid] = pos

state["positions"] = still_open
return closed_now
```

def run_cycle(state):
markets = get_markets()
new_trades = []

```
for m in markets:
    if state["balance"] < TRADE_SIZE_SMALL:
        break

    cid = m.get("conditionId", "")
    if not cid or cid in state["positions"]:
        continue

    try:
        price = float(m.get("bestBid", 0) or 0)
        volume = float(m.get("volume24hr", 0) or m.get("volume", 0) or 0)
    except:
        continue

    if not (MIN_PRICE <= price <= MAX_PRICE):
        continue
    if volume < MIN_VOLUME:
        continue

    size = get_trade_size(price)
    if state["balance"] < size:
        continue

    question = m.get("question", "")[:70]
    state["balance"] -= size
    state["trades_count"] += 1
    state["positions"][cid] = {
        "price": price,
        "size": size,
        "question": question,
        "outcome": "Yes",
        "timestamp": int(datetime.now(timezone.utc).timestamp())
    }
    new_trades.append({"question": question, "price": price, "size": size})

return new_trades
```

def format_report(state, new_trades, closed_now):
lines = [”<b>📊 Polymarket Dry Run Report</b>”, “”]

```
# סטטוס כללי
lines.append(f"💰 יתרה: <b>${state['balance']:.2f}</b> / $50.00")
lines.append(f"📈 PnL כולל: <b>${state['total_pnl']:.2f}</b>")
lines.append(f"🔄 טריידים שבוצעו: {state['trades_count']}")
lines.append(f"📂 פוזיציות פתוחות: {len(state['positions'])}")
lines.append("")

# פוזיציות שנסגרו
if closed_now:
    lines.append("✅ <b>נסגרו עכשיו:</b>")
    for c in closed_now:
        emoji = "🟢" if c["won"] else "🔴"
        lines.append(f"{emoji} {c['question'][:50]}...")
        lines.append(f"   {'ניצחון' if c['won'] else 'הפסד'} | PnL: ${c['pnl']:.2f}")
    lines.append("")

# טריידים חדשים
if new_trades:
    lines.append(f"🆕 <b>טריידים חדשים ({len(new_trades)}):</b>")
    for t in new_trades[:5]:
        lines.append(f"• {t['question'][:50]}...")
        lines.append(f"  מחיר: {t['price']:.2f} | השקעה: ${t['size']:.2f}")
    if len(new_trades) > 5:
        lines.append(f"  ועוד {len(new_trades)-5}...")

return "\n".join(lines)
```

def main():
state = load_state()

```
# הודעת פתיחה
if state["trades_count"] == 0:
    send_telegram("🚀 <b>Polymarket Dry Run מתחיל!</b>\n\n💰 תקציב: $50.00\n📋 אסטרטגיה: מחירים 0.2-0.8, $2-2.5 לטריד\n⏰ דוח כל שעה")

while True:
    # בדוק פוזיציות שנסגרו
    closed_now = check_closed_positions(state)

    # הרץ מחזור חדש
    new_trades = run_cycle(state)

    # שמור מצב
    save_state(state)

    # שלח דוח אם יש חדשות או כל שעה
    if new_trades or closed_now or True:
        report = format_report(state, new_trades, closed_now)
        send_telegram(report)

    # אם נגמר התקציב ואין פוזיציות פתוחות
    if state["balance"] < TRADE_SIZE_SMALL and not state["positions"]:
        send_telegram(f"🏁 <b>Dry Run הסתיים!</b>\n\n💰 PnL סופי: ${state['total_pnl']:.2f}\n📊 טריידים: {state['trades_count']}\n{'🟢 רווח' if state['total_pnl'] > 0 else '🔴 הפסד'}")
        break

    time.sleep(3600)  # המתן שעה
```

if **name** == “**main**”:
main()
