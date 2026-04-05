#!/usr/bin/env python3
"""
End-to-end latency measurement.

Reads cam0 overlay stream (white=LED on, black=LED off) and cam1 stream.
cam1 detects: (1) the LED directly, (2) the screen showing the overlay.
Measures: LED seen by cam1 → screen change seen by cam1 = full pipeline latency.

Usage:
    python3 latency_measure.py [--jetson 192.168.86.47] [--duration 60]

Output:
    Per-transition delay + statistics (mean/min/max/stddev)
"""

import argparse, socket, time, threading, urllib.request
import numpy as np
import cv2
import os

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",   default="192.168.86.47")
parser.add_argument("--cam0-port", type=int, default=5001)
parser.add_argument("--cam1-url",  default="http://192.168.86.47:8091/stream")
parser.add_argument("--duration",  type=int, default=60)
# LED positions (auto-calibrated if not given)
parser.add_argument("--led0-cx", type=int, default=460)
parser.add_argument("--led0-cy", type=int, default=164)
parser.add_argument("--led0-r",  type=int, default=8)
parser.add_argument("--led1-cx", type=int, default=869)
parser.add_argument("--led1-cy", type=int, default=349)
parser.add_argument("--led1-r",  type=int, default=10)
args = parser.parse_args()

events   = []  # [(time, event_type)]
ev_lock  = threading.Lock()
t_start  = [None]

SCR1 = {"cx": 0, "cy": 0, "r": 30, "thr": 0}  # cam1 screen ROI (auto-detected)
LED0 = {"cx": args.led0_cx, "cy": args.led0_cy, "r": args.led0_r, "thr": 35.0}
LED1 = {"cx": args.led1_cx, "cy": args.led1_cy, "r": args.led1_r, "thr": 30.0}

def log(ev, t=None):
    t = t or (time.time() - t_start[0])
    with ev_lock:
        events.append((t, ev))
    print(f"  {t:8.3f}s  {ev}", flush=True)

# ── cam0: detect screen state (white/black overlay) ──────────────────────────

def cam0_loop():
    last = None
    while time.time() - t_start[0] < args.duration:
        try:
            sock = socket.socket()
            sock.connect((args.jetson, args.cam0_port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(3)
            buf = b""
            while time.time() - t_start[0] < args.duration:
                buf += sock.recv(65536)
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e == -1: buf = buf[s:]; break
                    frame = buf[s:e+2]; buf = buf[e+2:]
                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if arr is None: continue
                    cx, cy, r = LED0["cx"], LED0["cy"], LED0["r"]
                    v = float(arr[max(0,cy-r):cy+r, max(0,cx-r):cx+r].mean())
                    on = v > LED0["thr"]
                    if on != last:
                        log(f"CAM0_SCR_{'ON' if on else 'OFF'} v={v:.1f}")
                        last = on
            sock.close()
        except Exception as ex:
            print(f"cam0 error: {ex}", flush=True)
            time.sleep(0.2)

# ── cam1: detect LED + screen ─────────────────────────────────────────────────

def cam1_loop():
    calib_frames = []
    calibrated   = False
    led_last     = None
    scr_last     = None
    scr_thr      = [None]

    while time.time() - t_start[0] < args.duration:
        try:
            req = urllib.request.urlopen(args.cam1_url, timeout=10)
            buf = b""
            while time.time() - t_start[0] < args.duration:
                chunk = req.read(32768)
                if not chunk: break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e == -1: buf = buf[s:]; break
                    frame = buf[s:e+2]; buf = buf[e+2:]
                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if arr is None: continue
                    t = time.time() - t_start[0]

                    # Collect frames for screen ROI auto-detection (first 5s)
                    if not calibrated:
                        calib_frames.append(arr)
                        if len(calib_frames) >= 150:
                            stack  = np.stack(calib_frames)
                            spread = stack.max(axis=0).astype(float) - stack.min(axis=0).astype(float)
                            # Mask out LED zone
                            lx, ly = LED1["cx"], LED1["cy"]
                            spread[max(0,ly-50):ly+50, max(0,lx-50):lx+50] = 0
                            y, x = np.unravel_index(spread.argmax(), spread.shape)
                            SCR1["cx"], SCR1["cy"] = int(x), int(y)
                            vals = stack[:, max(0,y-20):y+20, max(0,x-20):x+20].mean(axis=(1,2))
                            scr_thr[0] = float((vals.min() + vals.max()) / 2)
                            print(f"  cam1 screen ROI: ({x},{y}) spread={spread[y,x]:.0f} thr={scr_thr[0]:.0f}", flush=True)
                            calibrated = True

                    # LED detection
                    cx, cy, r = LED1["cx"], LED1["cy"], LED1["r"]
                    led_roi = arr[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    led_on  = led_roi.mean() > LED1["thr"]
                    if led_on != led_last:
                        log(f"LED1_{'ON' if led_on else 'OFF'}", t)
                        led_last = led_on

                    # Screen detection
                    if calibrated and scr_thr[0] is not None:
                        sx, sy = SCR1["cx"], SCR1["cy"]
                        sr     = SCR1["r"]
                        scr_roi = arr[max(0,sy-sr):sy+sr, max(0,sx-sr):sx+sr]
                        scr_on  = scr_roi.mean() > scr_thr[0]
                        if scr_on != scr_last:
                            log(f"SCR1_{'ON' if scr_on else 'OFF'}", t)
                            scr_last = scr_on
            req.close()
        except Exception as ex:
            print(f"cam1 error: {ex}", flush=True)
            time.sleep(0.2)

# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse():
    with ev_lock:
        evs = sorted(events)

    print(f"\n{'='*60}")
    print(f"All events ({len(evs)} total):")
    for t, ev in evs:
        print(f"  {t:8.3f}s  {ev}")

    # Full end-to-end: LED1_ON → SCR1_ON
    led1_ons = [t for t, e in evs if e == "LED1_ON"]
    scr1_ons = [t for t, e in evs if e == "SCR1_ON"]

    print(f"\nLED1_ON: {len(led1_ons)}  SCR1_ON: {len(scr1_ons)}")
    delays = []
    for lt in led1_ons:
        cands = [st - lt for st in scr1_ons if 0.010 < st - lt < 3.0]
        if cands:
            d = min(cands)
            delays.append(d)
            print(f"  LED@{lt:.3f} → SCR delay = {d*1000:.0f}ms")

    if delays:
        arr = np.array(delays) * 1000
        print(f"\n✅ End-to-end latency (ESP32 LED → cam1 sees screen):")
        print(f"   n={len(arr)}  mean={arr.mean():.0f}ms  "
              f"min={arr.min():.0f}ms  max={arr.max():.0f}ms  "
              f"std={arr.std():.0f}ms")
    else:
        print("\n⚠️  No LED1→SCR1 pairs found.")
        print("   Check: is screen visible on cam1? Is overlay running?")

# ── Main ──────────────────────────────────────────────────────────────────────

print(f"Starting measurement for {args.duration}s")
print(f"  cam0 TCP: {args.jetson}:{args.cam0_port}")
print(f"  cam1 URL: {args.cam1_url}")
print(f"  LED0: ({LED0['cx']},{LED0['cy']}) r={LED0['r']} thr={LED0['thr']}")
print(f"  LED1: ({LED1['cx']},{LED1['cy']}) r={LED1['r']} thr={LED1['thr']}")
print("Collecting calibration data (5s)...\n")

t_start[0] = time.time()

th0 = threading.Thread(target=cam0_loop, daemon=True)
th1 = threading.Thread(target=cam1_loop, daemon=True)
th0.start(); th1.start()
th0.join(); th1.join()

analyse()
