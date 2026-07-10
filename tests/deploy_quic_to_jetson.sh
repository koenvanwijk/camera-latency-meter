#!/bin/bash
# Deploy en start QUIC bridge op Jetson Orin.
# Uitvoeren vanaf LAPTOP (heeft SSH toegang tot Jetson via LAN).
#
# Gebruik:
#   JETSON=192.168.86.47 JETSON_USER=jetson bash deploy_quic_to_jetson.sh

set -e

JETSON="${JETSON:-192.168.86.47}"
JETSON_USER="${JETSON_USER:-jetson}"
REMOTE_DIR="/home/${JETSON_USER}/camera-latency-meter/jetson"
SSH="ssh ${JETSON_USER}@${JETSON}"
SCP="scp"

echo "=== Deploy QUIC bridge naar Jetson ${JETSON} ==="

# ── 1. Kopieer scripts ─────────────────────────────────────────────────────────
echo ""
echo "[1/4] Kopieer scripts naar Jetson ..."
$SSH "mkdir -p ${REMOTE_DIR}"
$SCP "$(dirname "$0")/../jetson/start_cam0_quic.py"   "${JETSON_USER}@${JETSON}:${REMOTE_DIR}/"
$SCP "$(dirname "$0")/../jetson/setup_quic_certs.sh"  "${JETSON_USER}@${JETSON}:${REMOTE_DIR}/"

# ── 2. Installeer aioquic ──────────────────────────────────────────────────────
echo ""
echo "[2/4] Installeer aioquic op Jetson ..."
$SSH "pip3 install --quiet aioquic"

# ── 3. Genereer TLS certificaat ────────────────────────────────────────────────
echo ""
echo "[3/4] Genereer TLS certificaat ..."
$SSH "cd ${REMOTE_DIR} && bash setup_quic_certs.sh ${JETSON}"

# Kopieer cert.pem terug naar laptop (voor optionele verificatie)
LOCAL_CERT="$(dirname "$0")/../jetson/cert.pem"
$SCP "${JETSON_USER}@${JETSON}:${REMOTE_DIR}/cert.pem" "${LOCAL_CERT}"
echo "    cert.pem lokaal opgeslagen: ${LOCAL_CERT}"

# ── 4. Start QUIC bridge (en TCP stream als die nog niet draait) ───────────────
echo ""
echo "[4/4] Start services op Jetson ..."

# Controleer of start_cam0_tcp.sh al draait
TCP_RUNNING=$($SSH "pgrep -f start_cam0_tcp.sh || pgrep -f tcpserversink" 2>/dev/null | wc -l)
if [[ "$TCP_RUNNING" -eq 0 ]]; then
    echo "    TCP stream (GStreamer) starten ..."
    $SSH "nohup bash ${REMOTE_DIR}/start_cam0_tcp.sh > /tmp/cam0_tcp.log 2>&1 &"
    sleep 2
else
    echo "    TCP stream al actief."
fi

# Start QUIC bridge
echo "    QUIC bridge starten ..."
$SSH "pkill -f start_cam0_quic.py 2>/dev/null; true"
$SSH "nohup python3 ${REMOTE_DIR}/start_cam0_quic.py \
    > /tmp/cam0_quic.log 2>&1 &"
sleep 2

# Controleer of het gelukt is
QUIC_PID=$($SSH "pgrep -f start_cam0_quic.py" 2>/dev/null || echo "")
if [[ -n "$QUIC_PID" ]]; then
    echo ""
    echo "✓ QUIC bridge draait op Jetson (PID ${QUIC_PID})"
    echo "  Log bekijken: ssh ${JETSON_USER}@${JETSON} tail -f /tmp/cam0_quic.log"
else
    echo ""
    echo "✗ QUIC bridge start mislukt. Controleer log:"
    $SSH "cat /tmp/cam0_quic.log"
    exit 1
fi

# ── Klaar: instructies voor laptop ────────────────────────────────────────────
echo ""
echo "=== Klaar — start nu op laptop ==="
echo ""
echo "  # Terminal 1 — QUIC bridge op laptop:"
echo "  python3 laptop/cam0_quic_bridge.py --jetson ${JETSON} --insecure"
echo ""
echo "  # Terminal 2 — meting:"
echo "  python3 laptop/calibrate_and_overlay.py \\"
echo "      --jetson 127.0.0.1 --cam0-port 5003 --session quic_clean"
echo ""
echo "Vergelijk daarna met TCP baseline:"
echo "  python3 laptop/calibrate_and_overlay.py \\"
echo "      --jetson ${JETSON} --cam0-port 5001 --session tcp_clean"
