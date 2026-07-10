#!/usr/bin/env python3
"""
Jetson QUIC cam0 bridge (vervangt start_cam0_tcp.sh voor QUIC transport test).

Leest JPEG frames van de lokale GStreamer TCP stream (start_cam0_tcp.sh, poort 5001)
en stuurt elk frame via QUIC UDP (poort 5011) naar verbonden laptop clients.

Transport protocol:
  - Één QUIC unidirectional stream per JPEG frame (geen head-of-line blocking)
  - Frame payload: 4-byte big-endian lengte + raw JPEG bytes

Requirements:
  pip3 install aioquic
  bash setup_quic_certs.sh [JETSON_IP]   (genereert cert.pem + key.pem)

Gebruik (naast start_cam0_tcp.sh):
  python3 start_cam0_quic.py [--tcp-port 5001] [--quic-port 5011]

Laptop:
  python3 cam0_quic_bridge.py --jetson 192.168.86.47 --quic-port 5011
"""

import argparse
import asyncio
import struct
import os

from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent

parser = argparse.ArgumentParser()
parser.add_argument("--tcp-host",  default="localhost",  help="GStreamer TCP host")
parser.add_argument("--tcp-port",  type=int, default=5001, help="GStreamer TCP poort (start_cam0_tcp.sh)")
parser.add_argument("--listen",    default="0.0.0.0",   help="QUIC listen adres")
parser.add_argument("--quic-port", type=int, default=5011, help="QUIC UDP poort")
parser.add_argument("--cert",      default=os.path.join(os.path.dirname(__file__), "cert.pem"))
parser.add_argument("--key",       default=os.path.join(os.path.dirname(__file__), "key.pem"))
args = parser.parse_args()

# Alle verbonden QUIC clients — elk heeft een eigen asyncio.Queue
_clients: list[asyncio.Queue] = []


async def tcp_reader():
    """Leest JPEG frames van de lokale TCP stream en broadcast naar alle QUIC clients."""
    while True:
        try:
            reader, _ = await asyncio.open_connection(args.tcp_host, args.tcp_port)
            print(f"TCP verbonden: {args.tcp_host}:{args.tcp_port}", flush=True)
            buf = b""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        buf = buf[soi:]
                        break
                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]
                    # Stuur frame naar elke verbonden client (drop als queue vol)
                    for q in list(_clients):
                        try:
                            q.put_nowait(frame)
                        except asyncio.QueueFull:
                            pass  # client te traag — drop, stuur volgende frame
        except Exception as ex:
            print(f"TCP reader fout: {ex}", flush=True)
            await asyncio.sleep(0.3)


class JpegQuicServer(QuicConnectionProtocol):
    """QUIC server protocol: stuurt JPEG frames naar één verbonden client."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=4)

    def connection_made(self, transport):
        super().connection_made(transport)
        _clients.append(self._queue)
        print(f"QUIC client verbonden ({len(_clients)} totaal)", flush=True)
        asyncio.ensure_future(self._send_frames())

    def connection_lost(self, exc):
        if self._queue in _clients:
            _clients.remove(self._queue)
        print(f"QUIC client verbroken ({len(_clients)} over)", flush=True)
        super().connection_lost(exc)

    async def _send_frames(self):
        while True:
            try:
                frame = await self._queue.get()
                # Elk frame krijgt een eigen unidirectional stream → geen HOL blocking
                stream_id = self._quic.get_next_available_stream_id(is_unidirectional=True)
                payload = struct.pack(">I", len(frame)) + frame
                self._quic.send_stream_data(stream_id, payload, end_stream=True)
                self.transmit()
            except Exception:
                break

    def quic_event_received(self, event: QuicEvent):
        pass  # server stuurt alleen; client stuurt niets terug


async def main():
    if not os.path.exists(args.cert) or not os.path.exists(args.key):
        print(f"Certificaat niet gevonden: {args.cert} / {args.key}")
        print("Voer uit: bash setup_quic_certs.sh")
        raise SystemExit(1)

    config = QuicConfiguration(is_client=False, alpn_protocols=["cam-stream-v1"])
    config.load_cert_chain(args.cert, args.key)

    print(f"QUIC server op UDP {args.listen}:{args.quic_port}", flush=True)
    print(f"Frames lezen van TCP {args.tcp_host}:{args.tcp_port}", flush=True)
    print("Wacht op laptop verbinding ...", flush=True)

    asyncio.ensure_future(tcp_reader())

    await serve(
        args.listen, args.quic_port,
        configuration=config,
        create_protocol=JpegQuicServer,
    )
    await asyncio.Future()  # draai voor altijd


if __name__ == "__main__":
    asyncio.run(main())
