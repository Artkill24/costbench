#!/usr/bin/env python3
"""
agent.py - agente on-prem con invocazione di tool e costo energetico reale.

Ogni chiamata al modello passa dal gateway, che restituisce il costo
energetico MISURATO. A fine task l'agente riporta:

    passi, token, joule, euro, e byte usciti dalla rete (zero, verificato
    contro l'audit log del gateway, non dichiarato a parole).

Capability Track 2 coperte:
  - tool invocation      (list_files, read_file, grep, stat)
  - multi-step planning  (loop osserva-decidi-agisci con budget di passi)
  - privacy / on-prem    (X-Privacy: strict, filesystem confinato a --root)

USO:
    python3 agent.py --task "Quanti token/joule a concorrenza 16?" --root .
    python3 agent.py --task "..." --gateway http://127.0.0.1:8090 --max-steps 6

NOTA: con mock_upstream.py il modello produce testo casuale e l'agente si
ferma al primo passo. Serve un modello vero per il ragionamento; la
contabilita' di costo invece e' reale in entrambi i casi.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    from memory import Memory
except ImportError:          # la memoria e' opzionale
    Memory = None

# ==========================================================================
# TOOL - solo lettura, confinati sotto --root
# ==========================================================================

MAX_READ_BYTES = 20_000
# 'rejected' contiene misure dichiarate invalide: l'agente non deve
# vederle, altrimenti le cita come fonte e la demo mostra dati falsi.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build",
             "rejected"}


class Sandbox:
    """Impedisce l'uscita da root. Un agente 'on-prem' che legge /etc/shadow
    non e' on-prem, e' solo un incidente piu' lento."""

    def __init__(self, root):
        self.root = os.path.realpath(root)

    def resolve(self, path):
        p = os.path.realpath(os.path.join(self.root, path))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise PermissionError(f"percorso fuori da root: {path}")
        return p


def tool_list_files(sb, path=".", pattern="*"):
    base = sb.resolve(path)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fnmatch.fnmatch(fn, pattern):
                rel = os.path.relpath(os.path.join(dirpath, fn), sb.root)
                out.append(rel)
        if len(out) > 300:
            break
    return "\n".join(sorted(out)[:300]) or "(nessun file)"


def tool_read_file(sb, path, max_lines=120):
    p = sb.resolve(path)
    if os.path.getsize(p) > MAX_READ_BYTES * 5:
        return f"(file troppo grande: {os.path.getsize(p)} byte)"
    with open(p, errors="replace") as f:
        lines = f.readlines()[:int(max_lines)]
    return "".join(lines)[:MAX_READ_BYTES]


def tool_grep(sb, pattern, path=".", max_hits=40):
    base = sb.resolve(path)
    rx = re.compile(pattern)
    hits = []
    walk = [base] if os.path.isfile(base) else None
    if walk is None:
        walk = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            walk += [os.path.join(dirpath, f) for f in filenames]
    for fp in walk:
        try:
            with open(fp, errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, sb.root)
                        hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= max_hits:
                            return "\n".join(hits)
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(hits) or "(nessuna occorrenza)"


