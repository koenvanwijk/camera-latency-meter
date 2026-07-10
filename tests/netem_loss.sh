#!/bin/bash
# tc netem packet loss simulator — uitvoeren op de JETSON
# (simuleert slechte netwerkverbinding op het uitgaande interface naar laptop)
#
# Gebruik:
#   bash netem_loss.sh add  5 wlan0    # 5% pakketverlies toevoegen
#   bash netem_loss.sh add 10 wlan0    # 10% pakketverlies
#   bash netem_loss.sh del    wlan0    # verlies verwijderen
#   bash netem_loss.sh status wlan0    # huidige regels tonen
#
# Vind het juiste interface:
#   ip route get <LAPTOP_IP>           # toont welk interface gebruikt wordt

set -e

ACTION="${1:-status}"
LOSS="${2:-5}"
IFACE="${3:-wlan0}"

case "$ACTION" in
  add)
    echo "Voeg ${LOSS}% pakketverlies toe op ${IFACE} ..."
    # Verwijder eerst eventueel bestaande qdisc om fouten te voorkomen
    sudo tc qdisc del dev "${IFACE}" root 2>/dev/null || true
    sudo tc qdisc add dev "${IFACE}" root netem loss "${LOSS}%"
    echo "Actief:"
    tc qdisc show dev "${IFACE}"
    ;;
  del)
    echo "Verwijder pakketverlies van ${IFACE} ..."
    sudo tc qdisc del dev "${IFACE}" root 2>/dev/null || echo "(geen qdisc actief)"
    echo "Klaar — verbinding is weer clean."
    ;;
  status)
    echo "Huidige qdisc voor ${IFACE}:"
    tc qdisc show dev "${IFACE}"
    ;;
  *)
    echo "Gebruik: $0 {add|del|status} [LOSS_%] [INTERFACE]"
    exit 1
    ;;
esac
