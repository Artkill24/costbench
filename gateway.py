#!/usr/bin/env python3
"""
gateway.py - cost-governance gateway per inferenza LLM on-prem.

Sta davanti a un server OpenAI-compatible (llama.cpp ROCm, vLLM) e:

  1. MISURA il costo energetico reale di ogni richiesta, usando la curva
     token/joule MISURATA su questo hardware, non un listino stimato.
  2. APPLICA una policy di routing: privacy=strict non lascia mai la rete.
  3. REGISTRA tutto su SQLite locale, verificabile, zero egress.

La differenza rispetto a un qualsiasi proxy: il cost model non e' un
prezzo di listino, e' fisica misurata su questa scheda. Vedi
proofs/costbench_*.json e il campo cost_model_source di ogni record.

USO:
    pip install fastapi uvicorn httpx --break-system-packages
    python3 gateway.py --upstream http://127.0.0.1:8081 --port 8080

    curl http://127.0.0.1:8080/v1/chat/completions \\
      -H 'Content-Type: application/json' \\
      -H 'X-Privacy: strict' \\
      -d '{"model":"local","messages":[{"role":"user","content":"ciao"}]}'

    curl http://127.0.0.1:8080/v1/usage
    curl http://127.0.0.1:8080/v1/audit?limit=5
"""

import argparse
import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, asdict, field
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    import dashboard as _dashboard
except ImportError:          # la dashboard e' opzionale
    _dashboard = None

# ==========================================================================
# COST MODEL - da misure reali, non da stime
# ==========================================================================

# Curva token/joule misurata su AMD Radeon PRO W7900 (gfx1100),
# Qwen2.5-Coder-7B Q4_K_M, llama.cpp ROCm, carico closed-loop.
# Fonte: proofs/costbench_qwen7b-q4km-v3-closedloop_20260729T222700Z.json
MEASURED_TOKENS_PER_JOULE = {1: 0.48, 4: 0.95, 16: 4.14}
COST_MODEL_SOURCE = (
    "costbench_qwen7b-q4km-v3-closedloop_20260729T222700Z.json "
    "(W7900 gfx1100, closed-loop, achieved concurrency verified)"
)
DEFAULT_EUR_PER_KWH = 0.25


def tokens_per_joule(concurrency: int) -> float:
    """Interpola la curva misurata. Log-lineare sulla concorrenza.

    Fuori dall'intervallo misurato [1,16] si aggancia agli estremi:
    extrapolare oltre i dati sarebbe inventare numeri.
    """
    pts = sorted(MEASURED_TOKENS_PER_JOULE.items())
    c = max(1, int(concurrency))
    if c <= pts[0][0]:
        return pts[0][1]
    if c >= pts[-1][0]:
        return pts[-1][1]
    import math
    for (c0, v0), (c1, v1) in zip(pts, pts[1:]):
        if c0 <= c <= c1:
            f = (math.log(c) - math.log(c0)) / (math.log(c1) - math.log(c0))
            return v0 + f * (v1 - v0)
    return pts[-1][1]


def energy_cost(tokens: int, concurrency: int, eur_per_kwh: float) -> dict:
    tpj = tokens_per_joule(concurrency)
    joules = tokens / tpj if tpj else 0.0
    kwh = joules / 3_600_000.0
    return {
        "tokens": tokens,
        "concurrency_at_request": concurrency,
        "tokens_per_joule_measured": round(tpj, 4),
        "energy_joules": round(joules, 3),
        "energy_kwh": round(kwh, 9),
        "cost_eur": round(kwh * eur_per_kwh, 8),
        "cost_eur_per_1m_tokens": round(
            (1_000_000 / tpj / 3_600_000.0) * eur_per_kwh, 6) if tpj else None,
        "basis": "GPU board power only; excludes CPU, RAM, cooling, PUE",
        "cost_model_source": COST_MODEL_SOURCE,
    }


# ==========================================================================
# POLICY
# ==========================================================================

@dataclass
class Decision:
    route: str              # "local" | "cloud" | "denied"
    reason: str
    privacy: str
    egress: str             # "none" | "external"


class Policy:
    """Regole di routing. privacy=strict non esce dalla rete, punto."""

    def __init__(self, cloud_enabled: bool = False):
        self.cloud_enabled = cloud_enabled

    def decide(self, privacy: str, max_latency_ms: Optional[int],
               queue_depth: int) -> Decision:
        privacy = (privacy or "standard").lower()

        if privacy == "strict":
            return Decision(
                "local",
                "privacy=strict: local inference enforced, external routing "
                "unavailable regardless of load or latency budget",
                privacy, "none",
            )

        # con la coda piena e un budget di latenza stretto, il cloud
        # avrebbe senso -- ma solo se e' stato abilitato esplicitamente
        overloaded = queue_depth > 16
        tight = max_latency_ms is not None and max_latency_ms < 200

        if overloaded and tight:
            if self.cloud_enabled:
                return Decision(
                    "cloud",
                    f"queue_depth={queue_depth} exceeds local capacity and "
                    f"latency budget {max_latency_ms}ms is tighter than "
                    f"measured local TTFT at this depth",
                    privacy, "external",
                )
            return Decision(
                "local",
                f"queue_depth={queue_depth} with latency budget "
                f"{max_latency_ms}ms would favour external routing, but no "
                f"cloud provider is configured; serving locally",
                privacy, "none",
            )

        return Decision("local", "default: on-prem inference", privacy, "none")


