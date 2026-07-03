#!/usr/bin/env python3
"""
realtime_monitor.py - Monitora il test in tempo reale (ogni 20 secondi)
"""

import json
import time
import os
from datetime import datetime
from collections import defaultdict

LOGFILE = "trade_arb_log.jsonl"
INTERVAL = 20  # secondi tra aggiornamenti
RUNTIME = 3 * 3600  # 3 ore in secondi

def load_entries():
    """Legge tutte le righe dal log."""
    entries = []
    if not os.path.exists(LOGFILE):
        return entries, 0, 0
    
    with open(LOGFILE) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except:
                pass
    
    signals = [e for e in entries if e.get("type") == "signal"]
    trades = [e for e in entries if e.get("type") == "dry_run_trade"]
    
    return entries, len(signals), len(trades)

def calc_stats(signals):
    """Calcola statistiche dai segnali."""
    if not signals:
        return None
    
    asks = [s["bestAsk"] for s in signals]
    discounts = [s["discount"] for s in signals]
    
    stats = {
        "count": len(signals),
        "ask_avg": sum(asks) / len(asks),
        "ask_min": min(asks),
        "ask_max": max(asks),
        "discount_avg": sum(discounts) / len(discounts),
        "discount_pct_avg": (sum(discounts) / len(discounts)) * 100,
    }
    return stats

def calc_profit(trades):
    """Calcola profitto totale."""
    if not trades:
        return 0
    return sum(t.get("expectedProfit", 0) for t in trades)

def monitor():
    """Loop di monitoraggio."""
    start_time = time.time()
    check_num = 0
    
    print("\n" + "="*70)
    print("🔴 MONITORAGGIO TEST ESTESO - Bot in DRY-RUN")
    print("="*70)
    print(f"Inizio: {datetime.now().strftime('%H:%M:%S')}")
    print(f"Durata prevista: 3 ore | Aggiornamento ogni {INTERVAL}s")
    print(f"Log file: {LOGFILE}\n")
    
    try:
        while True:
            check_num += 1
            now = time.time()
            elapsed = int(now - start_time)
            elapsed_min = elapsed // 60
            elapsed_sec = elapsed % 60
            
            entries, signal_count, trade_count = load_entries()
            signals = [e for e in entries if e.get("type") == "signal"]
            trades = [e for e in entries if e.get("type") == "dry_run_trade"]
            
            total_profit = calc_profit(trades)
            stats = calc_stats(signals)
            
            # Clear screen effect (simple)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Check #{check_num} - "
                  f"Tempo: {elapsed_min}m {elapsed_sec}s")
            print("-" * 70)
            
            if signal_count == 0:
                print("⏳ Nessun segnale ancora (in attesa della finestra opportuna)...")
            else:
                print(f"📊 SEGNALI GENERATI: {signal_count}")
                print(f"💰 PROFITTO TOTALE ATTESO: ${total_profit:.2f}")
                
                if stats:
                    print(f"\n📈 STATISTICHE ({signal_count} segnali):")
                    print(f"   Ask Price (medio): ${stats['ask_avg']:.3f}")
                    print(f"   Ask Price (range): ${stats['ask_min']:.3f} - ${stats['ask_max']:.3f}")
                    print(f"   Discount (medio): ${stats['discount_avg']:.4f} ({stats['discount_pct_avg']:.1f}%)")
                
                # Conteggio token
                token_counts = defaultdict(int)
                for sig in signals:
                    token_counts[sig.get("tokenName", "?")] += 1
                
                print(f"\n🎯 SEGNALI PER TOKEN:")
                for token, count in sorted(token_counts.items()):
                    pct = (count / signal_count) * 100
                    print(f"   {token}: {count} ({pct:.1f}%)")
                
                # Ultimi 3 segnali
                print(f"\n📋 ULTIMI SEGNALI:")
                for i, sig in enumerate(signals[-3:], 1):
                    token = sig.get("tokenName", "?")
                    ask = sig.get("bestAsk", 0)
                    fv = sig.get("fairValue", 0)
                    disc = sig.get("discount", 0)
                    t = sig.get("secondsIntoWindow", 0)
                    slug = sig.get("slug", "?")
                    
                    profit_marker = ""
                    if i <= len(signals) - 1:
                        # Cerca il trade corrispondente
                        for trade in trades:
                            if trade.get("slug") == slug:
                                profit = trade.get("expectedProfit", 0)
                                profit_marker = f" → ${profit:.2f}"
                                break
                    
                    print(f"   {i}. {token} @ ${ask:.3f} (fv=${fv:.3f}, disc=${disc:.4f}, t={t}s){profit_marker}")
            
            print("-" * 70)
            
            # Stima velocità
            if elapsed > 0 and signal_count > 0:
                segnali_per_min = (signal_count / elapsed) * 60
                tempo_per_segnale = elapsed / signal_count
                eta_next = tempo_per_segnale
                print(f"📊 VELOCITÀ: {segnali_per_min:.1f} segnali/min "
                      f"({tempo_per_segnale:.0f}s/segnale)")
                print(f"   Prossimo segnale atteso in ~{eta_next:.0f}s")
            
            if elapsed > RUNTIME:
                print(f"\n✅ Test completato! (durata 3 ore raggiunta)")
                break
            
            time.sleep(INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\n⏸️  Monitoraggio interrotto (il bot continua a girare)")
        
        # Report finale
        print("\n" + "="*70)
        print("📋 REPORT FINALE")
        print("="*70)
        
        if signal_count > 0:
            print(f"✅ Segnali totali: {signal_count}")
            print(f"💰 Profitto totale atteso: ${total_profit:.2f}")
            
            if stats:
                print(f"\n📊 Statistiche finali:")
                print(f"   Ask medio: ${stats['ask_avg']:.3f}")
                print(f"   Discount medio: {stats['discount_pct_avg']:.1f}%")
            
            print(f"\nEsegui: python3 analyze_arb_log.py")
            print(f"per l'analisi completa dei log.")

if __name__ == "__main__":
    monitor()
