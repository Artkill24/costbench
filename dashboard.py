#!/usr/bin/env python3
"""
dashboard.py - la dashboard del gateway.

Rende leggibile a un umano quello che l'audit ledger contiene gia': quanto
si e' speso, per chi, in quanti joule, e quanti byte sono usciti dalla rete.

Nessuna dipendenza esterna: HTML e SVG generati a mano, come il resto del
percorso di misura. Un grafico che arriva da una CDN non e' on-prem.

Usato da gateway.py su GET /dashboard. Testabile da solo:
    python3 dashboard.py audit.sqlite > out.html
"""

import html
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

# stessa palette di charts.py e del poster: teal, oro, antracite
INK, TEAL, GOLD, GREY, HAIR, WARM = (
    "#14161A", "#0D7A7A", "#C8992E", "#6E7377", "#D8DBDE", "#B4451F")


def fetch(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in
                db.execute("SELECT * FROM audit ORDER BY ts_utc")]
    return rows


def aggregate(rows):
    if not rows:
        return None
    tot_j = sum(r["energy_joules"] or 0 for r in rows)
    tot_eur = sum(r["cost_eur"] or 0 for r in rows)
    tot_tok = sum(r["tokens_out"] or 0 for r in rows)
    ext = sum(1 for r in rows if r["egress"] == "external")

    by_tenant = {}
    for r in rows:
        t = r["tenant"] or "default"
        d = by_tenant.setdefault(t, {"n": 0, "tok": 0, "j": 0.0, "eur": 0.0,
                                     "strict": 0, "ext": 0})
        d["n"] += 1
        d["tok"] += r["tokens_out"] or 0
        d["j"] += r["energy_joules"] or 0
        d["eur"] += r["cost_eur"] or 0
        if r["privacy"] == "strict":
            d["strict"] += 1
        if r["egress"] == "external":
            d["ext"] += 1

    by_conc = {}
    for r in rows:
        c = r["concurrency"] or 1
        d = by_conc.setdefault(c, {"n": 0, "tok": 0, "j": 0.0})
        d["n"] += 1
        d["tok"] += r["tokens_out"] or 0
        d["j"] += r["energy_joules"] or 0

    return {
        "rows": rows,
        "n": len(rows),
        "tokens": tot_tok,
        "joules": tot_j,
        "kwh": tot_j / 3_600_000,
        "eur": tot_eur,
        "external": ext,
        "by_tenant": by_tenant,
        "by_conc": by_conc,
        "source": rows[-1].get("cost_model_source") or "—",
        "span_s": (rows[-1]["ts_utc"] - rows[0]["ts_utc"]) if len(rows) > 1 else 0,
    }


def sparkline(rows, width=760, height=90):
    """Joule cumulativi nel tempo. Il contatore che gira, appunto."""
    if len(rows) < 2:
        return ""
    t0, t1 = rows[0]["ts_utc"], rows[-1]["ts_utc"]
    span = max(t1 - t0, 1e-6)
    cum, pts = 0.0, []
    for r in rows:
        cum += r["energy_joules"] or 0
        pts.append(((r["ts_utc"] - t0) / span, cum))
    top = max(p[1] for p in pts) or 1
    coords = " ".join(
        f"{x*width:.1f},{height - (y/top)*(height-8):.1f}" for x, y in pts)
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="spark" role="img" aria-label="cumulative joules over time">'
        f'<polyline points="{coords}" fill="none" stroke="{TEAL}" '
        f'stroke-width="2" vector-effect="non-scaling-stroke"/>'
        f'<polyline points="0,{height} {coords} {width},{height}" '
        f'fill="{TEAL}" opacity="0.08" stroke="none"/></svg>')


