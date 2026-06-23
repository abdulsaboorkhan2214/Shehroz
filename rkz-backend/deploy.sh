#!/bin/bash
# RKZ Backend — deploy script (Groq edition, no GPU needed)
# Works on any Ubuntu VPS or Oracle Cloud Free Tier
# Usage: chmod +x deploy.sh && GROQ_API_KEY=your_key ./deploy.sh

set -e

if [ -z "$GROQ_API_KEY" ]; then
  echo "❌ Missing GROQ_API_KEY"
  echo "   Run: GROQ_API_KEY=your_key ./deploy.sh"
  exit 1
fi

echo ""
echo "========================================"
echo "  RKZ Lead Agent — Deploying Backend"
echo "========================================"
echo ""

# ── System packages ───────────────────────────────────────────────────────────
echo "[1/3] Updating system packages..."
sudo apt-get update -y -q
sudo apt-get install -y -q python3-pip curl

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[2/3] Installing Python dependencies..."
pip3 install -r requirements.txt --quiet

# ── Systemd service ───────────────────────────────────────────────────────────
echo "[3/3] Setting up systemd service..."

WORK_DIR=$(pwd)
CURRENT_USER=$(whoami)

sudo tee /etc/systemd/system/rkz-backend.service > /dev/null <<EOF
[Unit]
Description=RKZ Lead Agent Backend
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$WORK_DIR
Environment="GROQ_API_KEY=$GROQ_API_KEY"
ExecStart=python3 -m uvicorn agent:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable rkz-backend
sudo systemctl restart rkz-backend

# ── Firewall ──────────────────────────────────────────────────────────────────
sudo ufw allow 8000/tcp 2>/dev/null && echo "      UFW: port 8000 opened" || true

# ── Done ──────────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "========================================"
echo "  DONE"
echo "========================================"
echo ""
echo "  Backend URL : http://$SERVER_IP:8000"
echo "  Health check: http://$SERVER_IP:8000/health"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status rkz-backend   # check if running"
echo "    sudo systemctl restart rkz-backend  # restart"
echo "    journalctl -u rkz-backend -f        # live logs"
echo ""
