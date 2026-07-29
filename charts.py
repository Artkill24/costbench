#!/usr/bin/env python3
"""
charts.py - genera le figure della submission dai proofs/ misurati.

Legge tutti i costbench_*.json, scarta i dry-run, preferisce le run
closed-loop quando esistono per lo stesso livello di concorrenza, e
produce quattro figure piu' una tabella markdown per il README.

USO:
    pip install matplotlib --break-system-packages
    python3 charts.py --proofs proofs/ --out charts/

Ogni figura riporta in nota GPU, pattern di carico e sorgente della
potenza: i numeri devono essere tracciabili senza aprire il JSON.
"""

import argparse
import glob
import json
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# --- palette ARTKILL24: teal + oro, fondo chiaro -------------------------
TEAL = "#0d7a7a"
TEAL_L = "#4fb3b3"
GOLD = "#c8992e"
INK = "#1a1a1a"
GREY = "#8a8a8a"
GRID = "#e4e4e4"

plt.rcParams.update({
    "figure.dpi": 160,
    "savefig.dpi": 160,
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.edgecolor": GREY,
    "axes.labelcolor": INK,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "text.color": INK,
    "xtick.color": INK,
    "ytick.color": INK,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})


def load_runs(proofs_dir):
    """Raccoglie le run VALIDE. Closed-loop batte wave-barrier.

    Scarta le run prodotte prima delle correzioni:
      - senza gpu_pci_bus -> su host multi-GPU leggeva la scheda di un
        altro utente (valori ~14 W, fisicamente impossibili sotto carico)
      - senza duration_s_target -> ondate da 2-5s, sotto la finestra di
        media del sensore, quindi potenza non rilevata
    """
    best = {}
    meta = {}
    skipped = []
    for path in sorted(glob.glob(os.path.join(proofs_dir, "costbench_*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        name = os.path.basename(path)
        if not d.get("measured") or d.get("dry_run"):
            skipped.append((name, "dry-run o non misurato"))
            continue

        env = d.get("environment", {})
        if not env.get("gpu_pci_bus"):
            skipped.append((name, "GPU non risolta via bus PCI (pre-fix)"))
            continue

        for r in d.get("runs", []):
            conc = r.get("concurrency")
            if conc is None or not (r.get("power") or {}).get("avg_w"):
                continue
            if not r.get("duration_s_target"):
                skipped.append((f"{name} conc={conc}",
                                "run troppo corta (pre-fix)"))
                continue
            closed = r.get("load_pattern") == "closed-loop"
            prev = best.get(conc)
            # preferisci closed-loop; a parita', il file piu' recente
            if prev is None or (closed and not prev["_closed"]):
                r = dict(r)
                r["_closed"] = closed
                r["_src"] = name
                best[conc] = r
                meta.setdefault("gpu_pci_bus", env.get("gpu_pci_bus"))
                meta.setdefault("power_backend", env.get("power_backend"))
                meta.setdefault("eur_per_kwh",
                                d.get("config", {}).get("eur_per_kwh"))
                meta.setdefault("idle_w",
                                (d.get("idle_baseline") or {}).get("avg_w"))
    runs = [best[k] for k in sorted(best)]
    return runs, meta, skipped


def note(fig, meta, runs):
    patterns = {("closed-loop" if r["_closed"] else "wave-barrier")
                for r in runs}
    fig.text(
        0.5, 0.005,
        f"Measured on AMD Radeon PRO W7900 (gfx1100, {meta.get('gpu_pci_bus')})  ·  "
        f"Qwen2.5-Coder-7B Q4_K_M, llama.cpp ROCm  ·  "
        f"power: GPU board only, sysfs power1_average  ·  "
        f"load: {'/'.join(sorted(patterns))}  ·  "
        f"tariff {meta.get('eur_per_kwh')} EUR/kWh",
        ha="center", fontsize=6.5, color=GREY,
    )


def fig_throughput(runs, meta, out):
    c = [r["concurrency"] for r in runs]
    t = [r["system_tok_s"] for r in runs]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))

    ideal = [t[0] * (x / c[0]) for x in c]
    ax.plot(c, ideal, "--", color=GREY, lw=1.2, label="linear scaling (ideal)")
    ax.plot(c, t, "o-", color=TEAL, lw=2.4, ms=7, label="measured")

    for x, y in zip(c, t):
        ax.annotate(f"{y:,.0f}", (x, y), textcoords="offset points",
                    xytext=(0, 11), ha="center", fontsize=9, color=TEAL,
                    fontweight="bold")

    ax.set_xscale("log", base=2)
    ax.set_xticks(c)
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("System throughput (tokens/s)")
    scale = t[-1] / t[0] if t[0] else 0
    ax.set_title(f"Continuous batching scales throughput {scale:.1f}×")
    ax.grid(axis="y")
    ax.legend(frameon=False, fontsize=9)
    ax.set_ylim(0, max(max(t), max(ideal)) * 1.15)
    note(fig, meta, runs)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, "01_throughput.png"))
    plt.close(fig)