def conc_bars(by_conc):
    """Dove sono finiti i joule, per livello di concorrenza.

    Il punto operativo: le richieste servite a bassa concorrenza costano
    fino a 8.7x per token. Se la massa e' a sinistra, si sta sprecando.
    """
    if not by_conc:
        return ""
    order = sorted(by_conc)
    top = max(d["j"] for d in by_conc.values()) or 1
    out = []
    for c in order:
        d = by_conc[c]
        pct = d["j"] / top * 100
        tpj = d["tok"] / d["j"] if d["j"] else 0
        col = WARM if c <= 2 else (GOLD if c <= 4 else TEAL)
        out.append(
            f'<div class="cbar"><div class="clab">conc {c}</div>'
            f'<div class="ctrack"><div class="cfill" style="width:{pct:.1f}%;'
            f'background:{col}"></div></div>'
            f'<div class="cval">{d["j"]:,.0f} J<span> · {tpj:.2f} tok/J · '
            f'{d["n"]} req</span></div></div>')
    return "".join(out)


def render(db_path, eur_per_kwh=0.25):
    rows = fetch(db_path)
    a = aggregate(rows)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if not a:
        body = ('<div class="empty"><h2>No requests yet</h2>'
                '<p>Send one through the gateway and it will appear here, '
                'with what it cost.</p>'
                '<code>curl localhost:8090/v1/chat/completions -H '
                "'X-Privacy: strict' ...</code></div>")
        return (PAGE.replace("@@BODY@@", body).replace("@@NOW@@", now)
                    .replace("@@SOURCE@@", "—"))

    # tenant ordinati per spesa
    tenants = sorted(a["by_tenant"].items(), key=lambda kv: -kv[1]["eur"])
    trows = "".join(
        f'<tr><td class="tn">{html.escape(t)}</td>'
        f'<td>{d["n"]:,}</td><td>{d["tok"]:,}</td>'
        f'<td>{d["j"]:,.0f}</td><td class="eur">€{d["eur"]:.6f}</td>'
        f'<td>{d["strict"]}/{d["n"]}</td>'
        f'<td class="{"bad" if d["ext"] else "ok"}">{d["ext"]}</td></tr>'
        for t, d in tenants)

    recent = "".join(
        f'<tr><td class="mono">{datetime.fromtimestamp(r["ts_utc"], timezone.utc).strftime("%H:%M:%S")}</td>'
        f'<td class="tn">{html.escape(r["tenant"] or "default")}</td>'
        f'<td class="mono">{r["concurrency"]}</td>'
        f'<td class="mono">{r["tokens_out"] or 0:,}</td>'
        f'<td class="mono">{r["energy_joules"] or 0:,.1f} J</td>'
        f'<td class="mono eur">€{r["cost_eur"] or 0:.8f}</td>'
        f'<td class="mono">{html.escape(r["privacy"] or "")}</td>'
        f'<td class="{"bad" if r["egress"]=="external" else "ok"}">'
        f'{html.escape(r["egress"] or "")}</td></tr>'
        for r in reversed(rows[-14:]))

    avg_tpj = a["tokens"] / a["joules"] if a["joules"] else 0
    # 4.142 tok/J e' il massimo misurato (7B, concorrenza 16). Il valore
    # effettivo e' sempre piu' basso: dichiararlo evita che sembri una
    # contraddizione con i numeri del README.
    eff_pct = avg_tpj / 4.142 * 100
    # quanto si risparmierebbe servendo tutto a concorrenza 16 (4.142 tok/J)
    ideal_j = a["tokens"] / 4.142 if a["tokens"] else 0
    waste = max(a["joules"] - ideal_j, 0)
    waste_eur = waste / 3_600_000 * eur_per_kwh

    body = f'''
<section class="meter">
  <div class="mlab">Energy metered · cumulative</div>
  <div class="mrow">
    <div class="mbig">{a["joules"]:,.0f}<span> J</span></div>
    <div class="mside">
      <div><b>{a["kwh"]:.6f}</b> kWh</div>
      <div><b>€{a["eur"]:.6f}</b> at {eur_per_kwh} EUR/kWh</div>
    </div>
  </div>
  {sparkline(rows)}
</section>

<section class="kpis">
  <div class="k"><div class="kv">{a["n"]:,}</div><div class="kl">requests</div></div>
  <div class="k"><div class="kv">{a["tokens"]:,}</div><div class="kl">tokens generated</div></div>
  <div class="k"><div class="kv">{avg_tpj:.2f}</div>
    <div class="kl">tokens per joule, effective<br>
    <span class="hint">{eff_pct:.0f}% of the 4.14 measured at concurrency 16</span></div></div>
  <div class="k egress {"bad" if a["external"] else "ok"}">
    <div class="kv">{a["external"]}</div>
    <div class="kl">requests with external egress</div></div>
</section>

<section class="panel">
  <h2>Where the joules went</h2>
  <p class="sub">Requests served at low concurrency cost up to 8.7× more per
     token. Mass on the left is capacity being wasted.</p>
  {conc_bars(a["by_conc"])}
  <p class="note">Serving this same {a["tokens"]:,} tokens entirely at
     concurrency 16 would have taken <b>{ideal_j:,.0f} J</b> —
     {waste:,.0f} J and €{waste_eur:.6f} of headroom.</p>
</section>

<section class="panel">
  <h2>Cost by tenant</h2>
  <table>
    <thead><tr><th>tenant</th><th>requests</th><th>tokens</th>
      <th>joules</th><th>cost</th><th>privacy strict</th>
      <th>external egress</th></tr></thead>
    <tbody>{trows}</tbody>
  </table>
</section>

<section class="panel">
  <h2>Recent requests</h2>
  <table class="dense">
    <thead><tr><th>time</th><th>tenant</th><th>conc</th><th>tokens</th>
      <th>energy</th><th>cost</th><th>privacy</th><th>egress</th></tr></thead>
    <tbody>{recent}</tbody>
  </table>
</section>'''

    return (PAGE.replace("@@BODY@@", body).replace("@@NOW@@", now)
            .replace("@@SOURCE@@", html.escape(a["source"])))


