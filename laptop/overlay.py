#!/usr/bin/env python3
"""
Camera latency overlay — laptop side.

Reads cam0 raw JPEG stream from Jetson via TCP (60fps, minimal buffering).
Uses Gemini Vision to auto-detect the LED position at startup.
Falls back to variance-based detection if Gemini is unavailable.
Shows full-screen white/black overlay on external display.

Usage:
    DISPLAY=:1 python3 overlay.py [--jetson 192.168.86.47] [--port 5001]

Requirements:
    pip install numpy pillow opencv-python-headless
    export GEMINI_API_KEY=your_key  (optional, enables auto-detect)
"""

import argparse, socket, time, threading, base64, json
import urllib.request
import numpy as np
import tkinter as tk
from PIL import Image, ImageTk
import cv2
import os

# ── Config ────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--jetson", default="192.168.86.47")
parser.add_argument("--port",   type=int, default=5001)
parser.add_argument("--display-x", type=int, default=0,    help="Overlay X offset")
parser.add_argument("--display-y", type=int, default=0,    help="Overlay Y offset")
parser.add_argument("--width",  type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
args = parser.parse_args()

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyC2PBKX4PqU3nr7GRBdkUCkymll2ulesJ8")
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
)

# Fallback LED position (overridden by auto-detect)
led = {"cx": 460, "cy": 164, "r": 8, "thr": 35.0}

current_state = [False]
last_state    = [None]
ready         = threading.Event()

# ── Tkinter window ────────────────────────────────────────────────────────────

root = tk.Tk()
root.geometry(f"{args.width}x{args.height}+{args.display_x}+{args.display_y}")
root.attributes("-fullscreen", True)
root.overrideredirect(True)
root.configure(bg="black")

canvas = tk.Canvas(root, bg="black", highlightthickness=0)
canvas.pack(fill="both", expand=True)

WHITE = ImageTk.PhotoImage(Image.new("RGB", (args.width, args.height), (255, 255, 255)))
BLACK = ImageTk.PhotoImage(Image.new("RGB", (args.width, args.height), (0,   0,   0)))
img_item = canvas.create_image(0, 0, anchor="nw", image=BLACK)

status_var = tk.StringVar(value="Initializing...")
tk.Label(root, textvariable=status_var, fg="yellow", bg="black",
         font=("monospace", 14)).place(x=10, y=10)

def update_display():
    if ready.is_set():
        s = current_state[0]
        if s != last_state[0]:
            last_state[0] = s
            canvas.itemconfig(img_item, image=WHITE if s else BLACK)
    root.after(4, update_display)  # ~250Hz polling

# ── Gemini LED detection ──────────────────────────────────────────────────────

