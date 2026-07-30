#!/usr/bin/env python3
"""
charts.py - figure della submission dai proofs/ misurati.

v2: raggruppa per MODELLO. La v1 univa le run per livello di concorrenza
e avrebbe mescolato 7B e 14B nella stessa curva.

USO:
    pip install matplotlib --break-system-packages
    python3 charts.py --proofs proofs/ --out charts/
"""

import argparse
import glob
import json
import os
import re
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

TEAL, TEAL_L, GOLD, INK, GREY, GRID = (
    "#0d7a7a", "#4fb3b3", "#c8992e", "#1a1a1a", "#8a8a8a", "#e4e4e4")

plt.rcParams.update({
    "figure.dpi": 160, "savefig.dpi": 160, "font.family": "DejaVu Sans",
    "font.size": 10, "axes.edgecolor": GREY, "axes.labelcolor": INK,
    "axes.titlesize": 13, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
    "text.color": INK, "xtick.color": INK, "ytick.color": INK,
    "grid.color": GRID, "grid.linewidth": 0.8,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

MODEL_PATTERNS = [
    (re.compile(r"qwen.*?14b", re.I), "Qwen2.5-Coder-14B Q4_K_M", 14),
    (re.compile(r"qwen.*?7b", re.I),  "Qwen2.5-Coder-7B Q4_K_M", 7),
    (re.compile(r"qwen.*?3b", re.I),  "Qwen2.5-Coder-3B Q4_K_M", 3),
]


def model_of(label):
    for rx, name, size in MODEL_PATTERNS:
        if rx.search(label or ""):
            return name, size
    return (label or "unknown"), 0


def load_runs(proofs_dir):
    """Run valide raggruppate per modello. Closed-loop batte wave-barrier.

    Scarta quelle prodotte prima delle correzioni:
      - senza gpu_pci_bus: su host multi-GPU leggeva la scheda di un
        altro utente (~14 W, impossibili sotto carico)
      - senza duration_s_target: ondate da 2-5s, sotto la finestra di
        media del sensore, potenza non rilevata
    """
    best = defaultdict(dict)
    meta, skipped = {}, []
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

        model, size = model_of(d.get("label"))
        for r in d.get("runs", []):
            conc = r.get("concurrency")
            if conc is None or not (r.get("power") or {}).get("avg_w"):
                continue
            if not r.get("duration_s_target"):
                skipped.append((f"{name} conc={conc}",
                                "run troppo corta (pre-fix)"))
                continue
            closed = r.get("load_pattern") == "closed-loop"
            prev = best[model].get(conc)
            if prev is None or (closed and not prev["_closed"]):
                r = dict(r)
                r.update(_closed=closed, _src=name, _model=model, _size=size)
                best[model][conc] = r
                meta.setdefault("gpu_pci_bus", env.get("gpu_pci_bus"))
                meta.setdefault("eur_per_kwh",
                                d.get("config", {}).get("eur_per_kwh"))
    models = {m: [best[m][c] for c in sorted(best[m])] for m in best}
    return models, meta, skipped


def note(fig, meta, subtitle):
    fig.text(0.5, 0.005,
             f"Measured on AMD Radeon PRO W7900 (gfx1100, "
             f"{meta.get('gpu_pci_bus')})  ·  {subtitle}  ·  "
             f"llama.cpp ROCm, closed-loop load  ·  power: GPU board only, "
             f"sysfs power1_average  ·  tariff {meta.get('eur_per_kwh')} EUR/kWh",
             ha="center", fontsize=6.5, color=GREY)


def fig_throughput(runs, meta, out, tag):
    c = [r["concurrency"] for r in runs]
    t = [r["system_tok_s"] for r in runs]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(c, [t[0] * (x / c[0]) for x in c], "--", color=GREY, lw=1.2,
            label="linear scaling (ideal)")
    ax.plot(c, t, "o-", color=TEAL, lw=2.4, ms=7, label="measured")
    for x, y in zip(c, t):
        ax.annotate(f"{y:,.0f}", (x, y), textcoords="offset points",
                    xytext=(0, 11), ha="center", fontsize=9,
                    color=TEAL, fontweight="bold")
    ax.set_xscale("log", base=2)
    ax.set_xticks(c)
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("System throughput (tokens/s)")
    ax.set_title(f"Continuous batching scales throughput {t[-1]/t[0]:.1f}×")
    ax.grid(axis="y")
    ax.legend(frameon=False, fontsize=9)
    note(fig, meta, runs[0]["_model"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, f"01_throughput_{tag}.png"))
    plt.close(fig)


def fig_power(runs, meta, out, tag):
    c = [r["concurrency"] for r in runs]
    w = [r["power"]["avg_w"] for r in runs]
    pk = [r["power"]["peak_w"] for r in runs]
    x = list(range(len(c)))
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.fill_between(x, w, pk, color=TEAL_L, alpha=0.18, label="avg → peak")
    ax.plot(x, w, "o-", color=TEAL, lw=2.4, ms=7, label="average")
    ax.plot(x, pk, "s--", color=GOLD, lw=1.4, ms=5, label="peak")
    hi = max(range(len(w)), key=lambda i: w[i])
    ax.annotate(f"peak draw at concurrency {c[hi]}\n{w[hi]:.0f} W",
                (hi, w[hi]), textcoords="offset points", xytext=(18, -30),
                fontsize=9,
                arrowprops=dict(arrowstyle="->", color=GREY, lw=1))
    for i, y in enumerate(w):
        ax.annotate(f"{y:.0f} W", (i, y), textcoords="offset points",
                    xytext=(0, 12), ha="center", fontsize=9,
                    color=TEAL, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("GPU board power (W)")
    ax.set_title("Power draw is not monotonic in batch size")
    ax.grid(axis="y")
    ax.legend(frameon=False, fontsize=9, loc="lower left")
    note(fig, meta, runs[0]["_model"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, f"02_power_{tag}.png"))
    plt.close(fig)


def fig_cost(runs, meta, out, tag):
    c = [r["concurrency"] for r in runs]
    cost = [r["cost"]["eur_per_1m_tokens_gross"] for r in runs]
    ttft = [(r.get("ttft_s_median") or 0) * 1000 for r in runs]
    x = list(range(len(c)))
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    bars = ax.bar(x, cost, width=0.55, color=TEAL, zorder=3)
    for b, v in zip(bars, cost):
        ax.annotate(f"€{v:.4f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 5), ha="center",
                    fontsize=9, color=TEAL, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("Electricity cost (EUR / 1M tokens)", color=TEAL)
    ax.tick_params(axis="y", labelcolor=TEAL)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"€{v:.3f}"))
    ax.grid(axis="y", zorder=0)
    ax.set_ylim(0, max(cost) * 1.25)

    ax2 = ax.twinx()
    ax2.plot(x, ttft, "o--", color=GOLD, lw=2, ms=6)
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
    note(fig, meta, runs[0]["_model"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, f"03_cost_vs_latency_{tag}.png"))
    plt.close(fig)


def fig_efficiency(runs, meta, out, tag):
    c = [r["concurrency"] for r in runs]
    tpj = [r["system_tok_s"] / r["power"]["avg_w"] for r in runs]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    bars = ax.bar(range(len(c)), tpj, width=0.55, color=GOLD, zorder=3)
    for b, v in zip(bars, tpj):
        ax.annotate(f"{v:.2f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 5), ha="center",
                    fontsize=9, color="#8a6a1a", fontweight="bold")
    ax.set_xticks(range(len(c)))
    ax.set_xticklabels(c)
    ax.set_xlabel("Concurrent requests")
    ax.set_ylabel("Tokens per joule")
    ax.set_title(f"Energy efficiency improves {tpj[-1]/tpj[0]:.1f}× "
                 f"with batching")
    ax.grid(axis="y", zorder=0)
    ax.set_ylim(0, max(tpj) * 1.2)
    note(fig, meta, runs[0]["_model"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(out, f"04_tokens_per_joule_{tag}.png"))
    plt.close(fig)


def fig_compare(models, meta, out):
    """Due leggi separate, entrambe misurate: l'efficienza assoluta scala
    con la dimensione del modello, il guadagno relativo dal batching no."""
    ordered = sorted(models.items(), key=lambda kv: -kv[1][0]["_size"])
    if len(ordered) < 2:
        return None
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.4))
    for (name, runs), col in zip(ordered, [TEAL, GOLD, GREY]):
        c = [r["concurrency"] for r in runs]
        tpj = [r["system_tok_s"] / r["power"]["avg_w"] for r in runs]
        x = list(range(len(c)))
        ax1.plot(x, tpj, "o-", color=col, lw=2.4, ms=7, label=name)
        ax2.plot(x, [v / tpj[0] for v in tpj], "o-", color=col, lw=2.4, ms=7,
                 label=f"{name}  ({tpj[-1]/tpj[0]:.1f}×)")
        for ax in (ax1, ax2):
            ax.set_xticks(x)
            ax.set_xticklabels(c)
            ax.set_xlabel("Concurrent requests")
            ax.grid(axis="y")
    ax1.set_ylabel("Tokens per joule")
    ax1.set_title("Absolute efficiency scales with model size")
    ax1.legend(frameon=False, fontsize=8.5)
    ax2.set_ylabel("Efficiency gain (× vs concurrency 1)")
    ax2.set_title("Relative gain is model-independent")
    ax2.legend(frameon=False, fontsize=8.5)
    note(fig, meta, " vs ".join(n for n, _ in ordered))
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    p = os.path.join(out, "05_model_comparison.png")
    fig.savefig(p)
    plt.close(fig)
    return p


def table(models, meta, out):
    lines = ["| Model | Concurrency | Achieved | Throughput (tok/s) | "
             "Power (W) | Tokens/J | TTFT p50 (ms) | EUR/1M tokens |",
             "|:--|---:|---:|---:|---:|---:|---:|---:|"]
    for name, runs in sorted(models.items(), key=lambda kv: -kv[1][0]["_size"]):
        for r in runs:
            p, c_ = r["power"], r["cost"]
            lines.append(
                f"| {name} | {r['concurrency']} "
                f"| {r.get('achieved_concurrency') or '—'} "
                f"| {r['system_tok_s']:,.1f} | {p['avg_w']:.1f} "
                f"| {r['system_tok_s']/p['avg_w']:.3f} "
                f"| {(r.get('ttft_s_median') or 0)*1000:.0f} "
                f"| {c_['eur_per_1m_tokens_gross']:.4f} |")
    lines += ["", "All values `measured`, not modeled. Closed-loop load with "
              "achieved concurrency verified per run. GPU board power only "
              f"(sysfs `power1_average`, bus {meta.get('gpu_pci_bus')}); "
              "excludes CPU, RAM, cooling and datacenter PUE. "
              f"Tariff {meta.get('eur_per_kwh')} EUR/kWh."]
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

    models, meta, skipped = load_runs(a.proofs)
    if skipped:
        print("Scartate:")
        for n, why in skipped:
            print(f"  {n}  ->  {why}")
        print()
    if not models:
        print("Nessuna run valida in", a.proofs)
        return 1

    for name, runs in sorted(models.items(), key=lambda kv: -kv[1][0]["_size"]):
        size = runs[0]["_size"]
        tag = f"{size}b" if size else "model"
        print(f"{name}: concorrenze {[r['concurrency'] for r in runs]}")
        fig_throughput(runs, meta, a.out, tag)
        fig_power(runs, meta, a.out, tag)
        fig_cost(runs, meta, a.out, tag)
        fig_efficiency(runs, meta, a.out, tag)

    p = fig_compare(models, meta, a.out)
    print(f"\nconfronto modelli -> {p}" if p else
          "\n(un solo modello: nessun confronto)")
    print()
    table(models, meta, a.out)
    print(f"\nScritto in {a.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
