#!/usr/bin/env bash
#
# memory_ab.sh - quanto risparmia la memoria, in joule.
#
# Stessa sequenza di task eseguita due volte:
#   A  memoria fredda (cancellata prima di iniziare)
#   B  memoria calda  (quella lasciata dal giro A)
#
# La differenza in token e' energia non rispesa, e su questo hardware
# sappiamo quanta: 4.142 tok/J misurati a concorrenza 16.
#
# USO (sull'istanza, con llama-server e gateway gia' attivi):
#     bash memory_ab.sh 2>&1 | tee proofs/memory_ab.log

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOFS="$ROOT/proofs"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
GW="${GW:-http://127.0.0.1:8090}"
MEMDB="/tmp/agent_memory_$STAMP.sqlite"

mkdir -p "$PROOFS"
log() { echo -e "\n\033[1;36m[$(date -u +%H:%M:%S)] $*\033[0m"; }

curl -sf --max-time 5 "$GW/healthz" >/dev/null || {
    echo "ABORT: gateway non raggiungibile su $GW"; exit 1; }

# I task si sovrappongono deliberatamente: il secondo e il terzo possono
# rispondere in parte con quello che il primo ha gia' stabilito.
TASKS=(
  "In proofs/, find which concurrency gives the best tokens-per-joule ratio. Cite the exact filename."
  "Which concurrency gives the best tokens-per-joule ratio, and what throughput was measured there?"
  "What is the best tokens-per-joule value measured, and in which file is it recorded?"
)

run_round() {   # $1 = etichetta, $2 = usare la memoria (0/1)
    local label="$1" use_mem="$2" i=0
    local memarg=""
    [ "$use_mem" = "1" ] && memarg="--memory $MEMDB"
    for t in "${TASKS[@]}"; do
        i=$((i+1))
        log "$label · task $i/${#TASKS[@]}"
        timeout 300 python3 agent.py \
            --root "$ROOT" --gateway "$GW" --max-steps 6 \
            --tenant "memtest-$label" --privacy strict $memarg \
            --json-out "$PROOFS/memab_${label}_${i}_$STAMP.json" \
            --task "$t" 2>&1 | grep -E "step |ANSWER|tokens generated|energy|memoria|memory" || true
    done
}

rm -f "$MEMDB"

log "=== ROUND A · memoria fredda"
run_round "cold" 0

log "=== popolo la memoria con il giro A"
rm -f "$MEMDB"
i=0
for t in "${TASKS[@]}"; do
    i=$((i+1))
    timeout 300 python3 agent.py --root "$ROOT" --gateway "$GW" \
        --max-steps 6 --tenant "memtest-warmup" --privacy strict \
        --memory "$MEMDB" --task "$t" > /dev/null 2>&1 || true
done
python3 memory.py "$MEMDB" list

log "=== ROUND B · memoria calda"
run_round "warm" 1

log "=== CONFRONTO"
python3 - "$PROOFS" "$STAMP" "$MEMDB" <<'PY'
import glob, json, sys
proofs, stamp, memdb = sys.argv[1], sys.argv[2], sys.argv[3]

def total(label):
    tok = steps = 0
    files = sorted(glob.glob(f"{proofs}/memab_{label}_*_{stamp}.json"))
    if len(files) != 3:
        print(f"ATTENZIONE: {len(files)} file per {label}, attesi 3")
    for f in files:
        d = json.load(open(f))
        acc = d.get("accounting") or {}
        tok += acc.get("tokens", 0)
        steps += d.get("steps", 0)
    return tok, steps

TPJ = 4.142          # misurato, W7900 7B conc 16
EUR = 0.25

ct, cs = total("cold")
wt, ws = total("warm")
cj, wj = ct / TPJ, wt / TPJ

print(f"\n{'':<22}{'cold':>10}{'warm':>10}{'delta':>12}")
print("-" * 54)
print(f"{'steps':<22}{cs:>10}{ws:>10}{ws-cs:>12}")
print(f"{'tokens':<22}{ct:>10}{wt:>10}{wt-ct:>12}")
print(f"{'joules':<22}{cj:>10.1f}{wj:>10.1f}{wj-cj:>12.1f}")
print(f"{'EUR':<22}{cj/3.6e6*EUR:>10.8f}{wj/3.6e6*EUR:>10.8f}"
      f"{(wj-cj)/3.6e6*EUR:>12.8f}")
if ct:
    print(f"\ntoken risparmiati: {(1-wt/ct)*100:.1f}%")
    print(f"energia risparmiata: {cj-wj:.1f} J "
          f"(EUR {(cj-wj)/3.6e6*EUR:.8f})")

out = {
    "measured": True,
    "experiment": "agent memory A/B",
    "basis_tokens_per_joule": TPJ,
    "basis_source": "costbench_qwen7b-q4km-v3-closedloop_20260729T222700Z.json",
    "eur_per_kwh": EUR,
    "cold": {"steps": cs, "tokens": ct, "joules": round(cj, 2),
             "eur": round(cj/3.6e6*EUR, 10)},
    "warm": {"steps": ws, "tokens": wt, "joules": round(wj, 2),
             "eur": round(wj/3.6e6*EUR, 10)},
    "tokens_saved_pct": round((1-wt/ct)*100, 1) if ct else None,
    "joules_saved": round(cj-wj, 2),
    "note": "Energy is derived from the measured tokens-per-joule curve, "
            "not measured directly during this experiment: the workload is "
            "too short for the power sensor's ~18 s averaging window.",
}
p = f"{proofs}/memory_ab_{stamp}.json"
json.dump(out, open(p, "w"), indent=2)
print(f"\nscritto: {p}")
PY

log "stato finale della memoria"
python3 memory.py "$MEMDB" stats
cp "$MEMDB" "$PROOFS/memory_$STAMP.sqlite" 2>/dev/null || true
