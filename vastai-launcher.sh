#!/bin/bash

# SEED-RECOVERY vastai.ai GPU Deployment Launcher
# For: 16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v (Electrum 2, 12-word)
# Auto-launches recovery on vast.ai GPU

set -e

echo "========================================"
echo "SEED-RECOVERY vastai.ai Launcher"
echo "========================================"
echo ""
echo "Wallet: 16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v"
echo "Seed Type: Electrum 2, 12-word English"
echo "Recovery Method: MPK-based validation"
echo ""

# Step 1: Check vast CLI installed
echo "[1/6] Checking vast.ai CLI..."
if ! command -v vastai &> /dev/null; then
    echo "vast.ai CLI not found. Installing..."
    pip install vastai
fi

# Step 2: Authenticate
echo "[2/6] Authenticating with vast.ai..."
read -p "Enter your vastai API Key: " API_KEY
vastai set api-key "$API_KEY"
vastai show user || { echo "Authentication failed!"; exit 1; }

# Step 3: Check Docker
echo "[3/6] Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker not installed. Please install Docker first."
    exit 1
fi

# Step 4: Build Docker image
echo "[4/6] Building Docker image this may take 2-3 minutes.."
docker build -t recovery_electrum2 -f Dockerfile.vastai .\
  --build-arg WALLET_ADDRESS="16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v" \
  --build-arg PUBLIC_KEY="02145d2611c823a396ef6712ce0f712f09b9b4f3135e3e0aa3230fb9b6d08d1e16"

#echo "[5/6] Searching for available GPUs on vast.ai..."
#echo "Looking for RTX 3090, A100, or A6000..."

# Ricerca RTX 3090 (Usa l'uguaglianza con stringa tra virgolette)
 #GPU_OFFER=$(vastai search offers 'gpu_name = "RTX 3090" cuda_vers >= 11.8 reliability >= 0.95' --raw | jq -r '.[0].id // empty')

 #if [ -z "$GPU_OFFER" ]; then
  #   echo "RTX 3090 not available, trying A100..."
         # Ricerca A100
   #          GPU_OFFER=$(vastai search offers 'gpu_name = "A100" cuda_vers >= 11.8 reliability >= 0.95' --raw | jq -r '.[0].id // empty')
    #         fi

             # Facoltativo: Aggiunta ricerca A6000 (menzionata nel tuo echo iniziale ma assente nel codice)
     #        if [ -z "$GPU_OFFER" ]; then
      #           echo "A100 not available, trying A6000..."
       #              GPU_OFFER=$(vastai search offers 'gpu_name = "RTX A6000" cuda_vers >= 11.8 reliability >= 0.95' --raw | jq -r '.[0].id // empty')
        #             fi

         #            if [ -z "$GPU_OFFER" ]; then
          #               echo "ERROR: No suitable GPU found. Please check vast.ai availability and account balance."
           #                  exit 1
            #                 fi

             #                echo "Found GPU offer ID: $GPU_OFFER"

# Step 6: Launch instance
echo "[6/6] Launching recovery instance..."
echo ""
echo "⏳ Starting GPU instance with official CUDA image..."

if ! command -v jq &> /dev/null; then
    echo "❌ ERROR: 'jq' is required but not installed."
    exit 1
fi

# Avvio dell'istanza con immagine CUDA ufficiale e sicura
#RESPONSE="" \
 # --image nvidia/cuda:12.2.2-devel-ubuntu22.04 \
 # --disk 100 \
 # --label "seed-recovery-electrum2" \
 # --ssh \
 # --direct \
 # --raw 2>&1)

INSTANCE="38636236"

#if [ -z "$INSTANCE" ] || [ "$INSTANCE" = "null" ]; then
#    echo "❌ ERROR: Failed to create instance"
 #   echo "Vast.ai response: $RESPONSE"
  #  exit 1
#fi

echo "✅ Instance created successfully! ID: $INSTANCE"
echo "⏳ Waiting for SSH IP and Port to become active..."

SSH_IP="45.83.205.200"
SSH_PORT="36236"
MAX_ATTEMPTS=30
ATTEMPT=1

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
    INFO=$(vastai show instance "$INSTANCE" --raw 2>/dev/null)
    SSH_IP=$(echo "$INFO" | jq -r '.ssh_host // empty')
    SSH_PORT=$(echo "$INFO" | jq -r '.ssh_port // empty')
    
    if [ ! -z "$SSH_IP" ] && [ "$SSH_IP" != "null" ] && [ ! -z "$SSH_PORT" ] && [ "$SSH_PORT" != "null" ]; then
        echo ""
        echo "🚀 Instance is network-ready!"
        echo "🌐 SSH Target: $SSH_IP:$SSH_PORT"
        break
    fi
    
    printf "."
    ATTEMPT=$((ATTEMPT + 1))
    sleep 10
done

if [ -z "$SSH_IP" ] || [ "$SSH_IP" = "null" ]; then
    echo ""
    echo "❌ ERROR: Timeout reached. SSH configuration not available."
    exit 1
fi

# Pausa di sicurezza di 15 secondi per garantire che l'ambiente SSH interno sia inizializzato
echo "⏳ Giving the container 15 seconds to fully initialize SSH services..."
sleep 15

# Opzioni SSH per ignorare i controlli dei dispositivi sconosciuti nell'automazione
SSH_OPTIONS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p $SSH_PORT"

echo "⚙️ Provisioning Electrum Environment inside the instance..."

# Invio dei comandi di installazione remoti (Eseguiti come root nel container Vast)
ssh $SSH_OPTIONS root@$SSH_IP << 'EOF'
echo "📦 Updating package lists..."
apt-get update -y && apt-get upgrade -y
  
  echo "🛠️ Installing Python, Pip and dependencies..."
  apt-get install -y python3-pip python3-setuptools python3-pyqt5 libsecp256k1-dev git
  
  #echo "🐍 Installing Electrum via Pip..."
  #pip3 install electrum
  
  echo "🎉 Environment ready! Verifying installation:"
  electrum version
EOF

if [ $? -eq 0 ]; then
    echo "🏆 Provisioning completed successfully! Electrum is ready."
else
    echo "❌ ERROR: Provisioning failed during SSH command execution."
    exit 1
fi


echo "✅ Instance created: $INSTANCE"
echo ""
echo "========================================"
echo "🚀 RECOVERY STARTED!"
echo "========================================"
echo ""
echo "Instance ID: $INSTANCE"
echo "Status: Starting takes 1-2 minutes to boot"
echo ""
echo "Monitor recovery progress:"
echo "  vast show instance $INSTANCE"
echo ""
echo "Get instance logs:"
echo "  vast logs $INSTANCE"
echo ""
echo "SSH into instance once running:"
echo "  vast ssh $INSTANCE"
echo ""
echo "Expected recovery time: 12-48 hours"
echo "Cost: ~€0.40/hour RTX 3090"
echo ""
echo "IMPORTANT: Once seed is found, check logs immediately!"
echo "The seed will appear in the recovery logs."
echo "========================================"
echo ""
