#!/usr/bin/env python3
"""
costbench.py - misura il costo REALE in euro per milione di token
su una GPU AMD ROCm, campionando il consumo elettrico durante il carico.

Zero dipendenze esterne (solo stdlib). Pensato per girare senza supervisione:
lanci, esci, torni quando ha finito.

USO:
    # 1. PREFLIGHT - 30 secondi, NON carica il modello. Fallo per primo.
    python3 costbench.py --preflight

    # 2. RUN COMPLETO - ~30 min, tutto in un colpo
    python3 costbench.py --model-path ./qwen.gguf --out proofs/

Se il preflight fallisce sui watt, FERMATI: l'angolo del progetto va cambiato.
"""

import argparse
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# TELEMETRIA POTENZA
# --------------------------------------------------------------------------

HWMON_GLOBS = [
    "/sys/class/drm/card*/device/hwmon/hwmon*/power1_average",
    "/sys/class/drm/card*/device/hwmon/hwmon*/power1_input",
]

# Su host multi-GPU condivisi sysfs espone gli hwmon di TUTTE le schede,
# comprese quelle di altri utenti. Prendere il primo path significa
# misurare i watt di qualcun altro. Risolviamo via bus PCI.

_RESOLVED = {"bus": None, "card_dir": None, "resolved": False}


def _target_pci_bus():
    """Bus PCI della GPU assegnata a noi. Override: COSTBENCH_PCI_BUS."""
    env = os.environ.get("COSTBENCH_PCI_BUS")
    if env:
        return env.strip().lower()
    if not shutil.which("rocm-smi"):
        return None
    try:
        r = subprocess.run(["rocm-smi", "--showbus"],
                           capture_output=True, text=True, timeout=20)
        m = re.search(r"PCI Bus:\s*([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:"
                      r"[0-9a-fA-F]{2}\.[0-9a-fA-F])", r.stdout)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return None


def _card_dir_for_bus(bus):
    """Trova la directory /sys/class/drm/cardN/device con quel bus PCI."""
    for uevent in sorted(glob.glob("/sys/class/drm/card*/device/uevent")):
        try:
            with open(uevent) as f:
                txt = f.read()
        except OSError:
            continue
        m = re.search(r"PCI_SLOT_NAME=(\S+)", txt)
        if m and m.group(1).strip().lower() == bus:
            return os.path.dirname(uevent)
    return None


def _resolve_card():
    if _RESOLVED["resolved"]:
        return _RESOLVED
    bus = _target_pci_bus()
    _RESOLVED["bus"] = bus
    _RESOLVED["card_dir"] = _card_dir_for_bus(bus) if bus else None
    _RESOLVED["resolved"] = True
    return _RESOLVED


def _hwmon_paths():
    """Solo gli hwmon della NOSTRA scheda. Fallback a tutte se irrisolvibile."""
    info = _resolve_card()
    if info["card_dir"]:
        out = []
        for pat in ("power1_average", "power1_input"):
            out.extend(sorted(glob.glob(
                os.path.join(info["card_dir"], "hwmon", "hwmon*", pat))))
        if out:
            return out
    out = []
    for g in HWMON_GLOBS:
        out.extend(sorted(glob.glob(g)))
    return out


def read_power_sysfs():
    """Legge i watt da sysfs. Microwatt -> watt. None se non disponibile."""
    for p in _hwmon_paths():
        try:
            with open(p) as f:
                raw = int(f.read().strip())
            if raw > 0:
                return raw / 1e6
        except (OSError, ValueError):
            continue
    return None


