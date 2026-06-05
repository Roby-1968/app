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
- Dashboard batch GPU-accelerated - Vast.ai RTX 3090 optimized
"""
import subprocess
import sys
import multiprocessing
import json
import os
import time
import math
import threading
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from collections import deque
from concurrent.futures import ProcessPoolExecutor
import signal

# ════════════════════════════════════════════════════════════════════════════
# 🖥️ VAST.AI ENVIRONMENT DETECTION & SETUP
# ════════════════════════════════════════════════════════════════════════════

def detect_environment():
    """Detect if running on Vast.ai and GPU availability"""
    env = {
        "on_vast": False,
        "has_gpu": False,
        "gpu_name": "None",
        "vram_gb": 0,
        "cuda_cores": 0,
        "workspace": None,
        "device": "cpu"
    }
    
    # Check Vast.ai environment
    if Path("/workspace").exists():
        env["on_vast"] = True
        env["workspace"] = Path("/workspace")
    
    # Check GPU with PyTorch
    try:
        import torch
        env["has_gpu"] = torch.cuda.is_available()
        if env["has_gpu"]:
            env["device"] = "cuda:0"
            props = torch.cuda.get_device_properties(0)
            env["gpu_name"] = torch.cuda.get_device_name(0)
            env["vram_gb"] = props.total_memory / 1e9
            env["cuda_cores"] = props.multi_processor_count * 128  # Approximation
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️  GPU detection error: {e}")
    
    return env

ENVIRONMENT = detect_environment()

# ────────────────────────────────────────────────────────────────────────────
# 📁 PATHS - Vast.ai compatible
# ────────────────────────────────────────────────────────────────────────────

if ENVIRONMENT["on_vast"] and ENVIRONMENT["workspace"]:
    BASE_DIR = ENVIRONMENT["workspace"] / "recovery"
    LOG_DIR = ENVIRONMENT["workspace"] / "logs"
    TENSORBOARD_DIR = ENVIRONMENT["workspace"] / "tensorboard"
else:
    BASE_DIR = Path.home() / "recovery"
    LOG_DIR = Path.home() / "logs"
    TENSORBOARD_DIR = Path.home() / "tensorboard"

# Create directories
try:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TENSORBOARD_DIR.mkdir(parents=True, exist_ok=True)
except (OSError, PermissionError) as e:
    print(f"⚠️  Directory creation error: {e}")
    BASE_DIR = Path("/tmp") / "recovery"
    LOG_DIR = Path("/tmp") / "logs"
    TENSORBOARD_DIR = Path("/tmp") / "tensorboard"
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TENSORBOARD_DIR.mkdir(parents=True, exist_ok=True)

WORDLIST = BASE_DIR / "electrum_wordlist.txt"
ADDRESSLIST = BASE_DIR / "addresslist.txt"
CUSTOM_WORDLIST = WORDLIST

# Results with timestamp
TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
RESULTS_FILE = BASE_DIR / f"results_{TIMESTAMP}.txt"
LOG_FILE = LOG_DIR / f"recovery_{TIMESTAMP}.log"
METRICS_FILE = LOG_DIR / f"metrics_{TIMESTAMP}.json"

# ────────────────────────────────────────────────────────────────────────────
# 📊 LOGGING SETUP
# ────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def log_msg(msg, level="INFO"):
    """Log message with timestamp"""
    if level == "INFO":
        logger.info(msg)
    elif level == "WARNING":
        logger.warning(msg)
    elif level == "ERROR":
        logger.error(msg)
    elif level == "DEBUG":
        logger.debug(msg)

# ════════════════════════════════════════════════════════════════════════════
# ⚙️ CONFIGURATION - RTX 3090 Optimized
# ════════════════════════════════════════════════════════════════════════════

WALLET_ADDRESS = "16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v"
PUBLIC_KEY = "02145d2611c823a396ef6712ce0f712f09b9b4f3135e3e0aa3230fb9b6d08d1e16"
SEED_LENGTH = "12"
WALLET_TYPE = "electrum2"
LANGUAGE = "english"

# 🖥️ GPU-aware configuration
USE_GPU = ENVIRONMENT["has_gpu"]
DEVICE = ENVIRONMENT["device"]

if USE_GPU:
    # RTX 3090: 10496 CUDA cores, 24GB VRAM
    MAX_WORKERS = 16  # Highly parallelizable with GPU
    MIN_PARALLELISM = 4
    TOTAL_JOBS = 50000  # Large batch for GPU
    SUBMIT_BURST = 64  # Aggressive burst
    BATCH_SIZE = 2048  # GPU batch size
    GPU_MEMORY_FRACTION = 0.9  # Use 90% of 24GB
else:
    # CPU fallback
    MAX_WORKERS = max(2, (os.cpu_count() or 4))
    MIN_PARALLELISM = 1
    TOTAL_JOBS = 5000
    SUBMIT_BURST = 8
    BATCH_SIZE = 128

# Performance tuning
CPU_HIGH_WATERMARK = 85.0
CPU_LOW_WATERMARK = 55.0
GPU_HIGH_WATERMARK = 90.0  # GPU memory threshold
GPU_LOW_WATERMARK = 40.0
BACKLOG_SCALE_HINT = 50
SCALER_INTERVAL = 0.3  # Aggressive scaling on GPU
DISPATCHER_INTERVAL = 0.01  # Fast dispatch
EMA_ALPHA = 0.2  # Responsive EMA

# Vast.ai specific
UI_HOST = os.getenv("UI_HOST", "0.0.0.0")
UI_PORT = int(os.getenv("UI_PORT", "5000"))
SECRET_KEY = os.getenv("SECRET_KEY", "vast-secret-key")

# Telegram notifications (optional)
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# ── Telegram config ────────────────────────────────────────────────────────────
# Ottieni il token creando un bot con @BotFather su Telegram
# Ottieni il chat_id avviando il bot e visitando:
#   https://api.telegram.org/bot<TOKEN>/getUpdates
#TG_TOKEN   = "7067029206:AAGTgCEARp6XfjWUXKKfZv_VDySuxp5YWWw"        # es. "123456789:AABBccDDeeFFggHH..."
#TG_CHAT_ID = "5126563581"          # es. "987654321"f

# Notifica periodica ogni N minuti (0 = disabilitata)
TG_HEARTBEAT_MINUTES = 30

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


def log_msg(msg):
    """Print and log message"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    with open(LOG_FILE, "a") as f:
        f.write(log_line + "\n")
        
