"""Rich terminal dashboard – all display logic lives here.

No print() calls anywhere in this module; all text goes through the Live
renderable objects so they don't collide with the live display.
"""

from typing import Optional

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Block-character helpers ────────────────────────────────────────────────────

_FULL  = "█"
_EMPTY = "░"


def _bar(ratio: float, width: int = 12) -> str:
    """Solid left-to-right progress bar."""
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * width)
    return _FULL * filled + _EMPTY * (width - filled)


def _split_bar(left_ratio: float, width: int = 10) -> str:
    """Two-colour bar: left side fills from right; right side fills from left."""
    left_ratio = max(0.0, min(1.0, left_ratio))
    right_ratio = 1.0 - left_ratio
    l_filled = round(left_ratio * width)
    r_filled = round(right_ratio * width)
    return _EMPTY * (width - l_filled) + _FULL * l_filled + _FULL * r_filled + _EMPTY * (width - r_filled)


# ── Header / market summary panel ─────────────────────────────────────────────

def make_header_panel(
    question: str,
    closes_in_secs: Optional[int],
    market_prob_up: float,
    smart_prob_up: float,
    confidence: str,
    scored_count: int,
    signal_label: str,
    is_stale: bool,
) -> Panel:
    up_pct  = round(market_prob_up * 100)
    dn_pct  = 100 - up_pct
    s_up    = round(smart_prob_up * 100)
    s_dn    = 100 - s_up

    # Countdown
    if closes_in_secs is not None and closes_in_secs >= 0:
        m, s = divmod(closes_in_secs, 60)
        closes_str = f"[bold white]{m:02d}:{s:02d}[/bold white]"
    else:
        closes_str = "[dim]??:??[/dim]"

    stale_tag = "  [bold red blink]STALE[/bold red blink]" if is_stale else ""

    # Direction arrow on smart line
    if "INSUFFICIENT" not in confidence and "NO DATA" not in confidence:
        arrow = " [bold green]▲[/bold green]" if smart_prob_up > market_prob_up else " [bold red]▼[/bold red]"
    else:
        arrow = ""

    mkt_bar   = _split_bar(market_prob_up)
    smart_bar = _split_bar(smart_prob_up)

    # Signal strength bar (0 = 50/50, 1 = full conviction)
    signal_strength = abs(smart_prob_up - 0.5) / 0.5
    sig_bar   = _bar(signal_strength, 12)
    sig_color = "green" if "BULL" in signal_label else "red" if "BEAR" in signal_label else "white"

    # Confidence bar
    conf_ratio = min(scored_count / 20.0, 1.0)
    conf_bar   = _bar(conf_ratio, 12)
    if "INSUFFICIENT" in confidence or "NO DATA" in confidence:
        conf_str = f"[yellow]{confidence}[/yellow]"
    else:
        conf_color = {"HIGH": "green", "MEDIUM": "yellow", "LOW": "red"}.get(confidence, "white")
        conf_str = f"[{conf_color}]{confidence}[/{conf_color}] ({scored_count} traders)"

    lines = Text.assemble(
        Text.from_markup(
            f"  [bold cyan]₿ BTC 5-MIN MARKET[/bold cyan]{stale_tag}"
            f"      [dim]Closes in:[/dim] {closes_str}\n"
            "\n"
            f"  [dim]Market odds:[/dim]   "
            f"[bold green]UP {up_pct:2d}%[/bold green]  {mkt_bar}  "
            f"[bold red]DOWN {dn_pct:2d}%[/bold red]\n"
            f"  [dim]Smart  odds:[/dim]   "
            f"[bold green]UP {s_up:2d}%[/bold green]  {smart_bar}  "
            f"[bold red]DOWN {s_dn:2d}%[/bold red]{arrow}\n"
            f"  [dim]Signal:      [/dim]  [{sig_color}]{sig_bar}[/{sig_color}]"
            f"  [{sig_color}]{signal_label}[/{sig_color}]\n"
            f"  [dim]Confidence:  [/dim]  {conf_bar}  {conf_str}"
        )
    )

    return Panel(
        lines,
        title=f"[bold]{question[:72]}[/bold]",
        border_style="cyan",
        expand=True,
    )


# ── Traders table ──────────────────────────────────────────────────────────────

