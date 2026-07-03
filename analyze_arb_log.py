#!/usr/bin/env python3
"""
analyze_arb_log.py — Analizza i log di trade_btc_arb.js

Estrae i segnali di arbitraggio e calcola:
  1. Numero di segnali generati per token (UP/DOWN)
  2. Distribuzione degli ask (prezzi di entrata)
  3. Distribuzione dei discount (mispricing rilevati)
  4. Expected profit medio e totale
  5. Timing dei segnali nella finestra
  6. Metriche di redditività simulata

Uso:
    python3 analyze_arb_log.py                  # legge ./trade_arb_log.jsonl
    python3 analyze_arb_log.py --log path.jsonl
    python3 analyze_arb_log.py --since 1783024000  # solo segnali dopo questo ts (ms)
"""

import argparse
import json
import statistics
import sys
import os
from collections import defaultdict

def load(path):
    """Carica i segnali dal log."""
    all_entries = []
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
            
            # Raccogli tutti gli entry (signal e trade)
            if o.get("type") in ["signal", "dry_run_trade"]:
                all_entries.append(o)
    
    # Pair signal con trade successivo per lo stesso slug
    paired = []
    i = 0
    while i < len(all_entries):
        entry = all_entries[i]
        if entry.get("type") == "signal":
            # Cerca il trade successivo per lo stesso slug
            slug = entry.get("slug")
            merged = entry.copy()
            
            if i + 1 < len(all_entries):
                next_entry = all_entries[i + 1]
                if next_entry.get("type") == "dry_run_trade" and next_entry.get("slug") == slug:
                    # Merge fields from trade
                    merged.update({k: v for k, v in next_entry.items() if k not in merged})
                    i += 2
                    paired.append(merged)
                    continue
            
            i += 1
            paired.append(merged)
        else:
            i += 1
    
    return paired, n_lines, n_bad

def analyze_signals(signals, since=None):
    """Analizza i segnali e ritorna statistiche."""
    if since:
        signals = [s for s in signals if s.get("loggedAtMs", 0) >= since]
    
    if not signals:
        return None
    
    # Raggruppa per token
    by_token = defaultdict(list)
    for sig in signals:
        token = sig.get("tokenName", "UNKNOWN")
        by_token[token].append(sig)
    
    # Statistiche globali
    asks = [s["bestAsk"] for s in signals]
    discounts = [s["discount"] for s in signals]
    expected_profits = [s.get("expectedProfit", 0) for s in signals]
    timing = [s.get("secondsIntoWindow", 0) for s in signals]
    
    # Fair values
    fair_values = [s["fairValue"] for s in signals]
    
    stats = {
        "total_signals": len(signals),
        "by_token": {token: len(sigs) for token, sigs in by_token.items()},
        "ask_stats": {
            "min": min(asks),
            "max": max(asks),
            "avg": statistics.mean(asks),
            "median": statistics.median(asks),
            "stdev": statistics.stdev(asks) if len(asks) > 1 else 0,
        },
        "discount_stats": {
            "min": min(discounts),
            "max": max(discounts),
            "avg": statistics.mean(discounts),
            "median": statistics.median(discounts),
            "stdev": statistics.stdev(discounts) if len(discounts) > 1 else 0,
        },
        "profit_stats": {
            "total_expected": sum(expected_profits),
            "avg_per_signal": statistics.mean(expected_profits),
            "min": min(expected_profits),
            "max": max(expected_profits),
        },
        "timing_stats": {
            "avg_seconds_into_window": statistics.mean(timing),
            "min_seconds": min(timing),
            "max_seconds": max(timing),
            "median_seconds": statistics.median(timing),
        },
        "fair_value_stats": {
            "avg": statistics.mean(fair_values),
            "min": min(fair_values),
            "max": max(fair_values),
        },
        "signals": signals,
    }
    
    return stats

def print_report(stats):
    """Stampa il report di analisi."""
    if not stats:
        print("Nessun segnale trovato.")
        return
    
    print("\n" + "="*60)
    print("ANALISI ARBITRAGGIO BTC UP/DOWN")
    print("="*60)
    
    print(f"\n📊 SEGNALI TOTALI: {stats['total_signals']}")
    print(f"   Per token: {stats['by_token']}")
    
    print(f"\n💰 PROFITTI ATTESI")
    print(f"   Totale: ${stats['profit_stats']['total_expected']:.2f}")
    print(f"   Media per segnale: ${stats['profit_stats']['avg_per_signal']:.2f}")
    print(f"   Range: ${stats['profit_stats']['min']:.2f} - ${stats['profit_stats']['max']:.2f}")
    
    ask = stats['ask_stats']
    print(f"\n🎯 ASK (PREZZO D'INGRESSO)")
    print(f"   Media: ${ask['avg']:.3f}")
    print(f"   Mediana: ${ask['median']:.3f}")
    print(f"   Range: ${ask['min']:.3f} - ${ask['max']:.3f}")
    print(f"   Std Dev: ${ask['stdev']:.4f}")
    
    disc = stats['discount_stats']
    print(f"\n📉 DISCOUNT (MISPRICING RILEVATO)")
    print(f"   Media: ${disc['avg']:.4f} ({disc['avg']*100:.2f}%)")
    print(f"   Mediana: ${disc['median']:.4f}")
    print(f"   Range: ${disc['min']:.4f} - ${disc['max']:.4f}")
    print(f"   Std Dev: ${disc['stdev']:.5f}")
    
    timing = stats['timing_stats']
    print(f"\n⏱️  TIMING DEI SEGNALI (secondi nella finestra)")
    print(f"   Media: {timing['avg_seconds_into_window']:.1f}s")
    print(f"   Mediana: {timing['median_seconds']:.1f}s")
    print(f"   Range: {timing['min_seconds']:.0f}s - {timing['max_seconds']:.0f}s")
    
    fv = stats['fair_value_stats']
    print(f"\n💎 FAIR VALUE (STIMATO)")
    print(f"   Media: ${fv['avg']:.3f}")
    print(f"   Range: ${fv['min']:.3f} - ${fv['max']:.3f}")
    
    print(f"\n{'='*60}")
    print(f"SEGNALI DETTAGLIATI:\n")
    
    for i, sig in enumerate(stats['signals'], 1):
        token = sig['tokenName']
        ask = sig['bestAsk']
        fv = sig['fairValue']
        disc = sig['discount']
        profit = sig.get('expectedProfit', 0)
        timing = sig.get('secondsIntoWindow', 0)
        slug = sig['slug']
        
        print(f"{i}. {token:4s} | ask=${ask:.3f} | fv=${fv:.3f} | " +
              f"disc=${disc:.4f} | profit=${profit:.2f} | " +
              f"t={timing}s | {slug}")
    
    print()

def main():
    parser = argparse.ArgumentParser(
        description="Analizza i log del bot arbitraggio BTC up/down"
    )
    parser.add_argument("--log", default="trade_arb_log.jsonl", 
                       help="Path al file di log (default: trade_arb_log.jsonl)")
    parser.add_argument("--since", type=int, default=None,
                       help="Filtra segnali dopo questo timestamp (ms)")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.log):
        print(f"❌ File non trovato: {args.log}")
        sys.exit(1)
    
    print(f"📂 Leggendo log da: {args.log}")
    signals, n_lines, n_bad = load(args.log)
    print(f"   Total lines: {n_lines}, Bad JSON: {n_bad}, Signals: {len(signals)}\n")
    
    stats = analyze_signals(signals, since=args.since)
    print_report(stats)

if __name__ == "__main__":
    main()
