#!/usr/bin/env bash
#
# demo.sh - sessione GPU unica che produce tutto il materiale mancante.
#
# Ordine per VALORE, non per comodita': se la sessione muore a meta',
# quello che serve davvero e' gia' scritto su disco.
#
#   FASE A  agente sul 7B misurato        <- essenziale
#   FASE B  registrazione per il video    <- essenziale
#   FASE C  secondo modello (14B)         <- opzionale, generalizza il metodo
#
# USO (sull'istanza Radeon):
#     cd /workspace/template-repos/template-890/repo
#     git pull && bash demo.sh 2>&1 | tee proofs/demo_session.log
#
# Salta la fase C:  SKIP_SECOND_MODEL=1 bash demo.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOFS="$ROOT/proofs"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MODEL_7B="$ROOT/models/qwen2.5-coder-7b-instruct-q4_k_m.gguf"
SERVER="$ROOT/llama.cpp/build/bin/llama-server"
LLAMA_PORT=8081
GW_PORT=8090
SKIP_SECOND_MODEL="${SKIP_SECOND_MODEL:-0}"
SRV_PID=""; GW_PID=""

mkdir -p "$PROOFS"
log()  { echo -e "\n\033[1;36m[$(date -u +%H:%M:%S)] $*\033[0m"; }
fail() { echo -e "\n\033[1;31m>>> ABORT: $*\033[0m"; cleanup; exit 1; }

cleanup() {
    for p in "$GW_PID" "$SRV_PID"; do
        [ -n "$p" ] && kill "$p" 2>/dev/null && wait "$p" 2>/dev/null
    done
    return 0
}
trap cleanup EXIT INT TERM

start_llama() {   # $1 = path modello, $2 = etichetta, $3 = slot paralleli
    local model="$1" tag="$2" slots="${3:-16}"
    local want; want=$(basename "$model")

    # 1. LIBERA LA PORTA. Un server zombie che risponde ancora fa passare
    #    il controllo di prontezza e ti fa misurare il modello sbagliato.
    [ -n "$SRV_PID" ] && { kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; }
    pkill -f "llama-server.*--port $LLAMA_PORT" 2>/dev/null
    SRV_PID=""
    for _ in $(seq 1 30); do
        curl -sf --max-time 2 "http://127.0.0.1:$LLAMA_PORT/v1/models" >/dev/null 2>&1 || break
        sleep 2
    done
    curl -sf --max-time 2 "http://127.0.0.1:$LLAMA_PORT/v1/models" >/dev/null 2>&1 \
        && fail "porta $LLAMA_PORT ancora occupata da un altro processo"
    sleep 5

    # 2. llama.cpp DIVIDE -c tra gli slot: con -c 8192 -np 16 ogni richiesta
    #    ha 512 token e l'agente viene troncato a meta' JSON.
    local ctx=$(( 8192 * slots ))
    log "avvio llama-server: $tag ($want, slot=$slots, $((ctx/slots)) tok/slot)"
    "$SERVER" -m "$model" --host 127.0.0.1 --port "$LLAMA_PORT" \
        -ngl 99 -c "$ctx" -np "$slots" -cb > "$PROOFS/srv_${tag}_$STAMP.log" 2>&1 &
    SRV_PID=$!

    for _ in $(seq 1 60); do
        curl -sf --max-time 3 "http://127.0.0.1:$LLAMA_PORT/v1/models" >/dev/null 2>&1 && break
        kill -0 "$SRV_PID" 2>/dev/null || fail "server morto: proofs/srv_${tag}_$STAMP.log"
        sleep 5
    done
    kill -0 "$SRV_PID" 2>/dev/null || fail "server morto: proofs/srv_${tag}_$STAMP.log"

    # 3. VERIFICA CHE SIA IL MODELLO GIUSTO, non solo che qualcuno risponda.
    local served; served=$(curl -sf --max-time 5 \
        "http://127.0.0.1:$LLAMA_PORT/v1/models" 2>/dev/null | tr -d ' \n')
    case "$served" in
        *"$want"*) log "  modello confermato: $want" ;;
        *) fail "il server sulla porta $LLAMA_PORT NON serve $want.
   Risposta /v1/models: ${served:0:300}
   Misurare cosi' produce dati falsi. Interrompo." ;;
    esac
}

# ---------------------------------------------------------------- preflight
log "PREFLIGHT"
[ -x "$SERVER" ] || fail "llama-server assente. Lancia prima setup.sh"
[ -f "$MODEL_7B" ] || fail "modello 7B assente: $MODEL_7B"
python3 -c "import fastapi, uvicorn, httpx" 2>/dev/null || {
    log "installo dipendenze del gateway"
    pip install -q fastapi uvicorn httpx --break-system-packages 2>/dev/null || \
    pip install -q fastapi uvicorn httpx || fail "pip fallito"
}
python3 costbench.py --preflight 2>&1 | grep -E "Backend potenza|Bus PCI|Directory scheda" \
    || fail "telemetria non disponibile"

