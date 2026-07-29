#!/usr/bin/env bash
#
# clockprobe.sh - perche' la potenza SCENDE a concorrenza alta?
#
# Ipotesi da verificare: a batch 1 il decode e' memory-bandwidth-bound
# (ogni token rilegge tutti i pesi dalla GDDR6). A batch 16 una lettura
# dei pesi serve 16 sequenze, il traffico per token crolla, il carico si
# sposta sul compute e il sottosistema di memoria assorbe meno.
#
# Se l'ipotesi regge: a conc-1 mclk alto / sclk basso / use% medio-basso,
# a conc-16 sclk piu' alto e use% alto.
#
# USO (sull'istanza, server llama.cpp gia' avviato sulla 8081):
#     bash clockprobe.sh
#
# Output: proofs/clockprobe_<stamp>.csv

set -uo pipefail

PORT="${PORT:-8081}"
CARD="${CARD:-/sys/class/drm/card3/device/hwmon/hwmon8/power1_average}"
LEVELS="${LEVELS:-1 4 16}"
SAMPLE_S=2
DURATION_S="${DURATION_S:-70}"     # > rampa del sensore (~18s) + margine
SETTLE_S=25                        # decadimento tra i livelli

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$ROOT/proofs/clockprobe_$STAMP.csv"
mkdir -p "$ROOT/proofs"

PROMPT='Write an extremely detailed technical essay on distributed consensus algorithms, covering Paxos, Raft, and PBFT with full pseudocode.'

log() { echo -e "\n\033[1;36m[$(date -u +%H:%M:%S)] $*\033[0m"; }

[ -r "$CARD" ] || { echo "ABORT: sensore non leggibile: $CARD"; exit 1; }
curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || { echo "ABORT: nessun server sulla porta $PORT"; exit 1; }

echo "phase,concurrency,t_rel_s,watt,gpu_use_pct,mclk_mhz,sclk_mhz,fclk_mhz,socclk_mhz" > "$OUT"

sample_once() {
    local phase="$1" conc="$2" t="$3"
    local w use clocks mclk sclk fclk socclk
    w=$(( $(cat "$CARD" 2>/dev/null || echo 0) / 1000000 ))
    clocks=$(rocm-smi --showclocks --showuse 2>/dev/null)
    use=$(echo "$clocks"  | grep -oP 'GPU use \(%\): \K[0-9]+'                  | head -1)
    mclk=$(echo "$clocks" | grep -oP 'mclk clock level: [0-9]+: \(\K[0-9]+'     | head -1)
    sclk=$(echo "$clocks" | grep -oP 'sclk clock level: [0-9]+: \(\K[0-9]+'     | head -1)
    fclk=$(echo "$clocks" | grep -oP 'fclk clock level: [0-9]+: \(\K[0-9]+'     | head -1)
    socclk=$(echo "$clocks" | grep -oP 'socclk clock level: [0-9]+: \(\K[0-9]+' | head -1)
    echo "$phase,$conc,$t,$w,${use:-},${mclk:-},${sclk:-},${fclk:-},${socclk:-}" >> "$OUT"
    printf "  %5ss  %4sW  use=%3s%%  mclk=%5s  sclk=%5s\n" \
        "$t" "$w" "${use:-?}" "${mclk:-?}" "${sclk:-?}"
}

fire() {   # tiene occupati $1 slot per $2 secondi
    local conc="$1"
    local dur="$2"
    # NB: assegnazioni separate. In "local a=$1 b=$((a+1))" le espansioni
    # avvengono PRIMA che local assegni a -> unbound variable con set -u.
    local end
    end=$(( $(date +%s) + dur ))
    while [ "$(date +%s)" -lt "$end" ]; do
        local i
        for i in $(seq 1 "$conc"); do
            curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
                -H 'Content-Type: application/json' \
                -d "{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":512}" \
                > /dev/null 2>&1 &
        done
        wait
    done
}

log "BASELINE idle (30s)"
for i in $(seq 0 $((30 / SAMPLE_S))); do
    sample_once idle 0 $((i * SAMPLE_S)); sleep $SAMPLE_S
done

for conc in $LEVELS; do
    log "SETTLE ${SETTLE_S}s"
    sleep "$SETTLE_S"

    log "CARICO concorrenza $conc per ${DURATION_S}s"
    fire "$conc" "$DURATION_S" &
    LOAD_PID=$!

    t=0
    while kill -0 "$LOAD_PID" 2>/dev/null && [ "$t" -lt "$DURATION_S" ]; do
        sample_once load "$conc" "$t"
        sleep "$SAMPLE_S"; t=$((t + SAMPLE_S))
    done
    wait "$LOAD_PID" 2>/dev/null || true
    pkill -f 'curl -s http://127.0.0.1' 2>/dev/null || true
done

log "FATTO: $OUT"
echo
echo "=== MEDIE (solo campioni oltre 25s, rampa del sensore esclusa) ==="
awk -F, 'NR>1 && $1=="load" && $3>25 {
    n[$2]++; w[$2]+=$4; u[$2]+=$5; m[$2]+=$6; s[$2]+=$7
} END {
    printf "%-6s %8s %8s %9s %9s\n","conc","watt","use%","mclk","sclk"
    for (c in n) printf "%-6s %8.1f %8.1f %9.0f %9.0f\n", c, w[c]/n[c], u[c]/n[c], m[c]/n[c], s[c]/n[c]
}' "$OUT" | sort -n
echo
echo ">>> Poi: base64 -w0 $OUT   e incolla l'output in chat."