def make_traders_panel(traders: list[dict]) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold magenta",
        expand=True,
        pad_edge=False,
        show_edge=False,
    )
    tbl.add_column("Wallet",  style="cyan",  no_wrap=True, min_width=14)
    tbl.add_column("Side",    justify="center", min_width=6)
    tbl.add_column("$",       justify="right",  min_width=8)
    tbl.add_column("Score",   justify="center", min_width=6)
    tbl.add_column("W/L",     justify="center", min_width=7)
    tbl.add_column("Status",  justify="center", min_width=8)

    shown = traders[:15]
    for t in shown:
        addr = str(t.get("address", ""))
        short = f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr

        side    = t.get("side", "?")
        side_mk = "[bold green] UP [/bold green]" if side == "UP" else "[bold red]DOWN[/bold red]"

        amount = t.get("amount", 0.0)
        if amount >= 1_000_000:
            amt_str = f"${amount/1_000_000:.1f}M"
        elif amount >= 1_000:
            amt_str = f"${amount/1_000:.1f}k"
        else:
            amt_str = f"${amount:.0f}"

        score = t.get("score", 0.5)
        sc_color = "green" if score > 0.65 else "red" if score < 0.40 else "yellow"
        score_mk = f"[{sc_color}]{score:.2f}[/{sc_color}]"

        wins   = t.get("win_count", 0)
        losses = t.get("loss_count", 0)
        wl_str = f"{wins}/{losses}" if (wins + losses) > 0 else "[dim]—[/dim]"

        label = t.get("label", "")
        if label == "NEW":
            status_mk = "[dim]NEW[/dim]"
        elif label == "LIMITED":
            status_mk = "[yellow]LTD[/yellow]"
        else:
            status_mk = "[bold green]✓[/bold green]"

        tbl.add_row(short, side_mk, amt_str, score_mk, wl_str, status_mk)

    if not shown:
        tbl.add_row("[dim]No positions found yet[/dim]", "", "", "", "", "")

    extra = ""
    if len(traders) > 15:
        extra = f"\n  [dim]… and {len(traders) - 15} more traders[/dim]"

    return Panel(
        Group(tbl, Text.from_markup(extra)) if extra else tbl,
        title=f"[bold]TRADERS IN THIS MARKET[/bold]  [dim]({len(traders)} total)[/dim]",
        border_style="blue",
        expand=True,
    )


# ── History & accuracy panel ───────────────────────────────────────────────────

def make_history_panel(
    recent_markets: list,
    session_correct: int,
    session_total: int,
) -> Panel:
    # Build resolved-market ribbon
    results_line = "  "
    for m in recent_markets:
        result       = _row_val(m, "result", "?")
        smart_up     = float(_row_val(m, "smart_prob_up", 0.5))
        was_correct  = (smart_up > 0.5 and result == "UP") or (smart_up <= 0.5 and result == "DOWN")
        tick_color   = "green" if was_correct else "red"
        res_color    = "green" if result == "UP" else "red"
        tick         = "✓" if was_correct else "✗"
        results_line += f"[{res_color}]{result}[/{res_color}] [{tick_color}]{tick}[/{tick_color}]   "

    if not recent_markets:
        results_line = "[dim]  No resolved markets yet[/dim]"

    if session_total > 0:
        pct = round(session_correct / session_total * 100)
        bar = _bar(session_correct / session_total, 10)
        acc_str = (
            f"  [bold]Smart odds accuracy this session:[/bold] "
            f"[green]{session_correct}[/green]/{session_total}  {bar}  [bold]{pct}%[/bold]"
        )
    else:
        acc_str = "  [dim]Smart odds accuracy this session: awaiting first resolution…[/dim]"

    return Panel(
        Text.from_markup(f"{results_line}\n{acc_str}"),
        title="[bold]LAST 5 RESOLVED MARKETS[/bold]",
        border_style="green",
        expand=True,
    )


# ── Waiting / status screens ───────────────────────────────────────────────────

def make_waiting_display(message: str = "Searching for active BTC 5-min market…") -> Panel:
    return Panel(
        Text.from_markup(f"\n  [bold yellow]⏳  {message}[/bold yellow]\n"),
        title="[bold cyan]₿ BTC 5-MIN MARKET MONITOR[/bold cyan]",
        border_style="yellow",
        expand=True,
    )


def make_resolved_display(result: str, smart_was_right: bool) -> Panel:
    color   = "green" if result == "UP" else "red"
    verdict = "✓ Smart odds were correct" if smart_was_right else "✗ Smart odds were wrong"
    vcolor  = "green" if smart_was_right else "red"
    body    = (
        f"\n  [bold {color}]MARKET RESOLVED: BTC {result}[/bold {color}]\n\n"
        f"  [{vcolor}]{verdict}[/{vcolor}]\n\n"
        "  [dim]Updating wallet scores… loading next market in 5s[/dim]\n"
    )
    return Panel(
        Text.from_markup(body),
        title="[bold cyan]₿ BTC 5-MIN MARKET MONITOR[/bold cyan]",
        border_style=color,
        expand=True,
    )


# ── Full composite layout ──────────────────────────────────────────────────────

def make_full_display(
    question: str,
    closes_in_secs: Optional[int],
    market_prob_up: float,
    smart_prob_up: float,
    confidence: str,
    scored_count: int,
    signal_label: str,
    is_stale: bool,
    traders: list[dict],
    recent_markets: list,
    session_correct: int,
    session_total: int,
) -> Group:
    return Group(
        make_header_panel(
            question, closes_in_secs, market_prob_up, smart_prob_up,
            confidence, scored_count, signal_label, is_stale,
        ),
        make_traders_panel(traders),
        make_history_panel(recent_markets, session_correct, session_total),
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row_val(row: object, key: str, default: object) -> object:
    """Extract a value from either a sqlite3.Row or a plain dict."""
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return default