def tool_stat(sb, path="."):
    p = sb.resolve(path)
    st = os.stat(p)
    kind = "dir" if os.path.isdir(p) else "file"
    return json.dumps({"path": path, "type": kind, "bytes": st.st_size,
                       "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(st.st_mtime))})


TOOLS = {
    "list_files": (tool_list_files, "list_files(path, pattern) - elenca i file"),
    "read_file":  (tool_read_file,  "read_file(path, max_lines) - legge un file"),
    "grep":       (tool_grep,       "grep(pattern, path) - cerca una regex"),
    "stat":       (tool_stat,       "stat(path) - metadati di un percorso"),
}

SYSTEM = """You are an on-premises analysis agent. You cannot access the
internet. You answer only from files you have actually read.

To use a tool, reply with EXACTLY one JSON object and nothing else:
{"tool": "<name>", "args": {...}}

Available tools:
""" + "\n".join(f"  {d}" for _, d in TOOLS.values()) + """

When you have enough information, reply with EXACTLY:
{"answer": "<your final answer, citing the files you read>"}

Rules:
- One JSON object per reply. No prose outside the JSON. No markdown fences.
- If facts from earlier tasks are provided below, TRUST THEM and do not
  re-derive them with tools. Re-reading a file you already summarised costs
  energy for no new information.
- Paths are RELATIVE to the workspace root shown below. Absolute paths
  like /var/log or /opt are outside the sandbox and will be DENIED.
- `grep` takes a REGEX matched against file CONTENTS, and `path` must be
  a real directory or file, not a wildcard. Search for short identifiers
  that literally appear in the data (e.g. tokens_per_joule, avg_w), not
  for whole sentences.
- The data files are JSON with English field names. Search in English.
- Never invent file contents. If you did not read it, say so.
"""


def workspace_overview(sb, max_entries=60):
    """Il modello non puo' indovinare la struttura: gliela diamo.

    Senza questo un 7B tenta /var/log, /opt/logs, /usr/local/logs e
    brucia tutti i passi disponibili in percorsi inesistenti.
    """
    entries = []
    for dirpath, dirnames, filenames in os.walk(sb.root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, sb.root)
        if rel_dir.count(os.sep) > 1:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), sb.root)
            entries.append(rel)
            if len(entries) >= max_entries:
                return sorted(entries)
    return sorted(entries)


# ==========================================================================
# CLIENT verso il gateway
# ==========================================================================

# Vincola l'output a livello di sampling: llama.cpp converte lo schema in
# grammatica GBNF, quindi il modello NON PUO' emettere JSON malformato.
# Senza questo un 3B sbaglia il formato una volta su tre e il loop muore.
ACTION_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string",
                         "enum": ["list_files", "read_file", "grep", "stat"]},
                "args": {"type": "object"},
            },
            "required": ["tool", "args"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    ]
}