required_files = {
    WORDLIST: "Electrum wordlist",
    ADDRESSLIST: "Address list"
}
       
def verify_files():
    """Verify all required files exist"""
    log_msg("Verifying system setup...")
    
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
    
    if not WORDLIST.exists():
        log_msg(f"❌ ERROR: Wordlist not found: {WORDLIST}")
        return False
    
    word_count = sum(1 for line in open(WORDLIST) if line.strip())
    log_msg(f"✓ Wordlist: {WORDLIST} ({word_count} words)")
    
    return True
	
# CUDA environment
os.environ['CUDA_VISIBLE_DEVICES'] = os.getenv('CUDA_VISIBLE_DEVICES', '0')
os.environ['CUDA_LAUNCH_BLOCKING'] = '0'  # Async for performance
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'  # Reduce TF verbosity

# ════════════════════════════════════════════════════════════════════════════
# 🔧 GPU UTILITIES
# ════════════════════════════════════════════════════════════════════════════

def get_gpu_stats():
    """Get GPU utilization and memory"""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_mem_allocated = torch.cuda.memory_allocated(0) / 1e9
            gpu_mem_reserved = torch.cuda.memory_reserved(0) / 1e9
            gpu_mem_total = torch.cuda.get_device_properties(0).total_memory / 1e9
            gpu_util = (gpu_mem_allocated / gpu_mem_total) * 100
            return gpu_util, gpu_mem_allocated, gpu_mem_total
    except:
        pass
    return 0.0, 0.0, 0.0

def cleanup_gpu_memory():
    """Force GPU memory cleanup"""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except:
        pass

# ════════════════════════════════════════════════════════════════════════════
# 📊 METRICS CLASS
# ════════════════════════════════════════════════════════════════════════════

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
        self.gpu_util = 0.0
        self.gpu_mem_gb = 0.0
        self.gpu_mem_total = 0.0
        self.last_scaling_reason = "init"
        self.state = "running"
        self.series_speed = deque(maxlen=120)
        self.series_cpu = deque(maxlen=120)
        self.series_gpu = deque(maxlen=120)
        self.lock = threading.Lock()

metrics = Metrics()

# ════════════════════════════════════════════════════════════════════════════
# 💼 JOB PROCESSING
# ════════════════════════════════════════════════════════════════════════════

