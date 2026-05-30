#!/usr/bin/env python3
"""
SEED-RECOVERY Electrum 2 - GPU-accelerated seed recovery
Wallet: 16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v
Seed: 12-word Electrum 2 English
"""

import subprocess
import sys
import time
import threading
import requests
from datetime import datetime
from pathlib import Path

# ── Wallet target ──────────────────────────────────────────────────────────────
WALLET_ADDRESS = "16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v"
SEED_LENGTH    = 12
WALLET_TYPE    = "electrum2"
LANGUAGE       = "english"

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path("/app/recovery")
WORDLIST    = BASE_DIR / "electrum_wordlist.txt"
ADDRESSLIST = BASE_DIR / "addresslist.txt"
SEEDRECOVER = Path("/opt/btcrecover/seedrecover.py")

LOG_DIR  = Path("/app/logs")
LOG_FILE = LOG_DIR / f"recovery_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Telegram config ────────────────────────────────────────────────────────────
# Ottieni il token creando un bot con @BotFather su Telegram
# Ottieni il chat_id avviando il bot e visitando:
#   https://api.telegram.org/bot<TOKEN>/getUpdates
TG_TOKEN   = "7067029206:AAGTgCEARp6XfjWUXKKfZv_VDySuxp5YWWw"        # es. "123456789:AABBccDDeeFFggHH..."
TG_CHAT_ID = "5126563581"          # es. "987654321"

# Notifica periodica ogni N minuti (0 = disabilitata)
TG_HEARTBEAT_MINUTES = 30


# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_send(text: str) -> bool:
    """
    Invia un messaggio Telegram. Restituisce True se inviato con successo.
    Non solleva eccezioni per non interrompere il recovery.
    """
    if TG_TOKEN == "YOUR_BOT_TOKEN" or TG_CHAT_ID == "YOUR_CHAT_ID":
        return False  # non configurato, skip silenzioso

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log_msg(f"⚠️  Telegram error: {exc}")
        return False


