"""Backtest reports: trades CSV, summary JSON/Markdown and an equity chart."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .backtest import Result
from .metrics import ByBook, GoalCheck, Monthly, Summarize


def _Fmt(v):
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def Write(res: Result, outdir: Path | str, title: str) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    s = Summarize(res)
    goals = GoalCheck(s)
    res.trades.to_csv(out / "trades.csv", index=False)
    (out / "summary.json").write_text(json.dumps({"summary": s, "goals": goals}, indent=2, default=float))
    bb = ByBook(res)
    mo = Monthly(res)

    lines = [f"# {title}", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {_Fmt(v)} |" for k, v in s.items()]
    lines += ["", "## Goals", "", "| goal | met |", "|---|---|"]
    lines += [f"| {k} | {'YES' if v else 'no'} |" for k, v in goals.items()]
    if not bb.empty:
        lines += ["", "## By book", "", bb.round(2).to_markdown()]
    if not mo.empty:
        lines += ["", "## Monthly P&L (USD)", "", mo.round(0).to_frame("pnl").to_markdown()]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    _Chart(res, out / "equity.png", title)
    return {"summary": s, "goals": goals}


def _Chart(res: Result, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eq = res.equity
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 1]})
    a1.plot(eq.index, eq.equity, lw=1.2, color="#1f6feb", label="equity (close-marked)")
    a1.fill_between(eq.index, eq.equity_low, eq.equity, color="#1f6feb", alpha=0.15, label="intrabar worst")
    a1.axhline(0, color="grey", lw=0.6)
    a1.set_ylabel("P&L (USD)")
    a1.set_title(title)
    a1.legend(loc="upper left")
    dd = eq.equity.cummax().clip(lower=0) - eq.equity_low
    a2.fill_between(eq.index, 0, -dd, color="#d1242f", alpha=0.5)
    a2.axhline(-2000, color="#d1242f", ls="--", lw=0.8, label="-$2,000 limit")
    a2.set_ylabel("drawdown")
    a2.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def TradesMarkdown(t: pd.DataFrame, n: int = 15) -> str:
    if t.empty:
        return "_no trades_"
    cols = ["book", "side", "entry_time", "entry", "stop", "tp1", "qty", "pnl", "r", "exit_reason"]
    return t.nlargest(n, "pnl")[cols].round(2).to_markdown(index=False)