def process_job_cpu(job_id: int) -> dict:
    """CPU-bound job simulation"""
    # Simulate some work
    result = sum(i**2 for i in range(10000))
    time.sleep(0.001)  # Small delay
    return {
        "job_id": job_id,
        "status": "ok",
        "device": "cpu",
        "result": result % 1000000
    }

def process_job_gpu(job_id: int) -> dict:
    """GPU-accelerated job"""
    try:
        import torch
        if torch.cuda.is_available():
            device = torch.device("cuda:0")
            # GPU work: matrix multiplication
            size = 2048
            x = torch.randn(size, size, device=device, dtype=torch.float32)
            y = torch.randn(size, size, device=device, dtype=torch.float32)
            result = torch.matmul(x, y)
            torch.cuda.synchronize()
            
            return {
                "job_id": job_id,
                "status": "ok",
                "device": "gpu",
                "result": float(result[0, 0].item()) % 1000000
            }
    except Exception as e:
        return {
            "job_id": job_id,
            "status": "error",
            "device": "gpu",
            "error": str(e)
        }
    
    # Fallback to CPU
    return process_job_cpu(job_id)

def process_job(job_id: int) -> dict:
    """Route to GPU or CPU"""
    if USE_GPU:
        return process_job_gpu(job_id)
    else:
        return process_job_cpu(job_id)

# ════════════════════════════════════════════════════════════════════════════
# 🎯 DISPATCHER & SCALER
# ════════════════════════════════════════════════════════════════════════════

def get_system_stats():
    """Get CPU/RAM/GPU stats"""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        
        if USE_GPU:
            gpu_util, gpu_mem, gpu_total = get_gpu_stats()
            return cpu, ram, gpu_util, gpu_mem, gpu_total
        return cpu, ram, 0.0, 0.0, 0.0
    except:
        return 0.0, 0.0, 0.0, 0.0, 0.0

def dispatcher_loop():
    """Main dispatcher with GPU support"""
    futures = {}
    last_completed = 0
    last_t = time.time()
    cleanup_counter = 0

    try:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            while True:
                # Cleanup completed futures
                done = []
                for fut, job_id in list(futures.items()):
                    if fut.done():
                        done.append((fut, job_id))

                for fut, job_id in done:
                    futures.pop(fut, None)
                    try:
                        result = fut.result()
                        with metrics.lock:
                            metrics.completed += 1
                            metrics.inflight = max(0, metrics.inflight - 1)
                    except Exception as e:
                        log_msg(f"⚠️  Job {job_id} failed: {e}", "WARNING")
                        with metrics.lock:
                            metrics.failed += 1
                            metrics.inflight = max(0, metrics.inflight - 1)
                
                # Periodic GPU memory cleanup
                cleanup_counter += 1
                if cleanup_counter % 100 == 0 and USE_GPU:
                    cleanup_gpu_memory()

                # Calculate throughput
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
                    metrics.throughput_ema = inst_tp if prev == 0 else (
                        EMA_ALPHA * inst_tp + (1 - EMA_ALPHA) * prev
                    )
                    tp = metrics.throughput_ema
                    metrics.eta_seconds = (backlog / tp) if tp > 0 else None

                last_completed = completed
                last_t = now

                # Check completion
                with metrics.lock:
                    if metrics.completed + metrics.failed >= metrics.total:
                        metrics.state = "done"
                        break
                    can_submit = max(0, metrics.target_parallelism - metrics.inflight)
                    remaining = metrics.total - metrics.submitted

                # Submit burst
                burst = min(SUBMIT_BURST, can_submit, remaining)
                for _ in range(burst):
                    with metrics.lock:
                        job_id = metrics.submitted
                        metrics.submitted += 1
                        metrics.inflight += 1

                    fut = executor.submit(process_job, job_id)
                    futures[fut] = job_id

                time.sleep(DISPATCHER_INTERVAL)
    
    except Exception as e:
        log_msg(f"❌ Dispatcher error: {e}", "ERROR")
        import traceback
        log_msg(traceback.format_exc(), "ERROR")

def format_eta(seconds):
    """Format ETA to human-readable"""
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