def gemini_find_led(img_bgr):
    """Ask Gemini to locate the LED. Returns (cx, cy) or None."""
    h, w = img_bgr.shape[:2]
    jpg = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    b64 = base64.b64encode(jpg).decode()
    prompt = (
        f"This is a {w}x{h}px camera image from a robotics latency measurement setup. "
        "A small LED is mounted in the scene and blinks periodically. "
        "Find the LED (a small bright spot, possibly a white/yellow/red dot). "
        "Return ONLY a JSON object: {\"cx\": <x_pixel>, \"cy\": <y_pixel>}. "
        "No markdown, no explanation."
    )
    body = json.dumps({"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = text.strip("`").strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    r = json.loads(text)
    cx, cy = int(r["cx"]), int(r["cy"])
    # Validate within frame
    if 0 <= cx < w and 0 <= cy < h:
        return cx, cy
    print(f"  Gemini returned out-of-frame coords ({cx},{cy}) for {w}x{h} — ignoring")
    return None

def variance_find_led(frames_gray):
    """Find LED via max variance pixel cluster across frames."""
    stack  = np.stack(frames_gray)
    spread = stack.max(axis=0).astype(float) - stack.min(axis=0).astype(float)
    y, x   = np.unravel_index(spread.argmax(), spread.shape)
    return int(x), int(y), float(spread[y, x])

# ── Grab a single frame from TCP ─────────────────────────────────────────────

def grab_frame():
    try:
        sock = socket.socket()
        sock.connect((args.jetson, args.port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(5)
        buf = b""
        while True:
            buf += sock.recv(65536)
            s = buf.find(b"\xff\xd8")
            if s == -1: buf = b""; continue
            e = buf.find(b"\xff\xd9", s + 2)
            if e == -1: buf = buf[s:]; continue
            frame = buf[s:e + 2]
            sock.close()
            return cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
    except Exception as ex:
        print(f"grab_frame error: {ex}")
        return None

# ── Threshold calibration ─────────────────────────────────────────────────────

def calibrate_threshold(n_frames=150, duration=3.0):
    """Collect frames, compute bimodal threshold for LED ROI."""
    print("Calibrating threshold...", flush=True)
    try:
        sock = socket.socket()
        sock.connect((args.jetson, args.port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(2)
        buf = b""; vals = []; gray_frames = []; t = time.time()
        while time.time() - t < duration and len(vals) < n_frames:
            buf += sock.recv(65536)
            while True:
                s = buf.find(b"\xff\xd8")
                if s == -1: buf = b""; break
                e = buf.find(b"\xff\xd9", s + 2)
                if e == -1: buf = buf[s:]; break
                frame = buf[s:e + 2]; buf = buf[e + 2:]
                arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
                if arr is None: continue
                gray_frames.append(arr)
                cx, cy, r = led["cx"], led["cy"], led["r"]
                vals.append(float(arr[max(0,cy-r):cy+r, max(0,cx-r):cx+r].mean()))
        sock.close()

        if len(vals) > 10:
            mn, mx = min(vals), max(vals)
            spread = mx - mn
            if spread > 5:
                led["thr"] = (mn + mx) / 2
                print(f"  Threshold: {led['thr']:.1f} (range {mn:.1f}–{mx:.1f})", flush=True)
            else:
                print(f"  Spread too small ({spread:.1f}) — keeping fallback {led['thr']}", flush=True)
        return gray_frames
    except Exception as ex:
        print(f"calibrate error: {ex}", flush=True)
        return []

# ── Init thread ───────────────────────────────────────────────────────────────

def init_loop():
    root.after(0, status_var.set, "Grabbing frame for Gemini...")
    print("=== Auto-detecting LED position ===", flush=True)

    frame = grab_frame()
    detected = False

    if frame is not None and GEMINI_KEY:
        root.after(0, status_var.set, "Asking Gemini to find LED...")
        try:
            result = gemini_find_led(frame)
            if result:
                led["cx"], led["cy"] = result
                print(f"  Gemini: LED @ ({led['cx']},{led['cy']})", flush=True)
                detected = True
        except Exception as ex:
            print(f"  Gemini failed: {ex} — falling back to variance", flush=True)

    if not detected:
        root.after(0, status_var.set, "Collecting frames for variance detect...")
        print("  Using variance detection...", flush=True)
        gray_frames = calibrate_threshold(n_frames=120, duration=4.0)
        if gray_frames:
            cx, cy, sp = variance_find_led(gray_frames)
            if sp > 20:
                led["cx"], led["cy"] = cx, cy
                print(f"  Variance: LED @ ({cx},{cy}) spread={sp:.0f}", flush=True)
                detected = True

    # Always calibrate threshold (catches current lighting conditions)
    root.after(0, status_var.set, "Calibrating threshold...")
    calibrate_threshold(n_frames=150, duration=3.0)

    msg = (f"LED=({led['cx']},{led['cy']}) thr={led['thr']:.0f} "
           f"{'[Gemini]' if detected else '[variance]'}")
    print(f"Ready! {msg}", flush=True)
    root.after(0, status_var.set, msg)
    ready.set()

# ── Main stream loop ──────────────────────────────────────────────────────────

def stream_loop():
    fc = 0; t0 = time.time()
    while True:
        try:
            sock = socket.socket()
            sock.connect((args.jetson, args.port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # IPTOS_LOWDELAY
            sock.settimeout(3)
            buf = b""
            print(f"Connected to {args.jetson}:{args.port}", flush=True)

            while True:
                chunk = sock.recv(65536)
                if not chunk: break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e == -1: buf = buf[s:]; break
                    frame = buf[s:e + 2]; buf = buf[e + 2:]
                    if not ready.is_set(): continue

                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if arr is None: continue

                    cx, cy, r = led["cx"], led["cy"], led["r"]
                    roi = arr[max(0, cy-r):cy+r, max(0, cx-r):cx+r]
                    current_state[0] = roi.mean() > led["thr"]

                    fc += 1
                    if fc % 120 == 0:
                        fps = fc / (time.time() - t0)
                        root.after(0, status_var.set,
                                   f"fps={fps:.0f} led={'ON' if current_state[0] else 'OFF'} "
                                   f"LED=({led['cx']},{led['cy']}) thr={led['thr']:.0f}")
            sock.close()
        except Exception as ex:
            if ready.is_set():
                print(f"Stream error: {ex}", flush=True)
            time.sleep(0.3)

# ── Start ─────────────────────────────────────────────────────────────────────

threading.Thread(target=stream_loop, daemon=True).start()
threading.Thread(target=init_loop,   daemon=True).start()

root.after(4, update_display)
root.bind("<Escape>", lambda e: root.destroy())
print(f"Overlay started on DISPLAY={os.environ.get('DISPLAY','?')} "
      f"at {args.display_x},{args.display_y}", flush=True)
root.mainloop()
