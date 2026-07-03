#!/usr/bin/env bash
#
# monitor_test.sh - Monitora il test esteso in tempo reale
#
# Uso: ./monitor_test.sh
# Esegue ogni 15 secondi fino a quando non viene interrotto

LOGFILE="trade_arb_log.jsonl"
INTERVAL=15

echo "📊 MONITORAGGIO TEST ESTESO - Aggiornamento ogni ${INTERVAL}s"
echo "Premi Ctrl+C per fermare il monitoraggio (il bot continuerà)"
echo ""

COUNT=0

while true; do
    COUNT=$((COUNT + 1))
    
    # Conta i segnali
    SIGNAL_COUNT=$(grep -c '"type":"signal"' "$LOGFILE" 2>/dev/null || echo 0)
    TRADE_COUNT=$(grep -c '"type":"dry_run_trade"' "$LOGFILE" 2>/dev/null || echo 0)
    
    # Calcola il profitto totale
    if [ "$TRADE_COUNT" -gt 0 ]; then
        TOTAL_PROFIT=$(grep '"type":"dry_run_trade"' "$LOGFILE" | python3 -c "
import sys, json
total = 0
for line in sys.stdin:
    try:
        obj = json.loads(line)
        total += obj.get('expectedProfit', 0)
    except:
        pass
print(f'{total:.2f}')
")
    else
        TOTAL_PROFIT="0.00"
    fi
    
    # Timestamp
    TIMESTAMP=$(date "+%H:%M:%S")
    
    # Display
    echo "[$TIMESTAMP] Check #$COUNT"
    echo "  📈 Segnali: $SIGNAL_COUNT"
    echo "  💰 Profit Totale Atteso: \$$TOTAL_PROFIT"
    echo "  ⏱️  Tempo Trascorso: ${COUNT}*${INTERVAL}s = $((COUNT * INTERVAL))s"
    
    # Mostra ultimi 2 segnali se ce ne sono
    if [ "$SIGNAL_COUNT" -gt 0 ]; then
        echo "  📋 Ultimi segnali:"
        grep '"type":"signal"' "$LOGFILE" | tail -2 | python3 -c "
import sys, json
for i, line in enumerate(sys.stdin, 1):
    try:
        obj = json.loads(line)
        token = obj.get('tokenName', '?')
        ask = obj.get('bestAsk', 0)
        fv = obj.get('fairValue', 0)
        disc = obj.get('discount', 0)
        t = obj.get('secondsIntoWindow', 0)
        print(f'     {i}. {token:4s} @ \${ask:.3f} (fv=\${fv:.3f}, disc=\${disc:.3f}, t={t}s)')
    except:
        pass
" || true
    fi
    
    echo ""
    sleep "$INTERVAL"
done
