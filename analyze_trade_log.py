#!/usr/bin/env python3
"""
analyze_trade_log.py — analisi del rendimento e della calibrazione del bot
trade_btc_momentum.js a partire dal suo trade_log.jsonl.

Risponde a tre domande:
  1) RENDIMENTO  : win rate reale e P&L cumulato dei segnali riconciliati.
  2) CALIBRAZIONE: la probabilita' stimata (PROB_TABLE) coincide col win rate
                   reale? (il modello e' onesto o promette piu' di quanto mantiene?)
  3) DOVE STA L'EDGE: win rate reale per timing d'ingresso (secondsIntoWindow),
                   per body_pct e per fascia di ask.

Uso:
    python3 analyze_trade_log.py                  # legge ./trade_log.jsonl
    python3 analyze_trade_log.py --log path.jsonl
    python3 analyze_trade_log.py --since 1782800000   # solo segnali dopo questo ts (sec)
    python3 analyze_trade_log.py --late-only          # solo ingressi >=180s (post-filtro)

Nessuna dipendenza esterna (solo standard library).
"""

import argparse
import json
import statistics
import sys
import os
import csv

INTERVAL_SEC = 300  # finestra 5 min
LATE_THRESHOLD = 180  # soglia (s) per --late-only (ultimi 180s)


def load(path):
    """Carica il log e ritorna (signals_per_slug, reconciled_list)."""
    signals = {}     # slug -> ultimo evento 'signal' (ha bodyPct, ask, prob, secondsIntoWindow)
    reconciled = []  # eventi 'reconciled' (hanno won, realPnl, esito reale)
    n_lines = 0
    n_bad = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_lines += 1
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            t = o.get("type")
            if t == "signal":
                signals[o["slug"]] = o
            elif t == "reconciled":
                reconciled.append(o)
    return signals, reconciled, n_lines, n_bad