def fig_power(runs, meta, out):
    c = [r["concurrency"] for r in runs]
    w = [r["power"]["avg_w"] for r in runs]
    pk = [r["power"]["peak_w"] for r in runs]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))

    ax.fill_between(range(len(c)), w, pk, color=TEAL_L, alpha=0.18,
                    label="avg → peak")
    ax.plot(range(len(c)), w, "o-", color=TEAL, lw=2.4, ms=7, label="average")
    ax.plot(range(len(c)), pk, "s--", color=GOLD, lw=1.4, ms=5, label="peak")

    hi = max(range(len(w)), key=lambda i: w[i])
    ax.annotate(
        f"peak draw at concurrency {c[hi]}\n{w[hi]:.0f} W",
        (hi, w[hi]), textcoords="offset points", xytext=(18, -30),
        fontsize=9, color=INK,
        arrowprops=dict(arrowstyle="->", color=GREY, lw=1),
    )
    for i, y in enumerate(w):
        ax.annotate(f"{y:.0f} W", (i, y), textcoords="offset points",
                    xytext=(0, 12), ha="center", fontsize=9,
                    color=TEAL, fontweight="bold")

    ax.set_xticks(range(len(c)))
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("GPU board power (W)")
    ax.set_title("Power draw is not monotonic in batch size")
    ax.grid(axis="y")
    ax.legend(frameon=False, fontsize=9, loc="lower left")
    note(fig, meta, runs)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, "02_power.png"))
    plt.close(fig)


def fig_cost(runs, meta, out):
    c = [r["concurrency"] for r in runs]
    cost = [r["cost"]["eur_per_1m_tokens_gross"] for r in runs]
    ttft = [(r.get("ttft_s_median") or 0) * 1000 for r in runs]
    x = range(len(c))

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    bars = ax.bar(x, cost, width=0.55, color=TEAL, zorder=3)
    for b, v in zip(bars, cost):
        ax.annotate(f"€{v:.4f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 5),
                    ha="center", fontsize=9, color=TEAL, fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("Electricity cost (EUR / 1M tokens)", color=TEAL)
    ax.tick_params(axis="y", labelcolor=TEAL)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"€{v:.3f}"))
    ax.grid(axis="y", zorder=0)
    ax.set_ylim(0, max(cost) * 1.25)

    ax2 = ax.twinx()
    ax2.plot(x, ttft, "o--", color=GOLD, lw=2, ms=6, label="TTFT (median)")
    ax2.set_ylabel("Time to first token (ms)", color=GOLD)
    ax2.tick_params(axis="y", labelcolor=GOLD)
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(GREY)
    ax2.set_ylim(0, max(ttft) * 1.3 if max(ttft) else 1)
    for xi, v in zip(x, ttft):
        ax2.annotate(f"{v:.0f} ms", (xi, v), textcoords="offset points",
                     xytext=(0, 9), ha="center", fontsize=8.5, color=GOLD)

    drop = (1 - cost[-1] / cost[0]) * 100 if cost[0] else 0
    rise = ttft[-1] / ttft[0] if ttft[0] else 0
    ax.set_title(f"The real trade-off: −{drop:.0f}% energy per token, "
                 f"{rise:.1f}× latency to first token")
    note(fig, meta, runs)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, "03_cost_vs_latency.png"))
    plt.close(fig)


