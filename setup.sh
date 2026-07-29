#!/usr/bin/env bash
#
# setup.sh - sessione GPU completa in un colpo solo, senza supervisione.
#
# Filosofia: 1 credito = 1 ora. Non si debugga qui dentro.
# Lanci, esci, torni tra 40 minuti e hai i proofs/.
#
# USO (sull'istanza Radeon):
#     git clone https://github.com/Artkill24/costbench.git
#     cd costbench && bash setup.sh 2>&1 | tee session.log
#
# FASE 0 aborta subito se la telemetria di potenza non c'e':
# meglio bruciare 2 minuti che 40.

set -uo pipefail

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOFS="$ROOT/proofs"
MODELS="$ROOT/models"
PORT=8081
SERVER_PID=""

mkdir -p "$PROOFS" "$MODELS"

log()  { echo -e "\n\033[1;36m[$(date -u +%H:%M:%S)] $*\033[0m"; }
fail() { echo -e "\n\033[1;31m>>> ABORT: $*\033[0m"; cleanup; exit 1; }

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        log "Chiudo llama-server (pid $SERVER_PID)"
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
log "FASE 0 - PREFLIGHT (aborta subito se i watt non escono)"
# ---------------------------------------------------------------------------

command -v rocm-smi >/dev/null || fail "rocm-smi assente: immagine sbagliata?"

python3 costbench.py --preflight 2>&1 | tee "$PROOFS/preflight_$STAMP.txt"

# La telemetria e' l'assunzione critica dell'intero progetto.
if ! grep -qE "Backend potenza *: *(sysfs|rocm-smi)" "$PROOFS/preflight_$STAMP.txt"; then
    fail "nessuna telemetria di potenza. Il progetto va ripensato, non proseguo."
fi

log "Telemetria OK. Proseguo."

# ---------------------------------------------------------------------------
log "FASE 1 - RILEVAMENTO GPU"
# ---------------------------------------------------------------------------

rocm-smi --showproductname --showmeminfo vram 2>&1 | tee "$PROOFS/gpu_$STAMP.txt"

GFX=$(rocminfo 2>/dev/null | grep -oP 'gfx[0-9a-f]+' | head -1)
[ -z "$GFX" ] && GFX="gfx1100"
log "Architettura: $GFX"

# VRAM in MB -> scelta modello. Sotto i 20 GB niente 7B in Q8.
VRAM_MB=$(rocm-smi --showmeminfo vram --csv 2>/dev/null \
          | grep -oP '\d{6,}' | head -1 | awk '{print int($1/1048576)}')
[ -z "$VRAM_MB" ] && VRAM_MB=16000
log "VRAM stimata: ${VRAM_MB} MB"

# ---------------------------------------------------------------------------
log "FASE 2 - BUILD llama.cpp CON HIP (~10-15 min)"
# ---------------------------------------------------------------------------

if [ ! -x "$ROOT/llama.cpp/build/bin/llama-server" ]; then
    [ -d "$ROOT/llama.cpp" ] || \
        git clone --depth 1 https://github.com/ggml-org/llama.cpp "$ROOT/llama.cpp" \
        || fail "clone llama.cpp fallito"

    cd "$ROOT/llama.cpp"
    cmake -B build \
        -DGGML_HIP=ON \
        -DAMDGPU_TARGETS="$GFX" \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        2>&1 | tail -20 || fail "cmake configure fallito"

    cmake --build build --config Release -j"$(nproc)" 2>&1 | tail -20 \
        || fail "build fallita"
    cd "$ROOT"
else
    log "llama-server gia' presente, salto il build."
fi

SERVER="$ROOT/llama.cpp/build/bin/llama-server"
[ -x "$SERVER" ] || fail "llama-server non trovato dopo il build"

# ---------------------------------------------------------------------------
log "FASE 3 - MODELLO"
# ---------------------------------------------------------------------------

if [ "$VRAM_MB" -ge 20000 ]; then
    HF_REPO="Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"
    HF_FILE="qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    LABEL="qwen7b-q4km"
else
    HF_REPO="Qwen/Qwen2.5-Coder-3B-Instruct-GGUF"
    HF_FILE="qwen2.5-coder-3b-instruct-q4_k_m.gguf"
    LABEL="qwen3b-q4km"
fi
log "Modello: $HF_REPO / $HF_FILE"

MODEL_PATH="$MODELS/$HF_FILE"
if [ ! -f "$MODEL_PATH" ]; then
    pip install -q huggingface_hub --break-system-packages 2>/dev/null || \
        pip install -q huggingface_hub 2>/dev/null || true
    python3 - "$HF_REPO" "$HF_FILE" "$MODELS" <<'PY' || fail "download modello fallito"
import sys
from huggingface_hub import hf_hub_download
p = hf_hub_download(repo_id=sys.argv[1], filename=sys.argv[2], local_dir=sys.argv[3])
print("scaricato:", p)
PY
fi
[ -f "$MODEL_PATH" ] || fail "modello non trovato: $MODEL_PATH"

# ---------------------------------------------------------------------------
log "FASE 4 - AVVIO SERVER"
# ---------------------------------------------------------------------------

"$SERVER" -m "$MODEL_PATH" \
    --host 127.0.0.1 --port "$PORT" \
    -ngl 99 -c 8192 -np 8 -cb \
    > "$PROOFS/server_$STAMP.log" 2>&1 &
SERVER_PID=$!
log "pid $SERVER_PID, attendo..."

for i in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && break
    kill -0 "$SERVER_PID" 2>/dev/null || fail "server morto, vedi proofs/server_$STAMP.log"
    sleep 5
done
curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || fail "server non risponde dopo 5 min"

log "Server su. Verifico che i layer siano sulla GPU:"
grep -iE "offload|device|ROCm|HIP" "$PROOFS/server_$STAMP.log" | head -10

# ---------------------------------------------------------------------------
log "FASE 5 - BENCHMARK (~15 min)"
# ---------------------------------------------------------------------------

python3 costbench.py \
    --label "$LABEL" \
    --base-url "http://127.0.0.1:$PORT" \
    --concurrency 1,2,4,8,16 \
    --max-tokens 256 \
    --eur-per-kwh 0.25 \
    --out "$PROOFS" 2>&1 | tee "$PROOFS/bench_$STAMP.txt" \
    || fail "benchmark fallito"

# ---------------------------------------------------------------------------
log "FASE 6 - PROVE"
# ---------------------------------------------------------------------------

cd "$PROOFS"
sha256sum ./*.json ./*.txt > "SHA256SUMS_$STAMP" 2>/dev/null || true
cd "$ROOT"

log "FATTO. Contenuto di proofs/:"
ls -la "$PROOFS"

echo
echo "================= RISULTATI ================="
cat "$PROOFS"/costbench_"$LABEL"_*.json 2>/dev/null | python3 -m json.tool | head -80
echo "============================================="
echo
echo ">>> SCARICA I FILE PRIMA DI CHIUDERE L'ISTANZA:"
echo "    scp -r <user>@<host>:$PROOFS ./proofs/"
echo ">>> Oppure dal file browser di JupyterLab: tasto destro -> Download"
