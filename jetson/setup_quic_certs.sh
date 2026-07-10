#!/bin/bash
# Genereer zelf-ondertekende TLS certificaten voor de QUIC bridge.
# Uitvoeren één keer op de Jetson; cert.pem kopiëren naar laptop voor verificatie.
#
# Gebruik:
#   bash setup_quic_certs.sh [JETSON_IP]
#
# Geeft: cert.pem  key.pem  in de huidige map

set -e

JETSON_IP="${1:-192.168.86.47}"
DAYS=3650

echo "Genereer QUIC TLS certificaat voor IP ${JETSON_IP} ..."

openssl req -x509 -newkey rsa:2048 \
  -keyout key.pem -out cert.pem \
  -days "${DAYS}" -nodes \
  -subj "/CN=jetson-quic" \
  -addext "subjectAltName=IP:${JETSON_IP},IP:127.0.0.1,DNS:localhost"

echo ""
echo "Klaar:"
echo "  cert.pem  (${DAYS} dagen geldig)"
echo "  key.pem"
echo ""
echo "Volgende stap — kopieer cert.pem naar laptop:"
echo "  scp jetson@${JETSON_IP}:~/camera-latency-meter/jetson/cert.pem \\"
echo "      ~/camera-latency-meter/jetson/cert.pem"
echo ""
echo "Of start laptop bridge zonder verificatie (insecure mode):"
echo "  python3 cam0_quic_bridge.py --insecure"