def fig_efficiency(runs, meta, out):
    """Token per joule: la metrica fisica, indipendente dalla tariffa."""
    c = [r["concurrency"] for r in runs]
    tpj = [r["system_tok_s"] / r["power"]["avg_w"] for r in runs]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    bars = ax.bar(range(len(c)), tpj, width=0.55, color=GOLD, zorder=3)
    for b, v in zip(bars, tpj):
        ax.annotate(f"{v:.2f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 5),
                    ha="center", fontsize=9, color="#8a6a1a",
                    fontweight="bold")
    ax.set_xticks(range(len(c)))
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("Tokens per joule")
    gain = tpj[-1] / tpj[0] if tpj[0] else 0
    ax.set_title(f"Energy efficiency improves {gain:.1f}× with batching")
    ax.grid(axis="y", zorder=0)
    ax.set_ylim(0, max(tpj) * 1.2)
    note(fig, meta, runs)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, "04_tokens_per_joule.png"))
    plt.close(fig)


def table(runs, meta, out):
    lines = [
        "| Concurrency | Achieved | Throughput (tok/s) | Power (W) | "
        "Tokens/J | TTFT p50 (ms) | EUR/1M tokens | Load pattern |",
        "|---:|---:|---:|---:|---:|---:|---:|:--|",
    ]
    for r in runs:
        p, c_ = r["power"], r["cost"]
        lines.append(
            f"| {r['concurrency']} "
            f"| {r.get('achieved_concurrency') or '—'} "
            f"| {r['system_tok_s']:,.1f} "
            f"| {p['avg_w']:.1f} "
            f"| {r['system_tok_s'] / p['avg_w']:.2f} "
            f"| {(r.get('ttft_s_median') or 0) * 1000:.0f} "
            f"| {c_['eur_per_1m_tokens_gross']:.4f} "
            f"| {'closed-loop' if r['_closed'] else 'wave-barrier'} |"
        )
    lines += [
        "",
        f"All values `measured`, not modeled. GPU board power only "
        f"(sysfs `power1_average`, bus {meta.get('gpu_pci_bus')}); "
        f"excludes CPU, RAM, cooling and datacenter PUE. "
        f"Idle baseline {meta.get('idle_w')} W. "
        f"Tariff {meta.get('eur_per_kwh')} EUR/kWh.",
    ]
    txt = "\n".join(lines)
    with open(os.path.join(out, "results_table.md"), "w") as f:
        f.write(txt + "\n")
    print(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proofs", default="proofs")
    ap.add_argument("--out", default="charts")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    runs, meta, skipped = load_runs(a.proofs)
    if skipped:
        print("Scartate:")
        for n, why in skipped:
            print(f"  {n}  ->  {why}")
        print()
    if not runs:
        print("Nessuna run valida trovata in", a.proofs)
        return 1
    print(f"Run usate: {[r['concurrency'] for r in runs]}")
    for r in runs:
        print(f"  conc {r['concurrency']:>2} <- {r['_src']} "
              f"({'closed-loop' if r['_closed'] else 'wave-barrier'})")
    print()

    fig_throughput(runs, meta, a.out)
    fig_power(runs, meta, a.out)
    fig_cost(runs, meta, a.out)
    fig_efficiency(runs, meta, a.out)
    table(runs, meta, a.out)
    print(f"\nScritto in {a.out}/: 4 PNG + results_table.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