def scaler_loop():
    """CPU/GPU-aware dynamic scaler"""
    try:
        while True:
            time.sleep(SCALER_INTERVAL)
            
            if USE_GPU:
                cpu, ram, gpu_util, gpu_mem, gpu_total = get_system_stats()
            else:
                cpu, ram, _, _, _ = get_system_stats()
                gpu_util = 0.0

            with metrics.lock:
                metrics.cpu = cpu
                metrics.ram = ram
                metrics.gpu_util = gpu_util
                metrics.gpu_mem_gb = gpu_mem
                metrics.gpu_mem_total = gpu_total
                
                completed = metrics.completed
                failed = metrics.failed
                total = metrics.total
                backlog = total - (completed + failed)
                target = metrics.target_parallelism

                if backlog <= 0:
                    metrics.last_scaling_reason = "completed"
                    break

                # GPU scaling logic
                if USE_GPU:
                    if gpu_util >= GPU_HIGH_WATERMARK and target > MIN_PARALLELISM:
                        metrics.target_parallelism -= 1
                        metrics.last_scaling_reason = f"gpu_high ({gpu_util:.1f}%)"
                    elif gpu_util <= GPU_LOW_WATERMARK and backlog >= BACKLOG_SCALE_HINT and target < MAX_WORKERS:
                        metrics.target_parallelism += 1
                        metrics.last_scaling_reason = f"gpu_low ({gpu_util:.1f}%)"
                    else:
                        metrics.last_scaling_reason = "hold_gpu"
                else:
                    # CPU scaling logic
                    if cpu >= CPU_HIGH_WATERMARK and target > MIN_PARALLELISM:
                        metrics.target_parallelism -= 1
                        metrics.last_scaling_reason = f"cpu_high ({cpu:.1f}%)"
                    elif cpu <= CPU_LOW_WATERMARK and backlog >= BACKLOG_SCALE_HINT and target < MAX_WORKERS:
                        metrics.target_parallelism += 1
                        metrics.last_scaling_reason = f"cpu_low ({cpu:.1f}%)"
                    else:
                        metrics.last_scaling_reason = "hold_cpu"

                metrics.series_speed.append(round(metrics.throughput_ema, 2))
                metrics.series_cpu.append(round(cpu, 1))
                metrics.series_gpu.append(round(gpu_util, 1))
    
    except Exception as e:
        log_msg(f"❌ Scaler error: {e}", "ERROR")

def broadcaster_loop():
    """Console output with GPU info"""
    try:
        iteration = 0
        while True:
            time.sleep(2.0)
            iteration += 1

            with metrics.lock:
                completed = metrics.completed
                failed = metrics.failed
                total = metrics.total
                backlog = total - (completed + failed)
                progress = round((completed / total) * 100, 2) if total else 0.0

                if iteration % 5 == 0:
                    if USE_GPU:
                        log_msg(
                            f"📊 [{metrics.state.upper()}] {progress}% | "
                            f"✓{completed} ✗{failed} 📦{backlog} | "
                            f"⚡{round(metrics.throughput_ema, 2)} job/s | "
                            f"CPU:{round(metrics.cpu, 1)}% RAM:{round(metrics.ram, 1)}% | "
                            f"GPU:{round(metrics.gpu_util, 1)}% "
                            f"({round(metrics.gpu_mem_gb, 2)}/{round(metrics.gpu_mem_total, 2)} GB) | "
                            f"ETA:{format_eta(metrics.eta_seconds)}"
                        )
                    else:
                        log_msg(
                            f"📊 [{metrics.state.upper()}] {progress}% | "
                            f"✓{completed} ✗{failed} 📦{backlog} | "
                            f"⚡{round(metrics.throughput_ema, 2)} job/s | "
                            f"CPU:{round(metrics.cpu, 1)}% RAM:{round(metrics.ram, 1)}% | "
                            f"ETA:{format_eta(metrics.eta_seconds)}"
                        )

            if metrics.state == "done":
                break
    
    except Exception as e:
        log_msg(f"❌ Broadcaster error: {e}", "ERROR")

# ════════════════════════════════════════════════════════════════════════════
# 🔍 VERIFICATION & SETUP
# ════════════════════════════════════════════════════════════════════════════