def window_start(slug):
    try:
        return int(slug.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return None


def safe_float(v):
    """Converte in float ignorando None/valori non convertibili."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_output(path, rows):
    """Esporta `rows` in jsonl/json/csv a seconda dell'estensione."""
    _, ext = os.path.splitext(path.lower())
    if ext == ".csv":
        fieldnames = ["slug", "won", "pnl", "body", "ask", "prob", "secs", "live"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                # assicurarsi che non ci siano valori non scrivibili
                out = {k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames}
                w.writerow(out)
    elif ext == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
    else:
        # default: jsonl (one JSON per riga)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def join_rows(signals, reconciled, since=None, late_only=False):
    """Unisce ogni esito riconciliato col suo segnale, per avere insieme
    body_pct/ask/timing e l'esito reale."""
    rows = []
    for r in reconciled:
        s = signals.get(r["slug"])
        if not s:
            continue
        ws = window_start(r["slug"])
        if since is not None and ws is not None and ws < since:
            continue
        # secondsIntoWindow: dal log se presente (nuovi run), altrimenti ricavato
        secs = s.get("secondsIntoWindow")
        if secs is None and ws is not None and "loggedAtMs" in s:
            secs = s["loggedAtMs"] / 1000.0 - ws
        if late_only and (secs is None or secs < LATE_THRESHOLD):
            continue
        rows.append({
            "slug": r["slug"],
            "won": bool(r["won"]),
            "pnl": float(r.get("realPnl", 0.0)),
            "body": safe_float(s.get("bodyPct")),
            "ask": safe_float(s.get("ask")),
            "prob": safe_float(s.get("prob")),
            "secs": secs,
            "live": r.get("live", False),
        })
    return rows


def bucket_report(title, rows, key, edges, fmt="{:.2f}"):
    """Stampa win rate / P&L reale per fasce di `key`."""
    print(f"\n=== {title} ===")
    vals = [x for x in rows if x.get(key) is not None]
    if not vals:
        print("  (nessun dato per questo campo — forse log da run vecchi senza il campo)")
        return
    for lo, hi in edges:
        sub = [x for x in vals if lo <= x[key] < hi]
        if not sub:
            continue
        wr = sum(1 for x in sub if x["won"]) / len(sub)
        pnl = sum(x["pnl"] for x in sub)
        extra = ""
        if key != "ask":
            asks = [x["ask"] for x in sub if x["ask"] is not None]
            if asks:
                extra = f"  breakeven~{statistics.mean(asks):.3f}"
        print(f"  [{fmt.format(lo)},{fmt.format(hi)}): n={len(sub):3d}  "
              f"winrate={wr:.3f}{extra}  pnl=${pnl:+.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="trade_log.jsonl", help="percorso del log (default ./trade_log.jsonl)")
    ap.add_argument("--since", type=int, default=None, help="solo finestre con window-start >= questo timestamp (sec)")
    ap.add_argument("--late-only", action="store_true", help="solo ingressi negli ultimi 120s (post-filtro)")
    ap.add_argument("--output", default=None, help="percorso file di export (.jsonl, .json, .csv)")
    args = ap.parse_args()

    try:
        signals, reconciled, n_lines, n_bad = load(args.log)
    except FileNotFoundError:
        sys.exit(f"Log non trovato: {args.log}")

    rows = join_rows(signals, reconciled, since=args.since, late_only=args.late_only)

    print(f"Log: {args.log}  ({n_lines} righe, {len(signals)} segnali, {len(reconciled)} riconciliati)")
    if n_bad:
        print(f"  righe scartate (JSON malformato): {n_bad}")
    if args.since:
        print(f"Filtro --since {args.since}")
    if args.late_only:
        print(f"Filtro --late-only (solo ingressi >={LATE_THRESHOLD}s)")
    print(f"Trade analizzati (riconciliati + uniti al segnale): {len(rows)}")

    if not rows:
        sys.exit("\nNessun trade riconciliato da analizzare ancora. Lascia girare il bot e riprova.")

    # 1) RENDIMENTO
    won = [x for x in rows if x["won"]]
    pnl = sum(x["pnl"] for x in rows)
    wr = len(won) / len(rows)
    probs = [x["prob"] for x in rows if x["prob"] is not None]
    avg_prob = statistics.mean(probs) if probs else float("nan")

    print("\n========== 1) RENDIMENTO REALE ==========")
    print(f"  Win rate reale   : {wr:.3f}  ({len(won)}/{len(rows)})")
    print(f"  P&L cumulato     : ${pnl:+.2f}")
    print(f"  P&L medio/trade  : ${pnl/len(rows):+.4f}")
    live = [x for x in rows if x["live"]]
    if live:
        lpnl = sum(x["pnl"] for x in live)
        print(f"  di cui LIVE      : {len(live)} trade, P&L ${lpnl:+.2f}")

    # 2) CALIBRAZIONE
    print("\n========== 2) CALIBRAZIONE MODELLO ==========")
    print(f"  Prob media STIMATA : {avg_prob:.3f}   (quanto il modello dice di vincere)")
    print(f"  Win rate REALE     : {wr:.3f}")
    gap = avg_prob - wr
    print(f"  Scarto             : {gap:+.3f}", end="  ")
    if abs(gap) <= 0.03:
        print("-> ben calibrato.")
    elif gap > 0.03:
        print("-> il modello PROMETTE PIU' di quanto mantiene (overconfident). Non aumentare i size.")
    else:
        print("-> il modello e' PRUDENTE (vince piu' del previsto).")

    # 3) DOVE STA L'EDGE
    bucket_report("3a) WIN RATE per TIMING d'ingresso (secondi nella finestra)",
                  rows, "secs",
                  [(0, 30), (30, 60), (60, 120), (120, 180), (180, 240), (240, 300), (300, 9999)],
                  fmt="{:.0f}")
    bucket_report("3b) WIN RATE per BODY_PCT (ampiezza movimento)",
                  rows, "body",
                  [(0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.5), (0.5, 100)],
                  fmt="{:.2f}")
    bucket_report("3c) WIN RATE per ASK pagato (confronta col breakeven = ask)",
                  rows, "ask",
                  [(0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)],
                  fmt="{:.1f}")

    print("\nNota: il win rate di breakeven a un dato ask è ~ask stesso (compri a ask, "
          "vinci 1). Un bucket è profittevole solo se winrate > breakeven.")

    # Export dei rows se richiesto
    if args.output:
        try:
            write_output(args.output, rows)
            print(f"\nExport: scritto {len(rows)} righe in {args.output}")
        except Exception as e:
            print(f"\nErrore export su {args.output}: {e}")


if __name__ == "__main__":
    main()

