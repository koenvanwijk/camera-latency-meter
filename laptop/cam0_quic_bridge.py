#!/usr/bin/env python3
"""
Laptop QUIC cam0 bridge.

Verbindt met de Jetson QUIC server (start_cam0_quic.py) en biedt ontvangen
JPEG frames aan als lokale TCP server op 127.0.0.1:5003.

Gebruik:
  # Terminal 1 — start bridge
  python3 cam0_quic_bridge.py --jetson 192.168.86.47

  # Terminal 2 — meting (--jetson 127.0.0.1 zodat cam0 via bridge gaat)
  python3 calibrate_and_overlay.py --jetson 127.0.0.1 --cam0-port 5003

Requirements: pip3 install aioquic

TLS opties:
  --insecure          skip certificaatverificatie (standaard voor LAN test)
  --cafile cert.pem   verifieer met het Jetson certificaat (scp van Jetson)
"""

import argparse
import asyncio
import ssl
import struct
import sys

from aioquic.asyncio import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",     default="192.168.86.47", help="Jetson IP")
parser.add_argument("--quic-port",  type=int, default=5011,  help="QUIC UDP poort op Jetson")
parser.add_argument("--local-port", type=int, default=5003,  help="Lokale TCP poort voor calibrate_and_overlay.py")
parser.add_argument("--cafile",     default=None,            help="CA cert voor verificatie (scp cert.pem van Jetson)")
parser.add_argument("--insecure",   action="store_true",     help="Skip TLS verificatie (zelf-ondertekend)")
args = parser.parse_args()

# Alle verbonden lokale TCP clients (asyncio StreamWriter)
_tcp_writers: list[asyncio.StreamWriter] = []


class JpegQuicClient(QuicConnectionProtocol):
    """QUIC client: ontvangt JPEG frames en schrijft ze naar alle lokale TCP clients."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream_bufs: dict[int, bytearray] = {}

    def quic_event_received(self, event):
        if not isinstance(event, StreamDataReceived):
            return

        sid = event.stream_id
        if sid not in self._stream_bufs:
            self._stream_bufs[sid] = bytearray()
        self._stream_bufs[sid] += event.data

        if not event.end_stream:
            return

        data = bytes(self._stream_bufs.pop(sid))
        if len(data) < 4:
            return
        length = struct.unpack(">I", data[:4])[0]
        frame = data[4:4 + length]
        if len(frame) != length:
            return

        # Schrijf raw JPEG frame naar alle verbonden TCP clients
        # cam0_loop in calibrate_and_overlay.py scant op \xff\xd8 / \xff\xd9 markers
        dead = []
        for w in list(_tcp_writers):
            try:
                w.write(frame)
            except Exception:
                dead.append(w)
        for w in dead:
            if w in _tcp_writers:
                _tcp_writers.remove(w)


async def local_tcp_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Behandel één verbinding van calibrate_and_overlay.py."""
    addr = writer.get_extra_info("peername")
    print(f"Lokale TCP client verbonden: {addr}", flush=True)
    _tcp_writers.append(writer)
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break  # client verbroken
    except Exception:
        pass
    finally:
        if writer in _tcp_writers:
            _tcp_writers.remove(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    print(f"Lokale TCP client verbroken: {addr}", flush=True)


async def main():
    # Lokale TCP server starten
    server = await asyncio.start_server(
        local_tcp_server, "127.0.0.1", args.local_port
    )
    print(f"Lokale TCP server: 127.0.0.1:{args.local_port}", flush=True)
    print(f"  → Start calibrate_and_overlay.py met:", flush=True)
    print(f"    --jetson 127.0.0.1 --cam0-port {args.local_port}", flush=True)

    # QUIC configuratie
    config = QuicConfiguration(is_client=True, alpn_protocols=["cam-stream-v1"])
    if args.insecure:
        config.verify_mode = ssl.CERT_NONE
    elif args.cafile:
        config.cafile = args.cafile
    else:
        print("Waarschuwing: geen --cafile of --insecure. Gebruik --insecure voor zelf-ondertekend cert.")

    print(f"Verbinden met QUIC {args.jetson}:{args.quic_port} ...", flush=True)

    async with server:
        while True:
            try:
                async with connect(
                    args.jetson, args.quic_port,
                    configuration=config,
                    create_protocol=JpegQuicClient,
                ) as client:
                    print("QUIC verbonden — frames ontvangen", flush=True)
                    await client.wait_closed()
                    print("QUIC verbroken, opnieuw proberen ...", flush=True)
            except Exception as ex:
                print(f"QUIC fout: {ex} — retry in 1s", flush=True)
                await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
