#!/usr/bin/env python3
"""
memory.py - memoria persistente per l'agente, misurata in joule.

L'agente senza memoria riparte da zero ogni volta: rifa' list_files, rilegge
gli stessi file, rispende gli stessi token. Ogni token rispeso e' energia
rispesa -- e su questo hardware sappiamo quanta, perche' l'abbiamo misurata.

Quindi la memoria qui non e' una feature di comodita': e' un'ottimizzazione
energetica, e il risparmio e' misurabile in joule ed euro.

Tre operazioni, niente di piu':
    remember(kind, key, value)   memorizza un fatto
    recall(query)                recupera i fatti pertinenti
    forget(key)                  dimentica

Nessun embedding: recall per keyword con punteggio. Su un workspace di
qualche decina di file funziona, non introduce dipendenze, e il percorso
di misura resta senza superficie di terze parti.

USO:
    python3 memory.py memory.sqlite stats
    python3 memory.py memory.sqlite recall "tokens per joule"
"""

import json
import math
import re
import sqlite3
import sys
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,      -- workspace | fact | answer
    value      TEXT NOT NULL,
    tokens_at_write INTEGER,       -- token spesi per produrre questo fatto
    created    REAL NOT NULL,
    last_used  REAL,
    uses       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memory(kind);
"""

STOP = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "are", "was", "were", "which", "what", "that", "this", "it", "with",
    "from", "by", "as", "be", "has", "have", "you", "your", "find", "cite",
    "exact", "filename", "file", "give", "tell", "me", "please",
}


def tokenize(text):
    return [w for w in re.findall(r"[a-z0-9_]+", (text or "").lower())
            if len(w) > 2 and w not in STOP]


class Memory:
    """Memoria dell'agente. Ogni richiamo registra i token che ha risparmiato."""

    def __init__(self, path="memory.sqlite"):
        self.path = path
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            db.commit()
        self.tokens_saved = 0      # nella sessione corrente
        self.hits = 0

    # -- scrittura --------------------------------------------------------
    def remember(self, kind, key, value, tokens_at_write=0):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO memory (key,kind,value,tokens_at_write,created,"
                "last_used,uses) VALUES (?,?,?,?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "tokens_at_write=excluded.tokens_at_write",
                (key, kind, value, tokens_at_write, time.time(), None))
            db.commit()

    def forget(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            n = db.execute("DELETE FROM memory WHERE key = ?", (key,)).rowcount
            db.commit()
        return n

    # -- lettura ----------------------------------------------------------
    def recall(self, query, limit=3, min_score=0.15):
        """Fatti pertinenti alla query, per punteggio di sovrapposizione.

        Registra i token che il richiamo ha risparmiato: sono quelli che
        l'agente aveva speso la prima volta per produrre quel fatto.
        """
        qt = set(tokenize(query))
        if not qt:
            return []
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT * FROM memory")]

        scored = []
        for r in rows:
            mt = set(tokenize(r["key"] + " " + r["value"]))
            if not mt:
                continue
            # Jaccard smorzato: privilegia la copertura della query
            overlap = len(qt & mt)
            score = overlap / len(qt) * (1 + math.log1p(overlap) / 4)
            if score >= min_score:
                scored.append((score, r))
        scored.sort(key=lambda s: -s[0])
        top = scored[:limit]

        if top:
            self.hits += len(top)
            self.tokens_saved += sum(r["tokens_at_write"] or 0 for _, r in top)
            with closing(sqlite3.connect(self.path)) as db:
                for _, r in top:
                    db.execute("UPDATE memory SET uses = uses + 1, "
                               "last_used = ? WHERE key = ?",
                               (time.time(), r["key"]))
                db.commit()
        return [{"key": r["key"], "kind": r["kind"], "value": r["value"],
                 "score": round(s, 3)} for s, r in top]

    def as_context(self, query, limit=3):
        """I fatti richiamati, pronti da iniettare nel prompt."""
        hits = self.recall(query, limit)
        if not hits:
            return ""
        lines = [f"  [{h['kind']}] {h['key']}: {h['value'][:600]}"
                 for h in hits]
        return ("Relevant facts you already established in earlier tasks "
                "(do not re-derive them):\n" + "\n".join(lines))

    # -- contabilita' -----------------------------------------------------
    def stats(self, tokens_per_joule=4.142, eur_per_kwh=0.25):
        """Cosa ha risparmiato la memoria, in token, joule ed euro.

        tokens_per_joule: 4.142 misurato su W7900 a concorrenza 16
        (proofs/costbench_qwen7b-q4km-v3-closedloop_*.json).
        """
        with closing(sqlite3.connect(self.path)) as db:
            n, uses, tw = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(uses),0), "
                "COALESCE(SUM(tokens_at_write),0) FROM memory").fetchone()
        joules = self.tokens_saved / tokens_per_joule if tokens_per_joule else 0
        return {
            "entries": n,
            "total_recalls": uses,
            "tokens_stored": tw,
            "session_hits": self.hits,
            "session_tokens_saved": self.tokens_saved,
            "session_joules_saved": round(joules, 2),
            "session_eur_saved": round(joules / 3_600_000 * eur_per_kwh, 10),
            "tokens_per_joule_basis": tokens_per_joule,
        }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__.strip().split("USO:")[1])
        sys.exit(1)
    m = Memory(sys.argv[1])
    cmd = sys.argv[2]
    if cmd == "stats":
        print(json.dumps(m.stats(), indent=2))
    elif cmd == "recall":
        print(json.dumps(m.recall(" ".join(sys.argv[3:])), indent=2))
    elif cmd == "list":
        with closing(sqlite3.connect(m.path)) as db:
            db.row_factory = sqlite3.Row
            for r in db.execute("SELECT key,kind,uses,tokens_at_write "
                                "FROM memory ORDER BY uses DESC"):
                print(f"  {r['uses']:>3} uses  {r['kind']:<10} {r['key']}")
    elif cmd == "forget":
        print("rimosse:", m.forget(sys.argv[3]))
    else:
        print("comandi: stats | recall <query> | list | forget <key>")