def read_power_rocmsmi():
    """Fallback: parsing di rocm-smi. Piu' lento, ~200ms per chiamata."""
    if not shutil.which("rocm-smi"):
        return None
    try:
        r = subprocess.run(
            ["rocm-smi", "--showpower", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
        for _card, fields in data.items():
            for k, v in fields.items():
                if "power" in k.lower():
                    try:
                        return float(str(v).split()[0])
                    except (ValueError, IndexError):
                        continue
    except Exception:
        pass
    # ultimo tentativo: output testuale
    try:
        r = subprocess.run(
            ["rocm-smi", "--showpower"], capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if "W" in line and any(c.isdigit() for c in line):
                for tok in line.replace("(", " ").replace(")", " ").split():
                    try:
                        val = float(tok)
                        if 1.0 < val < 1000.0:
                            return val
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


_POWER_BACKEND = None
_FAKE_POWER = False


def read_power_w():
    """Sceglie il backend una volta sola, poi lo riusa."""
    global _POWER_BACKEND
    if _FAKE_POWER:
        # DRY-RUN: numeri finti per validare la pipeline su hardware non-AMD.
        # I run prodotti in questa modalita' sono marcati measured=False.
        import random
        _POWER_BACKEND = "FAKE (dry-run)"
        return round(random.uniform(180.0, 260.0), 2)
    if _POWER_BACKEND is None:
        if read_power_sysfs() is not None:
            _POWER_BACKEND = "sysfs"
        elif read_power_rocmsmi() is not None:
            _POWER_BACKEND = "rocm-smi"
        else:
            _POWER_BACKEND = "none"
    if _POWER_BACKEND == "sysfs":
        return read_power_sysfs()
    if _POWER_BACKEND == "rocm-smi":
        return read_power_rocmsmi()
    return None


class PowerSampler(threading.Thread):
    """Campiona i watt in background mentre il carico gira."""

    def __init__(self, interval=0.25):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        # NB: non chiamare questo attributo _stop -- collide con
        # threading.Thread._stop() e rompe join().
        self._stopper = threading.Event()

    def run(self):
        while not self._stopper.is_set():
            w = read_power_w()
            if w is not None:
                self.samples.append(w)
            self._stopper.wait(self.interval)

    def stop(self):
        self._stopper.set()
        self.join(timeout=3)
        return self.samples

    def stats(self):
        if not self.samples:
            return {"avg_w": None, "peak_w": None, "n_samples": 0}
        return {
            "avg_w": round(statistics.mean(self.samples), 2),
            "peak_w": round(max(self.samples), 2),
            "min_w": round(min(self.samples), 2),
            "n_samples": len(self.samples),
        }


# --------------------------------------------------------------------------
# AMBIENTE
# --------------------------------------------------------------------------

def capture_env():
    """Snapshot dell'ambiente per proofs/. Ogni numero deve essere tracciabile."""
    env = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "power_backend": _POWER_BACKEND,
        "gpu_pci_bus": _RESOLVED.get("bus"),
        "gpu_sysfs_dir": _RESOLVED.get("card_dir"),
        "hwmon_paths_used": _hwmon_paths(),
    }
    for name, cmd in [
        ("rocm_smi_showallinfo", ["rocm-smi", "--showallinfo"]),
        ("rocminfo_head", ["rocminfo"]),
        ("hipcc_version", ["hipcc", "--version"]),
    ]:
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                env[name] = r.stdout[:4000]
            except Exception as e:
                env[name] = f"ERRORE: {e}"
        else:
            env[name] = "non installato"
    return env


# --------------------------------------------------------------------------
# CLIENT INFERENZA (endpoint OpenAI-compatible: llama-server o vLLM)
# --------------------------------------------------------------------------

PROMPT = (
    "Write a Python function that parses a CSV file and returns "
    "the median of a numeric column. Include error handling and docstrings."
)


def stream_one(base_url, model, prompt, max_tokens, timeout=300):
    """Una richiesta in streaming. Ritorna TTFT, token, wall time."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    ttft = None
    ntok = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or [{}]
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    ntok += 1
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "ttft_s": round(ttft, 4) if ttft else None,
        "tokens": ntok,
        "wall_s": round(time.perf_counter() - t0, 4),
    }


def wait_for_server(base_url, timeout=240):
    """Aspetta che il server sia su. Non sprecare ore GPU su un server morto."""
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/v1/models"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


# --------------------------------------------------------------------------
# SWEEP
# --------------------------------------------------------------------------

def run_concurrency(base_url, model, conc, max_tokens, settle=3.0):
    """Un livello di concorrenza: N richieste in parallelo, watt campionati."""
    time.sleep(settle)  # lascia decadere il carico precedente
    sampler = PowerSampler(interval=0.25)
    sampler.start()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futures = [
            ex.submit(stream_one, base_url, model, PROMPT, max_tokens)
            for _ in range(conc)
        ]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - t0
    sampler.stop()
    power = sampler.stats()

    ok = [r for r in results if r.get("ok")]
    failed = len(results) - len(ok)
    total_tokens = sum(r["tokens"] for r in ok)
    ttfts = [r["ttft_s"] for r in ok if r["ttft_s"] is not None]
    per_req = [
        r["tokens"] / r["wall_s"] for r in ok if r["wall_s"] and r["tokens"]
    ]

    return {
        "concurrency": conc,
        "requests_ok": len(ok),
        "requests_failed": failed,
        "wall_s": round(wall, 3),
        "total_tokens": total_tokens,
        "system_tok_s": round(total_tokens / wall, 2) if wall else 0,
        "per_request_tok_s_median": (
            round(statistics.median(per_req), 2) if per_req else None
        ),
        "ttft_s_median": round(statistics.median(ttfts), 4) if ttfts else None,
        "ttft_s_p95": (
            round(sorted(ttfts)[int(len(ttfts) * 0.95) - 1], 4)
            if len(ttfts) >= 2 else (round(ttfts[0], 4) if ttfts else None)
        ),
        "power": power,
    }


def add_cost(run, eur_per_kwh, idle_w):
    """
    Costo energetico per milione di token.

    NOTA ONESTA: e' potenza SCHEDA (board power), non sistema completo.
    Niente CPU, RAM, raffreddamento, PUE del datacenter. Il costo reale
    di esercizio e' piu' alto. Dichiaralo nel README.
    """
    p = run.get("power") or {}
    avg_w = p.get("avg_w")
    tok = run.get("total_tokens") or 0
    if not avg_w or not tok:
        run["cost"] = {"note": "dati insufficienti"}
        return run

    def _cost(watt):
        wh = watt * run["wall_s"] / 3600.0
        return (wh / 1000.0) * eur_per_kwh / tok * 1e6

    run["cost"] = {
        "eur_per_kwh_assumed": eur_per_kwh,
        "gpu_board_power_only": True,
        "eur_per_1m_tokens_gross": round(_cost(avg_w), 4),
        "eur_per_1m_tokens_net_of_idle": (
            round(_cost(max(avg_w - idle_w, 0.1)), 4) if idle_w else None
        ),
        "idle_w_subtracted": idle_w,
    }
    return run


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def preflight():
    print("=" * 62)
    print("PREFLIGHT - nessun modello caricato, nessuna ora GPU sprecata")
    print("=" * 62)

    w = read_power_w()
    print(f"\n[1] Backend potenza : {_POWER_BACKEND}")
    print(f"    Lettura watt    : {w}")
    if w is None:
        print("\n    >>> FALLITO. La telemetria di potenza non e' esposta.")
        print("    >>> Prova un altro template, o cambia angolo al progetto.")
    else:
        print("    >>> OK. Il progetto regge.")

    print(f"\n[2] Path hwmon trovati:")
    info = _resolve_card()
    print(f"    Bus PCI GPU assegnata : {info['bus'] or 'NON RISOLTO'}")
    print(f"    Directory scheda      : {info['card_dir'] or 'NON RISOLTA'}")
    if not info["card_dir"]:
        print("    !!! ATTENZIONE: host multi-GPU, scheda non identificata.")
        print("    !!! Rischio di misurare la GPU di un altro utente.")
        print("    !!! Imposta COSTBENCH_PCI_BUS=0000:xx:00.0 a mano.")
    paths = _hwmon_paths()
    for p in paths or ["  (nessuno)"]:
        print(f"    {p}")

    print(f"\n[3] Binari:")
    for b in ["rocm-smi", "rocminfo", "hipcc", "llama-server", "llama-cli", "vllm"]:
        print(f"    {b:<14} {shutil.which(b) or 'ASSENTE'}")

    if shutil.which("rocm-smi"):
        print(f"\n[4] rocm-smi:")
        try:
            r = subprocess.run(
                ["rocm-smi", "--showproductname", "--showmeminfo", "vram"],
                capture_output=True, text=True, timeout=20,
            )
            print("    " + r.stdout.replace("\n", "\n    "))
        except Exception as e:
            print(f"    errore: {e}")

    if w is not None:
        print("\n[5] Stabilita' lettura (5 campioni a 0.5s):")
        for _ in range(5):
            print(f"    {read_power_w()} W")
            time.sleep(0.5)

    print("\n" + "=" * 62)
    print("Incolla TUTTO questo output nella chat.")
    print("=" * 62)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true",
                    help="solo diagnostica, non tocca il modello")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="local-model",
                    help="nome modello passato all'API")
    ap.add_argument("--label", default="run",
                    help="etichetta run, es. qwen7b-q4km")
    ap.add_argument("--concurrency", default="1,2,4,8",
                    help="livelli di concorrenza separati da virgola")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--eur-per-kwh", type=float, default=0.25,
                    help="tariffa elettrica; default indicativo Italia")
    ap.add_argument("--out", default="proofs")
    ap.add_argument("--fake-power", action="store_true",
                    help="DRY-RUN locale: watt simulati, output NON valido "
                         "come prova. Serve solo a validare la pipeline.")
    args = ap.parse_args()

    global _FAKE_POWER
    _FAKE_POWER = args.fake_power
    if _FAKE_POWER:
        print("!" * 62)
        print("DRY-RUN: watt SIMULATI. Output marcato measured=False.")
        print("Non usare questi numeri nella submission.")
        print("!" * 62)

    read_power_w()  # inizializza il backend

    if args.preflight:
        preflight()
        return 0

    if read_power_w() is None:
        print("ERRORE: nessuna telemetria di potenza. Lancia --preflight.")
        return 2

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Attendo il server su {args.base_url} ...")
    if not wait_for_server(args.base_url):
        print("ERRORE: server non raggiungibile. Avvialo prima di questo script.")
        return 3
    print("[*] Server pronto.")

    # baseline idle: senza questo i numeri a bassa concorrenza sono gonfiati
    print("[*] Baseline idle (20s, non toccare nulla)...")
    idle_sampler = PowerSampler(interval=0.25)
    idle_sampler.start()
    time.sleep(20)
    idle_sampler.stop()
    idle_stats = idle_sampler.stats()
    idle_w = idle_stats.get("avg_w") or 0.0
    print(f"    idle medio: {idle_w} W")

    # warmup: la prima richiesta paga il caricamento dei layer, va scartata
    print("[*] Warmup...")
    stream_one(args.base_url, args.model, PROMPT, 64)

    runs = []
    for conc in [int(c) for c in args.concurrency.split(",")]:
        print(f"[*] Concorrenza {conc} ...", flush=True)
        r = run_concurrency(args.base_url, args.model, conc, args.max_tokens)
        r = add_cost(r, args.eur_per_kwh, idle_w)
        runs.append(r)
        p = r.get("power") or {}
        c = r.get("cost") or {}
        print(f"    sistema {r['system_tok_s']} tok/s | "
              f"{p.get('avg_w')} W | "
              f"EUR/1M tok {c.get('eur_per_1m_tokens_gross')}")

    payload = {
        "label": args.label,
        "measured": not _FAKE_POWER,
        "dry_run": _FAKE_POWER,
        "environment": capture_env(),
        "idle_baseline": idle_stats,
        "config": {
            "max_tokens": args.max_tokens,
            "eur_per_kwh": args.eur_per_kwh,
            "prompt": PROMPT,
            "token_counting": "chunk di streaming (approssima 1 chunk = 1 token)",
        },
        "runs": runs,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = outdir / f"costbench_{args.label}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2))
    print(f"\n[*] Scritto: {path}")

    if runs and runs[0].get("system_tok_s"):
        base = runs[0]["system_tok_s"]
        best = max(r["system_tok_s"] for r in runs)
        print(f"[*] Scaling batching: {round(best / base, 2)}x")
        c0 = (runs[0].get("cost") or {}).get("eur_per_1m_tokens_gross")
        cn = (runs[-1].get("cost") or {}).get("eur_per_1m_tokens_gross")
        if c0 and cn:
            print(f"[*] Costo per token: {c0} -> {cn} EUR/1M "
                  f"({round((1 - cn / c0) * 100, 1)}% in meno)")
    print("\n>>> git add proofs/ && git commit SUBITO. L'istanza puo' morire.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
