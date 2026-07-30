#!/usr/bin/env python3
"""
mock_upstream.py - finto server OpenAI-compatible.

Serve SOLO a validare gateway.py senza modello e senza GPU: risponde
token finti con streaming e usage. Nessun valore di misura, mai da usare
per numeri nella submission.

USO:
    python3 mock_upstream.py --port 8081
"""

import argparse
import asyncio
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

WORDS = ("silicon wafers hum beneath the cooling fans while tensors flow "
         "through matrix units and joules become tokens one clock at a "
         "time until the batch is drained and the queue is empty again "
         "and the memory bus finally rests").split()

app = FastAPI(title="mock upstream (NOT for measurement)")


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [
        {"id": "mock-model", "object": "model", "owned_by": "mock"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    n = min(int(body.get("max_tokens") or 48), 400)
    rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = body.get("model", "mock-model")

    if body.get("stream"):
        async def gen():
            for i in range(n):
                chunk = {
                    "id": rid, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": WORDS[i % len(WORDS)] + " "},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.012)   # ~80 tok/s, plausibile su CPU
            yield ("data: " + json.dumps({
                "id": rid, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}],
            }) + "\n\n")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    await asyncio.sleep(n * 0.012)
    text = " ".join(WORDS[i % len(WORDS)] for i in range(n))
    return JSONResponse({
        "id": rid, "object": "chat.completion", "created": created,
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 16, "completion_tokens": n,
                  "total_tokens": 16 + n},
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    print(f"MOCK upstream - token finti, nessun valore di misura")
    print(f"in ascolto su http://{a.host}:{a.port}")
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