def tg_heartbeat(stop_event: threading.Event, interval_sec: int) -> None:
    """Thread che invia un heartbeat periodico finché stop_event non è settato."""
    elapsed = 0
    while not stop_event.wait(timeout=60):   # controlla ogni minuto
        elapsed += 1
        if elapsed % (interval_sec // 60) == 0:
            tg_send(
                f"⏳ <b>Recovery in corso</b>\n"
                f"Wallet: <code>{WALLET_ADDRESS}</code>\n"
                f"Tempo trascorso: {elapsed} min\n"
                f"Orario: {datetime.now().strftime('%H:%M:%S')}"
            )


# ── Logging ────────────────────────────────────────────────────────────────────

def log_msg(msg: str) -> None:
    """Stampa e appende al log file con timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── File check ─────────────────────────────────────────────────────────────────

def verify_files() -> bool:
    """Ritorna True solo se tutti i file richiesti esistono."""
    log_msg("Verifying required files...")

    required = {
        WORDLIST:    "Electrum wordlist",
        ADDRESSLIST: "Address list",
        SEEDRECOVER: "btcrecover seedrecover.py",
    }

    ok = True
    for path, label in required.items():
        if path.exists():
            size = path.stat().st_size
            log_msg(f"  ✓ {label}: {path}  ({size:,} bytes)")
        else:
            log_msg(f"  ✗ ERROR: Missing {label}: {path}")
            ok = False

    return ok


def count_words() -> int:
    """Conta le righe non vuote nel wordlist."""
    with open(WORDLIST) as f:
        return sum(1 for line in f if line.strip())


# ── Recovery ───────────────────────────────────────────────────────────────────

def run_recovery() -> bool:
    n_words = count_words()

    log_msg("")
    log_msg("=" * 60)
    log_msg("Starting Electrum 2 Seed Recovery")
    log_msg("=" * 60)
    log_msg(f"Wallet address  : {WALLET_ADDRESS}")
    log_msg(f"Seed length     : {SEED_LENGTH} words")
    log_msg(f"Wallet type     : {WALLET_TYPE}")
    log_msg(f"Language        : {LANGUAGE}")
    log_msg(f"Wordlist        : {n_words:,} words  ({WORDLIST})")
    log_msg(f"Start time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_msg("=" * 60)
    log_msg("")

    # Notifica di avvio
    tg_send(
        f"🚀 <b>Recovery AVVIATO</b>\n"
        f"Wallet: <code>{WALLET_ADDRESS}</code>\n"
        f"Seed: {SEED_LENGTH} parole  |  Wordlist: {n_words:,} parole\n"
        f"Avvio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    cmd = [
        "python3", str(SEEDRECOVER),
        "--wallet-type",     WALLET_TYPE,
        "--seed-language",   LANGUAGE,
        "--addrs",           WALLET_ADDRESS,
        "--mnemonic-length", str(SEED_LENGTH),
        "--wordlist",        str(WORDLIST),
        "--addresslist",     str(ADDRESSLIST),
    ]

    log_msg(f"Command: {' '.join(cmd)}")
    log_msg("")
    log_msg("Recovery in progress… (this may take several hours)")
    log_msg("")

    seed_found  = False
    found_lines = []

    # ── Heartbeat thread ───────────────────────────────────────────────────────
    stop_event = threading.Event()
    if TG_HEARTBEAT_MINUTES > 0:
        hb_thread = threading.Thread(
            target=tg_heartbeat,
            args=(stop_event, TG_HEARTBEAT_MINUTES * 60),
            daemon=True,
        )
        hb_thread.start()
        log_msg(f"Heartbeat Telegram attivo ogni {TG_HEARTBEAT_MINUTES} min")

    start_ts = time.time()

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for raw_line in process.stdout:
            line = raw_line.rstrip()
            print(line)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")

            if "seed found" in line.lower():
                seed_found = True
                found_lines.append(line)
                log_msg("")
                log_msg("🎉  SEED FOUND!  🎉")
                log_msg("")
                # Notifica immediata seed trovato
                tg_send(
                    f"🎉 <b>SEED TROVATO!</b>\n"
                    f"Wallet: <code>{WALLET_ADDRESS}</code>\n"
                    f"<pre>{line}</pre>"
                )

        process.wait()
        exit_code = process.returncode

    except FileNotFoundError:
        log_msg(f"❌ ERROR: seedrecover.py not found at {SEEDRECOVER}")
        tg_send(f"❌ <b>ERRORE</b>: seedrecover.py non trovato in <code>{SEEDRECOVER}</code>")
        stop_event.set()
        return False
    except Exception as exc:
        log_msg(f"❌ ERROR: {exc}")
        tg_send(f"❌ <b>ERRORE</b>: {exc}")
        stop_event.set()
        return False
    finally:
        stop_event.set()   # ferma sempre il thread heartbeat

    elapsed_min = int((time.time() - start_ts) / 60)

    log_msg("")
    log_msg("=" * 60)
    log_msg(f"Recovery finished  (exit code: {exit_code})")
    log_msg(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_msg("=" * 60)
    log_msg("")

    # Notifica di fine
    if seed_found:
        result_text = "\n".join(found_lines)
        tg_send(
            f"✅ <b>Recovery COMPLETATO — SEED TROVATO</b>\n"
            f"Wallet: <code>{WALLET_ADDRESS}</code>\n"
            f"Durata: {elapsed_min} min\n"
            f"<pre>{result_text}</pre>"
        )
    else:
        tg_send(
            f"🔴 <b>Recovery COMPLETATO — seed non trovato</b>\n"
            f"Wallet: <code>{WALLET_ADDRESS}</code>\n"
            f"Exit code: {exit_code}  |  Durata: {elapsed_min} min\n"
            f"Fine: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    return seed_found or exit_code == 0


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    log_msg("Seed Recovery System Starting…")
    log_msg(f"Log file: {LOG_FILE}")
    log_msg("")

    if not verify_files():
        log_msg("❌ File verification failed — aborting.")
        tg_send(f"❌ <b>Recovery ABORTITO</b>: file mancanti — controlla i log.")
        sys.exit(1)

    success = run_recovery()
    sys.exit(0 if success else 1)


if __nam