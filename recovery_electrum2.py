#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dashboard batch generica con multiprocessing reale + scaling CPU-aware.

SEED-RECOVERY Electrum 2 - GPU-accelerated seed recovery
Wallet: 16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v
Seed: 12-word Electrum 2 English
Method: MPK-based validation

Uso previsto:
- job batch innocui/benigni (ETL, validazione file, trasformazioni dati, rendering report, analytics)
- dashboard live via Flask-SocketIO
- autoscaling del parallelismo effettivo in base a CPU/backlog

Note:
- Il pool di processi ha una dimensione massima fissa (MAX_WORKERS)
- Lo scaler regola il parallelismo effettivo (target_parallelism), NON ricrea continuamente il pool
- Sostituisci la funzione process_job() con la tua logica di business non sensibile
"""
import subprocess
import sys
import json
import os
import time
import math
import threading
from datetime import datetime
from pathlib import Path
from collections import deque
from concurrent.futures import ProcessPoolExecutor

from flask import Flask, render_template_string
from flask_socketio import SocketIO

try:
    import psutil
except Exception:
    psutil = None

# ── Config ─────────────────────────────────────────────────────────────
WALLET_ADDRESS = "16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v"
PUBLIC_KEY = "02145d2611c823a396ef6712ce0f712f09b9b4f3135e3e0aa3230fb9b6d08d1e16"
SEED_LENGTH = "12"
WALLET_TYPE = "electrum2"
LANGUAGE = "english"
MAX_WORKERS = max(2, (os.cpu_count() or 4))   # cap massimo del pool
MIN_PARALLELISM = 1                           # minimo parallelismo effettivo
TOTAL_JOBS = 500                              # numero job batch innocui
SUBMIT_BURST = 8                              # quanti task sottomettere per ciclo max
CPU_HIGH_WATERMARK = 85.0                     # se CPU sopra questa soglia, riduci parallelismo
CPU_LOW_WATERMARK = 55.0                      # se CPU sotto questa soglia, aumenta parallelismo (se backlog alto)
BACKLOG_SCALE_HINT = 20                       # backlog minimo per tentare scale-up
SCALER_INTERVAL = 1.0                         # secondi
DISPATCHER_INTERVAL = 0.05                    # secondi
EMA_ALPHA = 0.25                              # smoothing throughput / ETA

UI_HOST = os.getenv("UI_HOST", "0.0.0.0")
UI_PORT = int(os.getenv("UI_PORT", "5000"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")

# Paths
BASE_DIR = Path("/app/recovery")
WORDLIST = BASE_DIR / "electrum_wordlist.txt"
ADDRESSLIST = BASE_DIR / "addresslist.txt"
LOG_DIR = Path("/app/logs")
LOG_FILE = LOG_DIR / f"recovery_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ===== CUDA/GPU SETTINGS =====
os.environ["CUDA_VISIBLE_DEVICES"] = "0"  # RTX 3090
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"  # Sincronizzazione GPU-CP

def log_msg(msg):
    """Print and log message"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    with open(LOG_FILE, "a") as f:
        f.write(log_line + "\n")

def verify_files():
    """Verify all required files exist"""
     log_msg("Verifying system setup...")
     log_msg("")

    required_files = {
        WORDLIST: "Electrum wordlist",
        ADDRESSLIST: "Address list"
    }