PAGE = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>costbench · gateway</title>
<style>
:root{{--ink:{INK};--teal:{TEAL};--gold:{GOLD};--grey:{GREY};
       --hair:{HAIR};--warm:{WARM};--bg:#FAFBFB;
       --data:"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
       --body:"Helvetica Neue",Helvetica,Arial,sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);font-family:var(--body);
     font-size:14px;line-height:1.5}}
.wrap{{max-width:960px;margin:0 auto;padding:0 24px 64px}}

header{{background:var(--ink);color:#fff;margin-bottom:28px}}
header .wrap{{padding-top:26px;padding-bottom:22px}}
.brand{{font-size:22px;font-weight:700;letter-spacing:-.02em}}
.tag{{font-family:var(--data);font-size:10px;letter-spacing:.18em;
     text-transform:uppercase;color:#9BA1A6;margin-top:6px}}
.src{{font-family:var(--data);font-size:10.5px;color:#9BA1A6;margin-top:14px;
     padding-top:12px;border-top:1px solid #2C3136;word-break:break-all}}
.src b{{color:var(--gold);font-weight:400}}

.meter{{background:#fff;border:1px solid var(--hair);padding:22px 24px 8px;
       margin-bottom:18px}}
.mlab{{font-family:var(--data);font-size:10px;letter-spacing:.16em;
      text-transform:uppercase;color:var(--grey);margin-bottom:14px}}
.mrow{{display:flex;align-items:baseline;gap:28px;flex-wrap:wrap}}
.mbig{{font-family:var(--data);font-size:52px;font-weight:700;
      letter-spacing:-.04em;line-height:1;color:var(--teal)}}
.mbig span{{font-size:20px;color:var(--grey);font-weight:400}}
.mside{{font-family:var(--data);font-size:12.5px;color:var(--grey);
       line-height:1.9}}
.mside b{{color:var(--ink);font-weight:700}}
.spark{{width:100%;height:78px;margin-top:16px;display:block}}

.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
      background:var(--hair);border:1px solid var(--hair);margin-bottom:18px}}
.k{{background:#fff;padding:16px 18px}}
.kv{{font-family:var(--data);font-size:26px;font-weight:700;
    letter-spacing:-.03em;line-height:1}}
.kl{{font-size:11.5px;color:var(--grey);margin-top:6px}}
.hint{{font-size:10.5px;color:var(--grey);opacity:.75}}
.k.ok .kv{{color:var(--teal)}}
.k.bad .kv{{color:var(--warm)}}
.k.egress.ok::after{{content:"air-gap intact";display:block;
  font-family:var(--data);font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--teal);margin-top:8px}}

.panel{{background:#fff;border:1px solid var(--hair);padding:22px 24px;
       margin-bottom:18px}}
.panel h2{{font-size:16px;font-weight:600;letter-spacing:-.01em}}
.panel .sub{{font-size:12.5px;color:var(--grey);margin:6px 0 18px}}
.note{{font-size:12.5px;color:var(--grey);margin-top:16px;padding-top:14px;
      border-top:1px solid var(--hair)}}
.note b{{font-family:var(--data);color:var(--ink)}}

.cbar{{display:grid;grid-template-columns:64px 1fr 240px;gap:12px;
      align-items:center;margin-bottom:9px}}
.clab{{font-family:var(--data);font-size:11.5px;color:var(--grey)}}
.ctrack{{height:16px;background:#F0F2F3}}
.cfill{{height:100%}}
.cval{{font-family:var(--data);font-size:11.5px;text-align:right}}
.cval span{{color:var(--grey)}}

table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{font-family:var(--data);font-size:10px;letter-spacing:.1em;
   text-transform:uppercase;color:var(--grey);text-align:right;
   padding:0 0 9px;font-weight:400;border-bottom:1px solid var(--hair)}}
th:first-child{{text-align:left}}
td{{padding:8px 0;text-align:right;border-bottom:1px solid #EEF0F1;
   font-family:var(--data)}}
td:first-child{{text-align:left}}
.tn{{font-family:var(--body);font-weight:600}}
.eur{{color:var(--teal)}}
td.ok{{color:var(--teal)}}
td.bad{{color:var(--warm);font-weight:700}}
.dense td{{padding:5px 0;font-size:11.5px}}

.empty{{background:#fff;border:1px solid var(--hair);padding:56px 24px;
       text-align:center}}
.empty h2{{font-size:17px;margin-bottom:8px}}
.empty p{{color:var(--grey);margin-bottom:18px}}
.empty code{{font-family:var(--data);font-size:11.5px;background:#F0F2F3;
            padding:8px 12px;display:inline-block;color:var(--grey)}}

footer{{font-size:11.5px;color:var(--grey);text-align:center;padding-top:8px}}
footer b{{font-family:var(--data);font-weight:400;color:var(--ink)}}

@media (max-width:720px){{
  .kpis{{grid-template-columns:repeat(2,1fr)}}
  .cbar{{grid-template-columns:56px 1fr;grid-template-areas:"l t" "v v"}}
  .clab{{grid-area:l}}.ctrack{{grid-area:t}}
  .cval{{grid-area:v;text-align:left}}
  .mbig{{font-size:38px}}
  table{{font-size:11px}}
}}
</style></head>
<body>
<header><div class="wrap">
  <div class="brand">costbench · gateway</div>
  <div class="tag">on-prem inference, metered in measured joules</div>
  <div class="src">cost model: <b>@@SOURCE@@</b></div>
</div></header>
<div class="wrap">
@@BODY@@
<footer>Generated @@NOW@@ · GPU board power only; excludes CPU, RAM,
cooling and PUE · <b>every figure derives from a measurement in proofs/</b>
</footer>
</div></body></html>'''


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("uso: python3 dashboard.py audit.sqlite > out.html",
              file=sys.stderr)
        sys.exit(1)
    print(render(sys.argv[1]))