# ============================================================== FASE A
log "FASE A - agente sul 7B MISURATO (coerente col cost model)"
start_llama "$MODEL_7B" "7b" 4

python3 gateway.py --upstream "http://127.0.0.1:$LLAMA_PORT" \
    --port "$GW_PORT" --db "$PROOFS/audit_$STAMP.sqlite" \
    > "$PROOFS/gateway_$STAMP.log" 2>&1 &
GW_PID=$!
for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$GW_PORT/healthz" >/dev/null 2>&1 && break
    sleep 2
done
curl -sf "http://127.0.0.1:$GW_PORT/healthz" >/dev/null || fail "gateway non parte"
curl -s "http://127.0.0.1:$GW_PORT/healthz" | python3 -m json.tool

TASKS=(
  "In the proofs/ directory, find the measured throughput and power for each concurrency level, then say which concurrency gives the best tokens-per-joule ratio. Cite the exact filename you used."
  "Which GPU was used for the measurements, and what is its Max Graphics Package Power? Cite the exact filename."
  "Compare eur_per_1m_tokens_gross between concurrency 1 and concurrency 16. Cite the exact filename."
)

i=0
for t in "${TASKS[@]}"; do
    i=$((i+1))
    log "task $i/${#TASKS[@]}"
    timeout 600 python3 agent.py \
        --root "$ROOT" --gateway "http://127.0.0.1:$GW_PORT" \
        --max-steps 6 --tenant "demo" --privacy strict \
        --json-out "$PROOFS/agent_trace_${i}_$STAMP.json" \
        --task "$t" 2>&1 | tee "$PROOFS/agent_run_${i}_$STAMP.txt"
done

log "usage aggregato del tenant demo"
curl -s "http://127.0.0.1:$GW_PORT/v1/usage?tenant=demo" \
    | tee "$PROOFS/usage_$STAMP.json" | python3 -m json.tool

log "export audit (prova che l'egress e' zero, non dichiarato)"
python3 - "$PROOFS/audit_$STAMP.sqlite" "$PROOFS/audit_export_$STAMP.json" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
rows = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY ts_utc")]
ext = sum(1 for r in rows if r["egress"] == "external")
json.dump({"records": rows, "total": len(rows),
           "external_egress": ext}, open(sys.argv[2], "w"), indent=2)
print(f"  {len(rows)} record, {ext} con egress esterno")
PY

# ============================================================== FASE B
log "FASE B - registrazione per il video (una passata pulita)"
{
  echo "### on-prem agent, AMD Radeon PRO W7900, zero egress"
  echo "### modello: Qwen2.5-Coder-7B Q4_K_M | costo: curva token/joule misurata"
  echo
  timeout 600 python3 agent.py \
      --root "$ROOT" --gateway "http://127.0.0.1:$GW_PORT" \
      --max-steps 6 --tenant "video" --privacy strict \
      --task "In proofs/, find which concurrency gives the best tokens-per-joule ratio. Cite the exact filename."
} 2>&1 | tee "$PROOFS/video_take_$STAMP.txt"

# ============================================================== FASE C
if [ "$SKIP_SECOND_MODEL" = "1" ]; then
    log "FASE C saltata"
else
    log "FASE C - secondo modello: il metodo generalizza?"
    M14="$ROOT/models/qwen2.5-coder-14b-instruct-q4_k_m.gguf"
    if [ ! -f "$M14" ]; then
        python3 - <<'PY' || echo "download 14B fallito, salto la fase C"
from huggingface_hub import hf_hub_download
p = hf_hub_download("Qwen/Qwen2.5-Coder-14B-Instruct-GGUF",
                    "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
                    local_dir="models")
print("scaricato:", p)
PY
    fi
    if [ -f "$M14" ]; then
        kill "$GW_PID" 2>/dev/null; wait "$GW_PID" 2>/dev/null; GW_PID=""
        start_llama "$M14" "14b" 16
        python3 costbench.py --label qwen14b-q4km-closedloop \
            --base-url "http://127.0.0.1:$LLAMA_PORT" \
            --concurrency 1,4,16 --duration-s 90 --out "$PROOFS" \
            2>&1 | tee "$PROOFS/bench14b_$STAMP.txt"
    fi
fi

# ---------------------------------------------------------------- chiusura
cleanup
log "FATTO"
ls -la "$PROOFS" | tail -20
cd "$PROOFS" && sha256sum ./*.json ./*.txt > "SHA256SUMS_demo_$STAMP" 2>/dev/null
echo
echo ">>> PORTA FUORI TUTTO PRIMA DI CHIUDERE L'ISTANZA:"
echo "    cd $ROOT && tar czf demo.tar.gz --exclude='srv_*.log' proofs/"
echo "    base64 -w0 demo.tar.gz    # poi incolla in chat"