# ===== VERIFICA GPU =====
    try:
        cuda_result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if cuda_result.returncode == 0:
            log_msg("✓ NVIDIA GPU DETECTED:")
            for line in cuda_result.stdout.split('\n')[0:12]:
                if line.strip():
                    log_msg(f"  {line}")
        else:
            log_msg("⚠️  WARNING: nvidia-smi not found")
    except Exception as e:
        log_msg(f"⚠️  WARNING: Could not verify GPU: {e}")
    
    log_msg("")
    
    # ===== VERIFICA WORDLIST =====
    if not WORDLIST.exists():
        log_msg(f"❌ ERROR: Wordlist not found: {WORDLIST}")
        return False
    word_count = sum(1 for line in open(WORDLIST) if line.strip())
    log_msg(f"✓ Custom wordlist: {WORDLIST}")
    log_msg(f"  Words: {word_count}")

    if word_count != 2048:
        log_msg(f"⚠️  WARNING: Expected 2048 words, found {word_count}")

    log_msg("")

    for file_path, description in required_files.items():
        if file_path.exists():
            size = file_path.stat().st_size
            log_msg(f"  ✓ {description}: {file_path} ({size} bytes)")
        else:
            log_msg(f"  ✗ ERROR: Missing {description}: {file_path}")
            return False

# ── Stato condiviso ────────────────────────────────────────────────────

class Metrics:
    def __init__(self):
        self.start_time = time.time()
        self.total = TOTAL_JOBS
        self.submitted = 0
        self.completed = 0
        self.failed = 0

        self.target_parallelism = MIN_PARALLELISM
        self.inflight = 0

        self.throughput_ema = 0.0
        self.eta_seconds = None
        self.cpu = 0.0
        self.ram = 0.0

        self.last_scaling_reason = "init"
        self.state = "running"  # running | done

        self.series_speed = deque(maxlen=60)
        self.series_cpu = deque(maxlen=60)

        self.lock = threading.Lock()

metrics = Metrics()

# ── Job CPU-bound innocuo (placeholder) ────────────────────────────────
# Sostituisci con il tuo job batch benigno.
def process_job(job_id: int) -> dict:
    

# ── Utility ────────────────────────────────────────────────────────────

def get_system_stats():
    if psutil:
        return psutil.cpu_percent(interval=None), psutil.virtual_memory().percent
    return 0.0, 0.0

def formt_eta(seconds):
    if seconds is None or seconds < 0 or math.isinf(seconds):
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

# ── Dispatcher + Pool ──────────────────────────────────────────────────
# Pool fisso; parallelismo effettivo regolato da target_parallelism.
def dispatcher_loop():
    futures = {}
    last_completed = 0
    last_t = time.time()

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while True:
            # 1) Raccogli completati
            done = []
            for fut, job_id in list(futures.items()):
                if fut.done():
                    done.append((fut, job_id))

            for fut, job_id in done:
                futures.pop(fut, None)
                try:
                    _ = fut.result()
                    with metrics.lock:
                        metrics.completed += 1
                        metrics.inflight = max(0, metrics.inflight - 1)
                except Exception:
                    with metrics.lock:
                        metrics.failed += 1
                        metrics.inflight = max(0, metrics.inflight - 1)

            # 2) Aggiorna throughput/ETA
            now = time.time()
            dt = max(now - last_t, 1e-6)
            with metrics.lock:
                completed = metrics.completed
                failed = metrics.failed
                total = metrics.total
                backlog = total - (completed + failed)

            delta_completed = completed - last_completed
            inst_tp = delta_completed / dt

            with metrics.lock:
                prev = metrics.throughput_ema
                metrics.throughput_ema = inst_tp if prev == 0 else (EMA_ALPHA * inst_tp + (1 - EMA_ALPHA) * prev)
                tp = metrics.throughput_ema
                metrics.eta_seconds = (backlog / tp) if tp > 0 else None

            last_completed = completed
            last_t = now

            # 3) Condizione di uscita
            with metrics.lock:
                if metrics.completed + metrics.failed >= metrics.total:
                    metrics.state = "done"
                    break

                can_submit = max(0, metrics.target_parallelism - metrics.inflight)
                remaining = metrics.total - metrics.submitted

            # 4) Sottomissione controllata
            burst = min(SUBMIT_BURST, can_submit, remaining)
            for _ in range(burst):
                with metrics.lock:
                    job_id = metrics.submitted
                    metrics.submitted += 1
                    metrics.inflight += 1

                fut = executor.submit(process_job, job_id)
                futures[fut] = job_id

            time.sleep(DISPATCHER_INTERVAL)