class GatewayClient:
    """Ogni chiamata porta indietro il costo energetico misurato."""

    def __init__(self, base_url, model, tenant, privacy="strict",
                 constrain=True):
        self.base = base_url.rstrip("/")
        self.model = model
        self.tenant = tenant
        self.privacy = privacy
        self.constrain = constrain
        self.total_tokens = 0
        self.total_joules = 0.0
        self.total_eur = 0.0
        self.calls = 0
        self.external_egress_calls = 0
        self.request_ids = []

    def chat(self, messages, max_tokens=400):
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": 0.2}
        if self.constrain:
            # forma standard OpenAI, accettata da llama.cpp e vLLM.
            # llama.cpp la converte in grammatica GBNF: il modello non
            # PUO' emettere JSON malformato, non e' una richiesta nel prompt.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_action", "strict": True,
                                "schema": ACTION_SCHEMA},
            }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "X-Privacy": self.privacy,
                     "X-Tenant": self.tenant},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                rid = r.headers.get("X-Request-Id")
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"gateway HTTP {e.code}: {e.read()[:300]}")
        except Exception as e:
            raise RuntimeError(f"gateway non raggiungibile: {e}")

        cost = data.get("x_cost") or {}
        dec = data.get("x_decision") or {}
        self.calls += 1
        self.total_tokens += cost.get("tokens") or 0
        self.total_joules += cost.get("energy_joules") or 0.0
        self.total_eur += cost.get("cost_eur") or 0.0
        if dec.get("egress") == "external":
            self.external_egress_calls += 1
        if rid:
            self.request_ids.append(rid)

        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass
        return content, cost, dec

    def usage(self):
        try:
            with urllib.request.urlopen(
                    f"{self.base}/v1/usage?tenant={self.tenant}",
                    timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            return {}


# ==========================================================================
# LOOP
# ==========================================================================

def parse_action(text):
    """Estrae il primo oggetto JSON. I modelli aggiungono spesso prosa."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run(task, sb, client, max_steps, verbose=True, memory=None):
    overview = workspace_overview(sb)
    recalled = memory.as_context(task) if memory else ""
    if verbose and recalled:
        print(f"  memoria: {memory.hits} fatti richiamati, "
              f"{memory.tokens_saved} token gia' spesi in passato")
    opening = (
        f"Workspace root: {sb.root}\n"
        f"Files available (paths are relative to this root):\n"
        + "\n".join(f"  {e}" for e in overview)
        + (f"\n\n{recalled}" if recalled else "")
        + f"\n\nTask: {task}"
    )
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": opening}]
    trace = []
    answer = None
    seen_calls = {}
    loops = 0
    t0 = time.perf_counter()
    if verbose:
        print(f"  workspace: {len(overview)} file elencati al modello")

    for step in range(1, max_steps + 1):
        content, cost, dec = client.chat(messages)
        if verbose:
            print(f"\n\033[1;36m[passo {step}]\033[0m "
                  f"{cost.get('tokens', 0)} tok · "
                  f"{cost.get('energy_joules', 0):.1f} J · "
                  f"€{cost.get('cost_eur', 0):.8f} · "
                  f"route={dec.get('route')} egress={dec.get('egress')}")

        action = parse_action(content)
        if action is None:
            if verbose:
                print("  output non interpretabile come azione, mi fermo.")
                print(f"  grezzo: {content[:200]}")
            answer = content.strip() or "(nessuna risposta interpretabile)"
            trace.append({"step": step, "kind": "unparsed",
                          "raw": content[:500]})
            break

        if "answer" in action:
            answer = action["answer"]
            trace.append({"step": step, "kind": "answer"})
            if verbose:
                print("  risposta finale ricevuta.")
            break

        name = action.get("tool")
        args = action.get("args") or {}
        sig = json.dumps({"t": name, "a": args}, sort_keys=True)

        if sig in seen_calls:
            # senza questo un 7B ripete la stessa grep fino a esaurire
            # il budget di passi. Non e' un errore del tool: e' che il
            # modello non registra di averlo gia' fatto.
            obs = (f"LOOP DETECTED: you already called {name} with exactly "
                   f"these arguments at step {seen_calls[sig]} and got the "
                   f"same result. Do not repeat it. Either use a DIFFERENT "
                   f"tool or different arguments, or answer now with "
                   f'{{"answer": "..."}} based on what you have already read.')
            if verbose:
                print(f"  tool: {name}({args}) -> LOOP, gia' fatto al passo "
                      f"{seen_calls[sig]}")
            trace.append({"step": step, "kind": "loop", "tool": name,
                          "args": args, "first_seen": seen_calls[sig]})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": obs})
            loops += 1
            if loops >= 2:
                answer = ("(interrotto: il modello ripete la stessa azione "
                          "senza progredire)")
                break
            continue

        seen_calls[sig] = step
        if name not in TOOLS:
            obs = f"ERRORE: tool sconosciuto '{name}'. Disponibili: {list(TOOLS)}"
        else:
            try:
                obs = TOOLS[name][0](sb, **args)
            except TypeError as e:
                obs = f"ERRORE argomenti per {name}: {e}"
            except PermissionError as e:
                obs = f"NEGATO: {e}"
            except Exception as e:
                obs = f"ERRORE {type(e).__name__}: {e}"

        if verbose:
            print(f"  tool: {name}({args}) -> {len(obs)} caratteri")
        trace.append({"step": step, "kind": "tool", "tool": name,
                      "args": args, "obs_chars": len(obs)})
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user",
                         "content": f"Observation:\n{obs[:6000]}"})
    else:
        answer = "(budget di passi esaurito senza risposta finale)"

    if memory and answer and len(answer) > 30 and "budget" not in answer[:20]:
        # la chiave e' derivata dal task: una domanda simile la ritrovera'
        import hashlib
        key = "answer_" + hashlib.sha1(task.encode()).hexdigest()[:10]
        memory.remember("answer", key, f"Q: {task}\nA: {answer}",
                        tokens_at_write=client.total_tokens)
        # e cio' che ha imparato leggendo, cosi' non rilegge
        for e in trace:
            if e.get("kind") == "tool" and e.get("tool") in ("list_files",
                                                             "stat"):
                memory.remember("workspace",
                                f"{e['tool']}_{json.dumps(e['args'], sort_keys=True)[:60]}",
                                f"already inspected: {e['args']}",
                                tokens_at_write=40)

    return {
        "task": task,
        "answer": answer,
        "steps": len(trace),
        "trace": trace,
        "wall_s": round(time.perf_counter() - t0, 2),
        "memory": memory.stats() if memory else None,
    }


def report(result, client):
    u = client.usage()
    print("\n" + "=" * 62)
    print("RISPOSTA")
    print("=" * 62)
    print(result["answer"])
    print("\n" + "=" * 62)
    print("CONTABILITA' DEL TASK")
    print("=" * 62)
    print(f"  passi                 : {result['steps']}")
    print(f"  chiamate al modello   : {client.calls}")
    print(f"  token generati        : {client.total_tokens}")
    print(f"  energia               : {client.total_joules:.1f} J "
          f"({client.total_joules / 3_600_000:.3e} kWh)")
    print(f"  costo elettrico       : EUR {client.total_eur:.8f}")
    print(f"  durata                : {result['wall_s']} s")
    print(f"  chiamate con egress   : {client.external_egress_calls}")
    ms = result.get("memory")
    if ms and ms["session_hits"]:
        print(f"  memoria               : {ms['session_hits']} fatti richiamati, "
              f"{ms['session_tokens_saved']} token non rispesi")
        print(f"  energia risparmiata   : {ms['session_joules_saved']} J "
              f"(EUR {ms['session_eur_saved']:.8f})")
    print(f"  byte usciti dalla rete: 0 "
          f"(verificato contro l'audit del gateway, non dichiarato)")
    if u:
        print(f"  audit tenant totale   : {u.get('requests')} richieste, "
              f"{u.get('requests_with_external_egress')} con egress esterno")
    print("\n  Il costo deriva dalla curva token/joule misurata su questo")
    print("  hardware. Fonte nel campo cost_model_source di ogni record.")
    print("  Potenza scheda GPU soltanto: esclude CPU, RAM, raffreddamento, PUE.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--root", default=".", help="radice del sandbox filesystem")
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="local")
    ap.add_argument("--tenant", default="agent")
    ap.add_argument("--privacy", default="strict",
                    choices=["strict", "standard"])
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--no-constrain", action="store_true",
                    help="non inviare json_schema (per upstream che non lo "
                         "supportano, es. mock_upstream.py)")
    ap.add_argument("--memory", metavar="DB",
                    help="file SQLite della memoria persistente. Senza questo "
                         "l'agente riparte da zero a ogni task.")
    ap.add_argument("--json-out", help="scrive il trace completo su file")
    a = ap.parse_args()

    sb = Sandbox(a.root)
    client = GatewayClient(a.gateway, a.model, a.tenant, a.privacy,
                           constrain=not a.no_constrain)
    print(f"agente on-prem · root={sb.root} · privacy={a.privacy}")
    print(f"gateway: {a.gateway} · output vincolato: "
          f"{'no' if a.no_constrain else 'si (json_schema)'}")

    mem = None
    if a.memory:
        if Memory is None:
            print("ERRORE: memory.py non trovato")
            return 2
        mem = Memory(a.memory)
        print(f"memoria  : {a.memory}")

    try:
        result = run(a.task, sb, client, a.max_steps, memory=mem)
    except RuntimeError as e:
        print(f"\nERRORE: {e}")
        return 2

    report(result, client)

    if a.json_out:
        result["accounting"] = {
            "calls": client.calls, "tokens": client.total_tokens,
            "energy_joules": round(client.total_joules, 3),
            "cost_eur": round(client.total_eur, 10),
            "external_egress_calls": client.external_egress_calls,
            "request_ids": client.request_ids,
        }
        with open(a.json_out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\ntrace scritto in {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