def verify_system():
    """Verify environment and GPU"""
    log_msg("╔" + "="*80 + "╗")
    log_msg("║" + "GPU-Accelerated Batch Dashboard - Vast.ai Optimized".center(80) + "║")
    log_msg("╚" + "="*80 + "╝")
    
    log_msg(f"🐍 Python {sys.version.split()[0]} on {sys.platform}")
    log_msg(f"📁 Workspace: {BASE_DIR}")
    log_msg(f"📁 Logs: {LOG_FILE}")
    
    # Environment detection
    if ENVIRONMENT["on_vast"]:
        log_msg("✓ Running on Vast.ai")
    else:
        log_msg("⚠️  Not on Vast.ai (local run)")
    
    # GPU verification
    if USE_GPU:
        log_msg("✅ GPU ACCELERATION ENABLED")
        log_msg(f"  GPU: {ENVIRONMENT['gpu_name']}")
        log_msg(f"  VRAM: {ENVIRONMENT['vram_gb']:.2f} GB")
        log_msg(f"  CUDA Cores: {ENVIRONMENT['cuda_cores']}")
        log_msg(f"  Max Workers: {MAX_WORKERS}")
        log_msg(f"  Total Jobs: {TOTAL_JOBS}")
        
        try:
            import torch
            log_msg(f"  PyTorch: {torch.__version__}")
            log_msg(f"  CUDA: {torch.version.cuda}")
            log_msg(f"  cuDNN: {torch.backends.cudnn.version()}")
        except:
            pass
    else:
        log_msg("⚠️  GPU not available - using CPU")
        log_msg(f"  CPU Cores: {os.cpu_count()}")
        log_msg(f"  Max Workers: {MAX_WORKERS}")
    
    # Check wordlist
    if WORDLIST.exists():
        try:
            word_count = sum(1 for line in open(WORDLIST) if line.strip())
            log_msg(f"✓ Wordlist: {word_count} words")
        except Exception as e:
            log_msg(f"⚠️  Wordlist error: {e}", "WARNING")
    else:
        log_msg(f"ℹ️  Wordlist not found: {WORDLIST} (OK for simulation)")
    
    log_msg("")
    return True
    
def calculate_complexity():
    """Calculate search space"""
    if SEED_LENGTH == "12":
        combinations = 2048 ** 12
        log_msg(f"Search space: 2048^12 = {combinations:.2e} combinations")
        log_msg("GPU + Address filter = EXPONENTIAL speed boost")
    else:
        combinations = 2048 ** 24


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

# ════════════════════════════════════════════════════════════════════════════
# 🚀 MAIN & SIGNAL HANDLING
# ════════════════════════════════════════════════════════════════════════════

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    log_msg(f"\n⏸️  Received signal {signum}, shutting down...")
    metrics.state = "interrupted"
    sys.exit(130)

def save_metrics():
    """Save metrics to JSON"""
    try:
        with metrics.lock:
            data = {
                "timestamp": datetime.now().isoformat(),
                "total_jobs": metrics.total,
                "completed": metrics.completed,
                "failed": metrics.failed,
                "success_rate": (metrics.completed / metrics.total * 100) if metrics.total > 0 else 0,
                "throughput": round(metrics.throughput_ema, 2),
                "duration_seconds": time.time() - metrics.start_time,
                "gpu_enabled": USE_GPU,
                "gpu_name": ENVIRONMENT["gpu_name"] if USE_GPU else "N/A",
                "max_workers": MAX_WORKERS,
            }
        
        with open(METRICS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        log_msg(f"✓ Metrics saved to {METRICS_FILE}")
    except Exception as e:
        log_msg(f"⚠️  Could not save metrics: {e}", "WARNING")

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

        if not verify_files():
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
        
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Verify system
        if not verify_system():
            return 1
        
        log_msg("🚀 Starting batch processing...\n")
        
        # Start worker threads
        threads = []
        
        t_dispatcher = threading.Thread(target=dispatcher_loop, daemon=False, name="Dispatcher")
        t_scaler = threading.Thread(target=scaler_loop, daemon=True, name="Scaler")
        t_broadcaster = threading.Thread(target=broadcaster_loop, daemon=True, name="Broadcaster")
        
        threads.extend([t_dispatcher, t_scaler, t_broadcaster])
        
        for t in threads:
            t.start()
            log_msg(f"✓ Started thread: {t.name}")
        
        log_msg("")
        
        # Wait for completion
        t_dispatcher.join(timeout=3600)  # 1 hour max
        
        log_msg("\n" + "="*80)
        log_msg(f"✅ BATCH PROCESSING COMPLETED")
        log_msg(f"   Total time: {time.time() - metrics.start_time:.2f}s")
        log_msg(f"   Success rate: {(metrics.completed/metrics.total*100):.2f}%")
        log_msg(f"   Avg throughput: {metrics.throughput_ema:.2f} job/s")
        log_msg("="*80)
        
        # Save metrics
        save_metrics()
        
        return 0
    
    except KeyboardInterrupt:
        log_msg("\n⏸️  Interrupted by user")
        return 130
    except Exception as e:
        log_msg(f"❌ FATAL ERROR: {e}", "ERROR")
        import traceback
        log_msg(traceback.format_exc(), "ERROR")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)