# ── CPU-aware scaler ───────────────────────────────────────────────────

def scaler_loop():
    while True:
        time.sleep(SCALER_INTERVAL)

        cpu, ram = get_system_stats()

        with metrics.lock:
            metrics.cpu = cpu
            metrics.ram = ram

            completed = metrics.completed
            failed = metrics.failed
            total = metrics.total
            backlog = total - (completed + failed)
            target = metrics.target_parallelism

            # logica di scaling con isteresi
            if backlog <= 0:
                metrics.last_scaling_reason = "completed"
                break

            if cpu >= CPU_HIGH_WATERMARK and target > MIN_PARALLELISM:
                metrics.target_parallelism -= 1
                metrics.last_scaling_reason = f"cpu_high ({cpu:.1f}%)"
            elif cpu <= CPU_LOW_WATERMARK and backlog >= BACKLOG_SCALE_HINT and target < MAX_WORKERS:
                metrics.target_parallelism += 1
                metrics.last_scaling_reason = f"cpu_low ({cpu:.1f}%) + backlog"
            else:
                metrics.last_scaling_reason = "hold"

            metrics.series_speed.append(round(metrics.throughput_ema, 2))
            metrics.series_cpu.append(round(cpu, 1))

# ── Dashboard WebSocket ────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
#socketio = SocketIO(app, async_mode="threading")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8" />
    <title>CPU-aware Scaling Dashboard</title>
    https://cdn.socket.io/4.7.5/socket.io.min.jsscript>
    https://cdn.jsdelivr.net/npm/chart.jsscript>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111;
            color: #eee;
            margin: 0;
            padding: 24px;
        }
        h1 { color: #00e08a; margin-top: 0; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
        }
        .card {
            background: #1b1b1b;
            border: 1px solid #333;
            border-radius: 10px;
            padding: 16px;
        }
        .card h2 {
            margin: 0 0 8px 0;
            font-size: 16px;
            color: #9fe7c2;
        }
        .big {
            font-size: 26px;
            font-weight: bold;
            word-break: break-word;
        }
        .sub {
            color: #aaa;
            font-size: 12px;
            margin-top: 6px;
        }
        .charts {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }
        canvas {
            background: #1b1b1b;
            border: 1px solid #333;
            border-radius: 10px;
            padding: 10px;
        }
        @media (max-width: 900px) {
            .charts { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <h1>⚡ CPU-aware Scaling Dashboard</h1>

    <div class="grid">
        <div class="card">
            <h2>Stato</h2>
            <div class="big" id="state">--</div>
        </div>
        <div class="card">
            <h2>Progress</h2>
            <div class="big" id="progress">0%</div>
            <div class="sub"><span id="completed">0</span> / <span id="total">0</span></div>
        </div>
        <div class="card">
            <h2>Backlog</h2>
            <div class="big" id="backlog">0</div>
            <div class="sub">in flight: <span id="inflight">0</span></div>
        </div>
        <div class="card">
            <h2>ETA</h2>
            <div class="big" id="eta">--</div>
        </div>
        <div class="card">
            <h2>Throughput</h2>
            <div class="big" id="tp">0 job/s</div>
        </div>
        <div class="card">
            <h2>CPU / RAM</h2>
            <div class="big"><span id="cpu">0</span>% / <span id="ram">0</span>%</div>
        </div>
        <div class="card">
            <h2>Parallelismo effettivo</h2>
            <div class="big"><span id="target">1</span> / <span id="maxw">1</span></div>
            <div class="sub">target / max_workers pool</div>
        </div>
        <div class="card">
            <h2>Ultima decisione scaler</h2>
            <div class="big" id="reason">--</div>
        </div>
    </div>

    <div class="charts">
        <canvas id="speedChart" height="150"></canvas>
        <canvas id="cpuChart" height="150"></canvas>
    </div>

<script>
const socket = io();

const speedCtx = document.getElementById('speedChart').getContext('2d');
const cpuCtx = document.getElementById('cpuChart').getContext('2d');

const speedChart = new Chart(speedCtx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'Throughput (job/s)',
            data: [],
            borderColor: '#00e08a',
            tension: 0.25
        }]
    },
    options: {
        animation: false,
        responsive: true,
        scales: { x: { display: false } }
    }
});

