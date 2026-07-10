#!/bin/bash
# Transport vergelijking: TCP vs QUIC, clean vs pakketverlies
#
# Meet glass-to-glass latency voor alle sessie-labels uit TELEOPERATION_PLATFORMS.md:
#   tcp_clean, tcp_5loss, tcp_10loss
#   quic_clean, quic_5loss, quic_10loss
#   (adamo_clean, adamo_5loss, adamo_10loss — zie Adamo sectie hieronder)
#
# Vereisten VOOR gebruik:
#   Jetson: start_cam0_tcp.sh draait op poort 5001
#   Jetson: start_cam0_quic.py draait op UDP poort 5011
#   Laptop: cam0_quic_bridge.py beschikbaar
#   Laptop: pip install aioquic
#
# Configuratie (overschrijf met env vars):
#   JETSON=192.168.86.47  IFACE=wlan0  SESSION_SECS=120  bash run_comparison.sh

set -e

JETSON="${JETSON:-192.168.86.47}"
IFACE="${IFACE:-wlan0}"          # interface op JETSON voor netem (SSH vereist)
SESSION_SECS="${SESSION_SECS:-120}"  # seconden per sessie
JETSON_USER="${JETSON_USER:-jetson}"

LAPTOP_DIR="$(cd "$(dirname "$0")/.." && pwd)/laptop"
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Camera Latency Meter — Transport Vergelijking ==="
echo "Jetson:       ${JETSON}"
echo "Interface:    ${IFACE}"
echo "Sessie duur:  ${SESSION_SECS}s per sessie"
echo ""
echo "Stap 1: Controleer dat de Jetson services draaien"
echo "  SSH: python3 ~/camera-latency-meter/jetson/start_cam0_quic.py &"
echo ""
read -rp "Klaar? Druk Enter om te beginnen ..."

# ── Helper: run één meting ─────────────────────────────────────────────────────

run_session() {
    local label="$1"
    local cam0_host="$2"
    local cam0_port="$3"
    local bridge_pid=""

    echo ""
    echo "── Sessie: ${label} (${SESSION_SECS}s) ──"

    # Start QUIC bridge als we via QUIC meten
    if [[ "$cam0_host" == "127.0.0.1" ]]; then
        python3 "${LAPTOP_DIR}/cam0_quic_bridge.py" \
            --jetson "${JETSON}" --local-port "${cam0_port}" --insecure &
        bridge_pid=$!
        sleep 2   # bridge tijd geven om te verbinden
    fi

    # Start meting (--auto start threshold-kalibratie automatisch)
    timeout "${SESSION_SECS}" \
        python3 "${LAPTOP_DIR}/calibrate_and_overlay.py" \
            --jetson "${cam0_host}" \
            --cam0-port "${cam0_port}" \
            --session "${label}" \
            --auto \
        || true

    if [[ -n "$bridge_pid" ]]; then
        kill "$bridge_pid" 2>/dev/null || true
    fi

    echo "✓ ${label} klaar"
}

# Packet loss instellen via SSH naar Jetson
apply_loss() {
    local pct="$1"
    if [[ "$pct" -eq 0 ]]; then
        echo "Verwijder pakketverlies op Jetson (${IFACE}) ..."
        ssh "${JETSON_USER}@${JETSON}" \
            "sudo tc qdisc del dev ${IFACE} root 2>/dev/null; echo clean"
    else
        echo "Stel ${pct}% pakketverlies in op Jetson (${IFACE}) ..."
        ssh "${JETSON_USER}@${JETSON}" \
            "sudo tc qdisc del dev ${IFACE} root 2>/dev/null; \
             sudo tc qdisc add dev ${IFACE} root netem loss ${pct}%; \
             tc qdisc show dev ${IFACE}"
    fi
}

# ── TCP sessies ────────────────────────────────────────────────────────────────

echo ""
echo "=== TCP sessies ==="

apply_loss 0
run_session "tcp_clean"  "${JETSON}" 5001

apply_loss 5
run_session "tcp_5loss"  "${JETSON}" 5001

apply_loss 10
run_session "tcp_10loss" "${JETSON}" 5001

apply_loss 0

# ── QUIC sessies ───────────────────────────────────────────────────────────────

echo ""
echo "=== QUIC sessies ==="
echo "Controleer: start_cam0_quic.py draait op Jetson poort 5011"
read -rp "Klaar? Druk Enter ..."

apply_loss 0
run_session "quic_clean"  "127.0.0.1" 5003

apply_loss 5
run_session "quic_5loss"  "127.0.0.1" 5003

apply_loss 10
run_session "quic_10loss" "127.0.0.1" 5003

apply_loss 0

# ── Adamo sessies (handmatig — zie instructies) ────────────────────────────────
#
# Test 1 uit TELEOPERATION_PLATFORMS.md:
#   1. Vraag Adamo trial aan op adamohq.com
#   2. Installeer Adamo agent op Jetson: bash adamo_agent_install.sh
#   3. Configureer cam0 device in Adamo dashboard
#   4. Start Adamo client op laptop (hun interface)
#   5. Voer onderstaande sessies handmatig uit:
#
#      apply_loss 0  && run_session "adamo_clean"  "127.0.0.1" 5004  # Adamo→local bridge
#      apply_loss 5  && run_session "adamo_5loss"  "127.0.0.1" 5004
#      apply_loss 10 && run_session "adamo_10loss" "127.0.0.1" 5004
#      apply_loss 0
#
# (Adamo local bridge: ffmpeg -i <adamo_stream_url> -f mjpeg pipe:1 | nc -l 5004)

# ── Vergelijking ───────────────────────────────────────────────────────────────

echo ""
echo "=== Resultaten vergelijken ==="
python3 "${LAPTOP_DIR}/compare_sessions.py"

echo ""
echo "=== Klaar ==="
echo "Verwachte volgorde (laag → hoog onder 10% verlies):"
echo "  quic_10loss < tcp_10loss"
echo "  (als adamo_10loss beschikbaar: adamo_10loss ≈ 133ms, quic_10loss < 40ms doel)"