# ==========================================================================
# AUDIT LOG - SQLite locale, nessuna dipendenza esterna
# ==========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id                TEXT PRIMARY KEY,
    ts_utc            REAL NOT NULL,
    tenant            TEXT,
    model             TEXT,
    route             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    privacy           TEXT,
    egress            TEXT NOT NULL,
    prompt_chars      INTEGER,
    tokens_out        INTEGER,
    concurrency       INTEGER,
    latency_ms        REAL,
    ttft_ms           REAL,
    energy_joules     REAL,
    cost_eur          REAL,
    cost_model_source TEXT,
    status            TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts_utc);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant);
"""


class Audit:
    def __init__(self, path: str):
        self.path = path
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            db.commit()

    def write(self, row: dict):
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(f"INSERT INTO audit ({cols}) VALUES ({marks})",
                       list(row.values()))
            db.commit()

    def recent(self, limit: int = 20, tenant: Optional[str] = None):
        q = "SELECT * FROM audit"
        args = []
        if tenant:
            q += " WHERE tenant = ?"
            args.append(tenant)
        q += " ORDER BY ts_utc DESC LIMIT ?"
        args.append(limit)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(q, args)]

    def totals(self, tenant: Optional[str] = None):
        q = ("SELECT COUNT(*) n, COALESCE(SUM(tokens_out),0) tokens, "
             "COALESCE(SUM(energy_joules),0) joules, "
             "COALESCE(SUM(cost_eur),0) eur, "
             "SUM(CASE WHEN egress='external' THEN 1 ELSE 0 END) external "
             "FROM audit")
        args = []
        if tenant:
            q += " WHERE tenant = ?"
            args.append(tenant)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute(q, args).fetchone())


# ==========================================================================
# GATEWAY
# ==========================================================================

@dataclass
class State:
    upstream: str
    eur_per_kwh: float
    audit: Audit
    policy: Policy
    inflight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_app(state: State) -> FastAPI:
    app = FastAPI(title="on-prem LLM cost gateway", version="0.1.0")

    @app.get("/healthz")
    async def healthz():
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{state.upstream}/v1/models")
            up = r.status_code == 200
        except Exception:
            up = False
        return {"gateway": "ok", "upstream_reachable": up,
                "inflight": state.inflight,
                "cost_model_source": COST_MODEL_SOURCE}

    @app.get("/", response_class=HTMLResponse)
    async def root():
        # una 404 sulla radice, in una demo, sembra un errore
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0;url=/dashboard">')

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        """L'audit ledger reso leggibile a un umano.

        Stessi dati di /v1/audit, ma con la domanda operativa in evidenza:
        quanto e' costato, a chi, e quanta capacita' si sta sprecando
        servendo a bassa concorrenza.
        """
        if _dashboard is None:
            raise HTTPException(501, "dashboard.py non trovato")
        return HTMLResponse(
            _dashboard.render(state.audit.path, state.eur_per_kwh))

    @app.get("/v1/usage")
    async def usage(tenant: Optional[str] = None):
        t = state.audit.totals(tenant)
        return {
            "requests": t["n"],
            "tokens": t["tokens"],
            "energy_joules": round(t["joules"], 2),
            "energy_kwh": round(t["joules"] / 3_600_000, 8),
            "cost_eur": round(t["eur"], 6),
            "requests_with_external_egress": t["external"],
            "cost_model_source": COST_MODEL_SOURCE,
            "basis": "GPU board power only; excludes CPU, RAM, cooling, PUE",
        }

    @app.get("/v1/audit")
    async def audit_tail(limit: int = 20, tenant: Optional[str] = None):
        return {"records": state.audit.recent(min(limit, 200), tenant)}

    @app.get("/v1/models")
    async def models():
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{state.upstream}/v1/models")
        return JSONResponse(r.json(), status_code=r.status_code)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "corpo JSON non valido")

        privacy = request.headers.get("X-Privacy", "standard")
        tenant = request.headers.get("X-Tenant", "default")
        try:
            budget = int(request.headers.get("X-Max-Latency-Ms", "")) \
                if request.headers.get("X-Max-Latency-Ms") else None
        except ValueError:
            budget = None

        async with state.lock:
            queue_depth = state.inflight
            state.inflight += 1
        conc_at_start = max(1, queue_depth + 1)

        decision = state.policy.decide(privacy, budget, queue_depth)
        rid = str(uuid.uuid4())
        t0 = time.perf_counter()
        prompt_chars = sum(
            len(str(m.get("content", "")))
            for m in body.get("messages", []) if isinstance(m, dict)
        )

        def record(tokens_out, ttft_ms, status):
            latency_ms = (time.perf_counter() - t0) * 1000
            ec = energy_cost(tokens_out, conc_at_start, state.eur_per_kwh)
            state.audit.write({
                "id": rid,
                "ts_utc": time.time(),
                "tenant": tenant,
                "model": body.get("model"),
                "route": decision.route,
                "reason": decision.reason,
                "privacy": decision.privacy,
                "egress": decision.egress,
                "prompt_chars": prompt_chars,
                "tokens_out": tokens_out,
                "concurrency": conc_at_start,
                "latency_ms": round(latency_ms, 2),
                "ttft_ms": round(ttft_ms, 2) if ttft_ms else None,
                "energy_joules": ec["energy_joules"],
                "cost_eur": ec["cost_eur"],
                "cost_model_source": COST_MODEL_SOURCE,
                "status": status,
            })
            return ec, latency_ms

        if decision.route == "cloud":
            async with state.lock:
                state.inflight -= 1
            record(0, None, "not_implemented")
            raise HTTPException(501, {
                "error": "external routing selected but no provider wired",
                "decision": asdict(decision),
            })

        headers = {
            "X-Route": decision.route,
            "X-Egress": decision.egress,
            "X-Decision-Reason": decision.reason[:180],
            "X-Request-Id": rid,
        }

        # ---- streaming ------------------------------------------------
        if body.get("stream"):
            async def gen():
                tokens = 0
                ttft = None
                status = "ok"
                try:
                    async with httpx.AsyncClient(timeout=600) as c:
                        async with c.stream(
                            "POST", f"{state.upstream}/v1/chat/completions",
                            json=body,
                        ) as r:
                            async for line in r.aiter_lines():
                                if line.startswith("data:"):
                                    payload = line[5:].strip()
                                    # il [DONE] dell'upstream va SOPPRESSO:
                                    # i client chiudono lo stream appena lo
                                    # vedono, e non leggerebbero il trailer
                                    # di costo. Lo riemettiamo noi in coda.
                                    if payload == "[DONE]":
                                        continue
                                    if payload:
                                        try:
                                            obj = json.loads(payload)
                                            d = (obj.get("choices") or [{}])[0]
                                            if (d.get("delta") or {}).get(
                                                    "content"):
                                                tokens += 1
                                                if ttft is None:
                                                    ttft = (
                                                        time.perf_counter()
                                                        - t0) * 1000
                                        except json.JSONDecodeError:
                                            pass
                                    yield f"{line}\n\n"
                                elif line:
                                    yield f"{line}\n"
                except Exception as e:
                    status = f"error: {type(e).__name__}"
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                finally:
                    async with state.lock:
                        state.inflight -= 1
                    ec, _ = record(tokens, ttft, status)
                    # trailer: il costo del task, dentro lo stream stesso
                    yield ("data: " + json.dumps(
                        {"x_cost": ec, "x_decision": asdict(decision)}
                    ) + "\n\n")
                    yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers=headers)

        # ---- non streaming --------------------------------------------
        try:
            async with httpx.AsyncClient(timeout=600) as c:
                r = await c.post(f"{state.upstream}/v1/chat/completions",
                                 json=body)
            data = r.json()
        except Exception as e:
            async with state.lock:
                state.inflight -= 1
            record(0, None, f"error: {type(e).__name__}")
            raise HTTPException(502, f"upstream non raggiungibile: {e}")

        async with state.lock:
            state.inflight -= 1

        tokens_out = ((data.get("usage") or {}).get("completion_tokens")
                      or 0)
        ec, latency = record(tokens_out, None, "ok")
        data["x_cost"] = ec
        data["x_decision"] = asdict(decision)
        data["x_latency_ms"] = round(latency, 2)
        return JSONResponse(data, headers=headers)

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="http://127.0.0.1:8081")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="audit.sqlite")
    ap.add_argument("--eur-per-kwh", type=float, default=DEFAULT_EUR_PER_KWH)
    ap.add_argument("--enable-cloud", action="store_true",
                    help="consente il routing esterno (default: mai)")
    a = ap.parse_args()

    state = State(
        upstream=a.upstream.rstrip("/"),
        eur_per_kwh=a.eur_per_kwh,
        audit=Audit(a.db),
        policy=Policy(cloud_enabled=a.enable_cloud),
    )
    print(f"gateway -> {state.upstream}")
    print(f"audit    : {os.path.abspath(a.db)}")
    print(f"cloud    : {'abilitato' if a.enable_cloud else 'DISABILITATO'}")
    print(f"modello di costo: {COST_MODEL_SOURCE}")

    import uvicorn
    uvicorn.run(build_app(state), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