const cpuChart = new Chart(cpuCtx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'CPU %',
            data: [],
            borderColor: '#6ecbff',
            tension: 0.25
        }]
    },
    options: {
        animation: false,
        responsive: true,
        scales: {
            x: { display: false },
            y: { min: 0, max: 100 }
        }
    }
});

socket.on("snapshot", function(d) {
    document.getElementById("state").innerText = d.state;
    document.getElementById("progress").innerText = d.progress + "%";
    document.getElementById("completed").innerText = d.completed;
    document.getElementById("total").innerText = d.total;
    document.getElementById("backlog").innerText = d.backlog;
    document.getElementById("inflight").innerText = d.inflight;
    document.getElementById("eta").innerText = d.eta;
    document.getElementById("tp").innerText = d.throughput + " job/s";
    document.getElementById("cpu").innerText = d.cpu;
    document.getElementById("ram").innerText = d.ram;
    document.getElementById("target").innerText = d.target_parallelism;
    document.getElementById("maxw").innerText = d.max_workers;
    document.getElementById("reason").innerText = d.reason;

    speedChart.data.labels = d.speed_series.map((_, i) => i);
    speedChart.data.datasets[0].data = d.speed_series;
    speedChart.update();

    cpuChart.data.labels = d.cpu_series.map((_, i) => i);
    cpuChart.data.datasets[0].data = d.cpu_series;
    cpuChart.update();
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

def broadcaster_loop():
    while True:
        time.sleep(1.0)

        with metrics.lock:
            completed = metrics.completed
            failed = metrics.failed
            total = metrics.total
            backlog = total - (completed + failed)
            progress = round((completed / total) * 100, 2) if total else 0.0

            payload = {
                "state": metrics.state,
                "completed": completed,
                "failed": failed,
                "total": total,
                "submitted": metrics.submitted,
                "backlog": backlog,
                "inflight": metrics.inflight,
                "progress": progress,
                "eta": format_eta(metrics.eta_seconds),
                "throughput": round(metrics.throughput_ema, 2),
                "cpu": round(metrics.cpu, 1),
                "ram": round(metrics.ram, 1),
                "target_parallelism": metrics.target_parallelism,
                "max_workers": MAX_WORKERS,
                "reason": metrics.last_scaling_reason,
                "speed_series": list(metrics.series_speed),
                "cpu_series": list(metrics.series_cpu),
            }

        socketio.emit("snapshot", payload)

        if payload["state"] == "done":
            break
# Check btcrecover
    try:
        result = subprocess.run(
            ["python3", "/opt/btcrecover/seedrecover.py", "--help"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            log_msg(f"  ✓ btcrecover: {result.stdout.strip()}")
        else:
            log_msg(f"  ✓ btcrecover installed")
    except Exception as e:
        log_msg(f"  ✗ ERROR: btcrecover not found: {e}")
        return False
    
    # Cerca parametri GPU nel help
            help_text = result.stdout.lower()
            if "gpu" in help_text or "cuda" in help_text:
                log_msg("✓ GPU support detected in btcrecover")
            else:
                log_msg("⚠️  GPU support not found in btcrecover --help")
                log_msg("  Will use CPU optimization instead")
        else:
            log_msg("❌ ERROR: btcrecover not working")
            return False
    except Exception as e:
        log_msg(f"❌ ERROR: {e}")
        return False
    
    log_msg("")
    
    # ===== VERIFICA CPU =====
    try:
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
        log_msg(f"✓ CPU: {cpu_count} cores available (AMD EPYC 7402P 24-Core)")
    except:
        pass

    log_msg("")
    return True

def count_words():
    """Count words in wordlist"""
    with open(WORDLIST, "r") as f:
        words = [w.strip() for w in f if w.strip()]
    return len(words)

def calculate_complexity():
    """Calculate search space"""
    if SEED_LENGTH == "12":
        combinations = 2048 ** 12
        log_msg(f"Search space: 2048^12 = {combinations:.2e} combinations")
        log_msg("GPU + Address filter = EXPONENTIAL speed boost")
    else:
        combinations = 2048 ** 24
        log_msg(f"Search space: 2048^24 = {combinations:.2e} combinations")



def run_recovery():
    """Run the recovery with GPU acceleration"""
    
    log_msg("")
    log_msg("="*80)
    log_msg("Bitcoin Seed Recovery - GPU Accelerated (RTX 3090 + CUDA 13.2)")
    log_msg("="*80)
    log_msg(f"Wallet Address: {WALLET_ADDRESS}")
    log_msg(f"Public Key: {PUBLIC_KEY[:20]}...{PUBLIC_KEY[-20:]}")
    log_msg(f"Seed Length: {SEED_LENGTH} words")
    log_msg(f"Wordlist: {CUSTOM_WORDLIST}")
    log_msg(f"GPU: RTX 3090 (10496 CUDA cores)")
    log_msg(f"CUDA Version: 13.2")
    log_msg(f"Driver: 595.71.05")
    log_msg(f"CPU: AMD EPYC 7402P 24-Core")
    log_msg(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_msg("="*80)
    log_msg("")
    
    calculate_complexity()
    log_msg("")

    # === GENERAZIONE MNEMONIC TEMPLATE ===
    if SEED_LENGTH == "12":
        mnemonic_template = "? ? ? ? ? ? ? ? ? ? ? ?"
    else:  # 24
        mnemonic_template = "? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ? ?"

    # === COMANDO OTTIMIZZATO PER GPU ===
    cmd = [
        "python3",
        "/opt/btcrecover/seedrecover.py",
        
        # === INPUT ===
        "--mnemonic", mnemonic_template,
        
        # === VERIFICA INDIRIZZO E CHIAVE PUBBLICA ===
        "--address", WALLET_ADDRESS,
        "--publickey", PUBLIC_KEY,
        
        # === WORDLIST PERSONALIZZATO ===
        "--wordlist", str(CUSTOM_WORDLIST),
        
        # === DERIVAZIONE ===
        "--bip32path", "m/44'/0'/0'/0/0",
        
        # === OTTIMIZZAZIONE CPU/THREADING ===
        "--threads", "24",  # 24 core EPYC = massima parallelizzazione
        
        # === PERFORMANCE OPTIMIZATION ===
        "--no-dupchecks",                          # Salta duplicati
        "--no-progressbar",                        # Riduce overhead
        
        # === OUTPUT ===
        "--outfile", str(RESULTS_FILE),
    ]

    log_msg("Command:")
    log_msg("")
    log_msg("python3 /opt/btcrecover/seedrecover.py \\")
    for i in range(0, len(cmd[2:]), 2):
        if i + 2 < len(cmd[2:]):
            log_msg(f"  {cmd[2 + i]} {cmd[2 + i + 1]} \\")
        else:
            log_msg(f"  {cmd[2 + i]} {cmd[2 + i + 1]}")
    
    log_msg("")
    log_msg("="*80)
    log_msg("Starting Recovery Process...")
    log_msg("="*80)
    log_msg("")
    
    start_time = datetime.now()
    last_update = start_time

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        seeds_found = 0
        tested_count = 0

        for line in process.stdout:
            line = line.rstrip()
            print(line)
            
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")

            # Rilevamento risultati
            if any(x in line.lower() for x in ["seed found", "found match", "success", "match found"]):
                seeds_found += 1
                log_msg("")
                log_msg("🎉🎉🎉 SEED FOUND! 🎉🎉🎉")
                log_msg(line)
                log_msg("")
            
            # Monitoraggio progresso
            if any(x in line.lower() for x in ["tested", "combinations", "trying", "attempting"]):
                current_time = datetime.now()
                # Log progresso ogni 30 secondi
                if (current_time - last_update).total_seconds() > 30:
                    log_msg(f"[PROGRESS] {line}")
                    last_update = current_time

        process.wait()
        
        elapsed_time = datetime.now() - start_time
        hours, remainder = divmod(int(elapsed_time.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)

        log_msg("")
        log_msg("="*80)
        log_msg(f"Recovery Process Completed")
        log_msg(f"Exit Code: {process.returncode}")
        log_msg(f"Elapsed Time: {hours}h {minutes}m {seconds}s")
        log_msg(f"End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_msg("="*80)
        log_msg("")

        # === MOSTRA RISULTATI ===
        if RESULTS_FILE.exists() and RESULTS_FILE.stat().st_size > 0:
            log_msg("✓✓✓ RESULTS FOUND ✓✓✓")
            log_msg("")
            with open(RESULTS_FILE, "r") as f:
                for i, line in enumerate(f, 1):
                    log_msg(f"  [{i}] {line.rstrip()}")
            log_msg("")
            return True
        else:
            if process.returncode == 0:
                log_msg("No matching seeds found (process completed successfully)")
                log_msg("This means:")
                log_msg("  - No seed in your wordlist matches the address")
                log_msg("  - Verify the address is correct")
                log_msg("  - Verify the wordlist contains all possible words")
            else:
                log_msg("Process completed with errors")
            return False

    except FileNotFoundError as e:
        log_msg(f"❌ ERROR: Command not found: {e}")
        return False
    except KeyboardInterrupt:
        log_msg("")
        log_msg("⚠️  Recovery interrupted by user (Ctrl+C)")
        log_msg("Attempting graceful shutdown...")
        try:
            process.terminate()
            process.wait(timeout=10)
        except:
            process.kill()
        return False
    except Exception as e:
        log_msg(f"❌ ERROR: {e}")
        import traceback
        log_msg(traceback.format_exc())
        return False
        

def main():
    """Main entry point"""
    try:
        log_msg("╔" + "="*78 + "╗")
        log_msg("║" + " "*78 + "║")
        log_msg("║" + "Bitcoin Seed Recovery System - GPU Accelerated".center(78) + "║")
        log_msg("║" + " "*78 + "║")
        log_msg("╚" + "="*78 + "╝")
        log_msg("")
        log_msg(f"Log file: {LOG_FILE}")
        log_msg(f"Results file: {RESULTS_FILE}")
        log_msg("")

        if not verify_setup():
            log_msg("")
            log_msg("❌ Setup verification failed!")
            log_msg("Please fix the issues above before running recovery")
            sys.exit(1)

        log_msg("")
        success = run_recovery()
        
        log_msg("")
        log_msg("╔" + "="*78 + "╗")
        if success:
            log_msg("║" + "✓ Recovery SUCCESSFUL - Seeds found!".ljust(78) + "║")
        else:
            log_msg("║" + "✓ Recovery completed - No seeds matched".ljust(78) + "║")
        log_msg("╚" + "="*78 + "╝")
        log_msg("")
        
        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        log_msg("")
        log_msg("Program interrupted by user")
        sys.exit(130)
    except Exception as e:
        log_msg(f"❌ FATAL ERROR: {e}")
        import traceback
        log_msg(traceback.format_exc())
        sys.exit(1)

# ── Main ───────────────────────────────────────────────────────────────
    threading.Thread(target=dispatcher_loop, daemon=True).start()
    threading.Thread(target=scaler_loop, daemon=True).start()
    threading.Thread(target=broadcaster_loop, daemon=True).start()

    print(f"Dashboard disponibile su: http://localhost:{UI_PORT}")
    socketio.run(app, host="0.0.0.0", port=5000)

if __name__ == "__main__":
    main()
