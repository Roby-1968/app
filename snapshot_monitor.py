#!/usr/bin/env python3
"""
snapshot_monitor.py - Snapshot dei progressi ogni 5 minuti
Esegui in un loop: while true; do python3 snapshot_monitor.py; sleep 300; done
"""

import json
import os
from datetime import datetime
from collections import defaultdict

LOGFILE = "trade_arb_log.jsonl"

def load_and_analyze():
    """Carica il log e calcola statistiche."""
    entries = []
    if not os.path.exists(LOGFILE):
        return None
    
    with open(LOGFILE) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except:
                pass
    
    if not entries:
        return None
    
    signals = [e for e in entries if e.get("type") == "signal"]
    trades = [e for e in entries if e.get("type") == "dry_run_trade"]
    
    if not signals:
        return None
    
    # Statistiche
    asks = [s["bestAsk"] for s in signals]
    discounts = [s["discount"] for s in signals]
    profits = [t.get("expectedProfit", 0) for t in trades]
    
    # Token split
    token_counts = defaultdict(int)
    for sig in signals:
        token_counts[sig.get("tokenName", "?")] += 1
    
    return {
        "count": len(signals),
        "total_profit": sum(profits),
        "ask_avg": sum(asks) / len(asks),
        "ask_min": min(asks),
        "ask_max": max(asks),
        "discount_avg": sum(discounts) / len(discounts),
        "discount_pct_avg": (sum(discounts) / len(discounts)) * 100,
        "signals": signals,
        "token_counts": dict(token_counts),
    }

def main():
    data = load_and_analyze()
    
    print("\n" + "="*70)
    print(f"📸 SNAPSHOT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    if not data:
        print("⏳ Nessun segnale ancora...")
        return
    
    print(f"\n📊 SEGNALI: {data['count']}")
    print(f"💰 PROFITTO TOTALE: ${data['total_profit']:.2f}")
    print(f"📈 PROFITTO MEDIO: ${data['total_profit']/data['count']:.2f}")
    
    print(f"\n📉 ASK PRICE (Ingresso):")
    print(f"   Media: ${data['ask_avg']:.3f}")
    print(f"   Range: ${data['ask_min']:.3f} - ${data['ask_max']:.3f}")
    
    print(f"\n🎯 DISCOUNT (Mispricing):")
    print(f"   Media: ${data['discount_avg']:.4f} ({data['discount_pct_avg']:.1f}%)")
    
    print(f"\n🔄 DISTRIBUZIONE TOKEN:")
    for token, count in sorted(data['token_counts'].items()):
        pct = (count / data['count']) * 100
        print(f"   {token}: {count} ({pct:.1f}%)")
    
    print(f"\n📋 ULTIMI 5 SEGNALI:")
    for i, sig in enumerate(data['signals'][-5:], 1):
        token = sig.get("tokenName", "?")
        ask = sig.get("bestAsk", 0)
        disc = sig.get("discount", 0)
        t = sig.get("secondsIntoWindow", 0)
        slug = sig.get("slug", "?")[-15:]  # ultimi 15 char dello slug
        
        print(f"   {i}. {token:4s} @ ${ask:.3f} (disc=${disc:.3f}, t={t}s) [{slug}]")
    
    print("\n" + "="*70)

if __name__ == "__main__":
    main()
