Istruzioni per eseguire il progetto su GitHub Codespaces

1) Inizializza un repository Git e push su GitHub (se non lo è già):

   git init
   git add .
   git commit -m "Add project and devcontainer"
   git branch -M main
   git remote add origin git@github.com:<tuo-utente>/<tuo-repo>.git
   git push -u origin main

2) Apri il repository su GitHub e crea un Codespace (pulsante verde "Code" -> "Open with Codespaces" -> "New codespace").

3) All'interno del Codespace apri un terminale e avvia i processi desiderati:

   # avvia il bot in background (logs/bot.log)
   bash scripts/start_bot.sh

   # avvia il ciclo di analisi in background (export.jsonl aggiornato ogni 5m)
   nohup bash scripts/analyze_loop.sh > logs/analyze.log 2>&1 &

4) Per fermare il bot:

   # crea file KILL_SWITCH nella workspace root
   touch KILL_SWITCH

   # oppure uccidi il pid
   kill $(cat logs/bot.pid)

Note:
- Verifica che `trade_log.jsonl` e gli script `trade_btc_momentum.js` e `analyze_trade_log.py` siano presenti nella root del repository nel Codespace.
- Se vuoi che il bot giri in modalità live, rimuovi o modifica la flag DRY-RUN all'interno di `trade_btc_momentum.js` (fai attenzione!).
