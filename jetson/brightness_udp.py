#!/usr/bin/env python3
"""
Jetson-side brightness server (#A).

Leest cam0 en cam1 via GStreamer/NVMM, berekent ROI brightness,
stuurt UDP packets naar laptop: struct { ts, led0, led1, scr1 }

UDP pakket formaat (40 bytes):
  8 bytes: timestamp (double, seconden)
  4 bytes: cam_id (int, 0=cam0 1=cam1)
  4 bytes: roi_id (int, 0=led 1=scr)
  4 bytes: brightness (float)
  4 bytes: frame_id (int)
  16 bytes: reserved

Start: python3 brightness_udp.py --host 192.168.86.25 --port 5010
"""
import argparse, socket, struct, time, threading
import numpy as np
import json, os, sys

parser = argparse.ArgumentParser()
parser.add_argument("--host",     default="192.168.86.25", help="Laptop IP")
parser.add_argument("--port",     type=int, default=5010)
parser.add_argument("--config",   default="/tmp/roi_config.json")
parser.add_argument("--cam0-src", default="tcp://localhost:5001")  # of direct GStreamer
parser.add_argument("--cam1-src", default="tcp://localhost:5002")
args = parser.parse_args()

# ── ROI config ─────────────────────────────────────────────────────────────
DEFAULT_ROIS = {
    "led0": {"cx": 437, "cy": 174, "r": 7},
    "led1": {"cx": 779, "cy": 463, "r": 10},
    "scr1": {"cx": 849, "cy": 243, "r": 10},
}

def load_rois():
    if os.path.exists(args.config):
        try:
            return json.load(open(args.config))
        except: pass
    return DEFAULT_ROIS

rois = load_rois()
print(f"ROIs: {rois}", flush=True)

# ── UDP socket ──────────────────────────────────────────────────────────────
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
dst = (args.host, args.port)

def send_brightness(cam_id, roi_id, brightness, frame_id, ts=None):
    if ts is None: ts = time.time()
    pkt = struct.pack("!diiif4i", ts, cam_id, roi_id, frame_id, brightness, 0, 0, 0, 0)
    sock.sendto(pkt, dst)

# ── Frame reader ────────────────────────────────────────────────────────────
import cv2

def roi_mean(gray, cfg):
    cx, cy, r = cfg["cx"], cfg["cy"], cfg["r"]
    roi = gray[max(0, cy-r):cy+r, max(0, cx-r):cx+r]
    return float(roi.mean()) if roi.size > 0 else 0.0

def stream_loop(tcp_host, tcp_port, cam_id, roi_keys):
    fc = 0
    while True:
        try:
            s = socket.socket()
            s.connect((tcp_host, tcp_port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(3)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk: break
                buf += chunk
                while True:
                    st = buf.find(b"\xff\xd8")
                    if st == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", st+2)
                    if e == -1: buf = buf[st:]; break
                    frame = buf[st:e+2]; buf = buf[e+2:]
                    ts = time.time()
                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if arr is None: continue
                    rois_now = rois  # live reference
                    for roi_id, key in enumerate(roi_keys):
                        if key in rois_now:
                            b = roi_mean(arr, rois_now[key])
                            send_brightness(cam_id, roi_id, b, fc, ts)
                    fc += 1
            s.close()
        except Exception as ex:
            print(f"cam{cam_id} err: {ex}", flush=True)
            time.sleep(0.3)

# ── Watch config file for ROI updates ──────────────────────────────────────
def config_watcher():
    last_mtime = 0
    while True:
        try:
            mt = os.path.getmtime(args.config)
            if mt != last_mtime:
                last_mtime = mt
                rois.update(load_rois())
                print(f"ROIs herladen: {rois}", flush=True)
        except: pass
        time.sleep(2)

# ── Start threads ───────────────────────────────────────────────────────────
print(f"Brightness UDP → {args.host}:{args.port}", flush=True)
threading.Thread(target=stream_loop, args=("localhost", 5001, 0, ["led0"]), daemon=True).start()
threading.Thread(target=stream_loop, args=("localhost", 5002, 1, ["led1", "scr1"]), daemon=True).start()
threading.Thread(target=config_watcher, daemon=True).start()

fps_counter = [0, time.time()]
while True:
    time.sleep(5)
    # heartbeat
    print(f"alive, rois={list(rois.keys())}", flush=True)
