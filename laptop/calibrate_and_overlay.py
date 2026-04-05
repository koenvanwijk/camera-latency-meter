#!/usr/bin/env python3
"""
Camera Latency Meter — Calibration + Overlay

Layout:
  ┌─────────────────────┬─────────────────────┐
  │  CAM0               │  CAM1               │
  │  Ziet: LED          │  Ziet: LED + SCHERM │
  │  Taak: overlay      │  Taak: METEN        │
  │  sturen             │  (volledige loop)   │
  └─────────────────────┴─────────────────────┘

Keys:
  SPACE  → start overlay mode (wit/zwart op scherm)
  R      → Gemini LED auto-detect (beide cameras)
  T      → auto-threshold (3s)
  Klik   → verplaats LED cirkel (op actieve cam)
  Scroll → vergroot/verklein ROI
  ESC    → afsluiten
"""

import argparse, socket, time, threading, base64, json
import urllib.request, os
import numpy as np
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageDraw, ImageFont
import cv2

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",    default="192.168.86.47")
parser.add_argument("--cam0-port", type=int, default=5001)
parser.add_argument("--cam1-url",  default="http://192.168.86.47:8091/stream")
parser.add_argument("--win-x",     type=int, default=0)
parser.add_argument("--win-y",     type=int, default=0)
args = parser.parse_args()

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "AIzaSyC2PBKX4PqU3nr7GRBdkUCkymll2ulesJ8")
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL   = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")

WIN_W, WIN_H = 1920, 1080
PANEL_W = WIN_W // 2   # 960 per camera
CAM_H   = 720          # camera display height
TOP_H   = WIN_H - CAM_H  # header height

# ── Shared state ──────────────────────────────────────────────────────────────
mode = ["calibrate"]  # "calibrate" | "overlay"

cam0_frame = [None]; cam0_lock = threading.Lock()
cam1_frame = [None]; cam1_lock = threading.Lock()

led0 = {"cx": 460, "cy": 164, "r": 12, "thr": 35.0}
led1 = {"cx": 869, "cy": 349, "r": 12, "thr": 30.0}

cam0_bright = [0.0]; cam0_on = [False]; cam0_fps = [0.0]
cam1_bright = [0.0]; cam1_on = [False]; cam1_fps = [0.0]

status_msg = ["Press R to auto-detect LEDs with Gemini"]
gemini_running = [False]

# ── TCP cam0 stream ───────────────────────────────────────────────────────────
def cam0_loop():
    fc = 0; t0 = time.time()
    while True:
        try:
            sock = socket.socket()
            sock.connect((args.jetson, args.cam0_port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(3)
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk: break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", s+2)
                    if e == -1: buf = buf[s:]; break
                    frame = buf[s:e+2]; buf = buf[e+2:]
                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
                    if arr is None: continue
                    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
                    cx, cy, r = led0["cx"], led0["cy"], led0["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    v = float(roi.mean()) if roi.size > 0 else 0.0
                    cam0_bright[0] = v
                    cam0_on[0] = v > led0["thr"]
                    with cam0_lock: cam0_frame[0] = arr
                    fc += 1
                    if fc % 30 == 0:
                        cam0_fps[0] = 30 / (time.time()-t0)
                        t0 = time.time(); fc = 0
            sock.close()
        except Exception as ex:
            time.sleep(0.3)

# ── MJPEG cam1 stream ─────────────────────────────────────────────────────────
def cam1_loop():
    fc = 0; t0 = time.time()
    while True:
        try:
            req = urllib.request.urlopen(args.cam1_url, timeout=10)
            buf = b""
            while True:
                chunk = req.read(32768)
                if not chunk: break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s == -1: buf = b""; break
                    e = buf.find(b"\xff\xd9", s+2)
                    if e == -1: buf = buf[s:]; break
                    frame = buf[s:e+2]; buf = buf[e+2:]
                    arr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
                    if arr is None: continue
                    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
                    cx, cy, r = led1["cx"], led1["cy"], led1["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    v = float(roi.mean()) if roi.size > 0 else 0.0
                    cam1_bright[0] = v
                    cam1_on[0] = v > led1["thr"]
                    with cam1_lock: cam1_frame[0] = arr
                    fc += 1
                    if fc % 30 == 0:
                        cam1_fps[0] = 30 / (time.time()-t0)
                        t0 = time.time(); fc = 0
            req.close()
        except Exception as ex:
            time.sleep(0.3)

threading.Thread(target=cam0_loop, daemon=True).start()
threading.Thread(target=cam1_loop, daemon=True).start()

# ── Gemini ────────────────────────────────────────────────────────────────────
def gemini_detect_led(img_bgr, label):
    h, w = img_bgr.shape[:2]
    jpg = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    b64 = base64.b64encode(jpg).decode()
    prompt = (
        f"Camera image {w}x{h}px, robotics latency setup. "
        "Find the small blinking LED light (bright spot). "
        "Return ONLY JSON: {\"cx\": <x>, \"cy\": <y>} in 0-1000 normalized coords. No markdown."
    )
    body = json.dumps({"contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip().strip("`").strip()
    if text.lower().startswith("json"): text = text[4:].strip()
    r = json.loads(text)
    raw_cx, raw_cy = int(r["cx"]), int(r["cy"])
    # Scale from 0-1000 to pixels
    cx = int(raw_cx / 1000 * w)
    cy = int(raw_cy / 1000 * h)
    print(f"Gemini {label}: raw=({raw_cx},{raw_cy}) → pixels=({cx},{cy})", flush=True)
    return cx, cy

def run_gemini():
    if gemini_running[0]: return
    gemini_running[0] = True
    def _run():
        status_msg[0] = "Gemini: detecting LEDs..."
        errors = []
        with cam0_lock:
            f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        with cam1_lock:
            f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None
        if f0 is not None:
            try:
                cx, cy = gemini_detect_led(f0, "cam0")
                led0["cx"], led0["cy"] = cx, cy
            except Exception as ex: errors.append(f"cam0:{ex}")
        else: errors.append("cam0: no frame")
        if f1 is not None:
            try:
                cx, cy = gemini_detect_led(f1, "cam1")
                led1["cx"], led1["cy"] = cx, cy
            except Exception as ex: errors.append(f"cam1:{ex}")
        else: errors.append("cam1: no frame")
        if errors:
            status_msg[0] = "Gemini errors: " + " | ".join(errors)
        else:
            status_msg[0] = f"Gemini ✓  cam0=({led0['cx']},{led0['cy']})  cam1=({led1['cx']},{led1['cy']})"
        gemini_running[0] = False
    threading.Thread(target=_run, daemon=True).start()

def run_autothreshold():
    def _run():
        status_msg[0] = "Auto-threshold: collecting 3s..."
        vals0, vals1 = [], []
        t = time.time()
        while time.time()-t < 3.5:
            vals0.append(cam0_bright[0])
            vals1.append(cam1_bright[0])
            time.sleep(0.02)
        for vals, led, name in [(vals0, led0, "cam0"), (vals1, led1, "cam1")]:
            mn, mx = min(vals), max(vals)
            if mx - mn > 4:
                led["thr"] = (mn + mx) / 2
                print(f"{name} threshold: {led['thr']:.1f} (range {mn:.1f}–{mx:.1f})")
        status_msg[0] = (f"Threshold ✓  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}")
    threading.Thread(target=_run, daemon=True).start()

# ── Draw annotated camera frame ────────────────────────────────────────────────
def draw_cam_panel(frame_bgr, led_cfg, bright, on, fps, role_title, role_desc, color):
    """Returns PIL image sized PANEL_W x CAM_H with annotations."""
    if frame_bgr is None:
        img = Image.new("RGB", (PANEL_W, CAM_H), (30, 30, 30))
        draw = ImageDraw.Draw(img)
        draw.text((PANEL_W//2 - 80, CAM_H//2 - 10), "Waiting for stream...", fill=(150,150,150))
        return img

    h, w = frame_bgr.shape[:2]
    ratio = min(PANEL_W/w, CAM_H/h)
    nw, nh = int(w*ratio), int(h*ratio)
    ox = (PANEL_W - nw) // 2
    oy = (CAM_H  - nh) // 2

    resized = cv2.resize(frame_bgr, (nw, nh))
    img = Image.new("RGB", (PANEL_W, CAM_H), (20, 20, 20))
    cam_img = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
    img.paste(cam_img, (ox, oy))
    draw = ImageDraw.Draw(img)

    # LED circle on frame
    cx_d = int(led_cfg["cx"] * ratio) + ox
    cy_d = int(led_cfg["cy"] * ratio) + oy
    r_d  = max(5, int(led_cfg["r"] * ratio))
    ring_col = (0, 255, 80) if on else (255, 60, 60)
    draw.ellipse([cx_d-r_d, cy_d-r_d, cx_d+r_d, cy_d+r_d], outline=ring_col, width=3)
    draw.line([(cx_d-r_d-8, cy_d), (cx_d+r_d+8, cy_d)], fill=ring_col, width=1)
    draw.line([(cx_d, cy_d-r_d-8), (cx_d, cy_d+r_d+8)], fill=ring_col, width=1)
    draw.text((cx_d+r_d+6, cy_d-8), f"{bright:.0f}", fill=ring_col)

    # Bottom info bar
    bar_y = CAM_H - 44
    draw.rectangle([0, bar_y, PANEL_W, CAM_H], fill=(0,0,0))
    led_col = (0,255,80) if on else (255,60,60)
    led_txt = "● LED ON " if on else "○ LED OFF"
    draw.text((10, bar_y+6),  led_txt, fill=led_col)
    draw.text((160, bar_y+6), f"thr={led_cfg['thr']:.0f}  pos=({led_cfg['cx']},{led_cfg['cy']})", fill=(180,180,180))
    draw.text((PANEL_W-90, bar_y+6), f"{fps:.0f} fps", fill=(120,120,120))

    return img, ox, oy, ratio

# ── Tkinter ───────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Camera Latency Meter")
root.geometry(f"{WIN_W}x{WIN_H}+{args.win_x}+{args.win_y}")
root.attributes("-fullscreen", True)
root.configure(bg="#111")

canvas = tk.Canvas(root, bg="#111", highlightthickness=0, cursor="crosshair")
canvas.pack(fill="both", expand=True)

# Overlay images
WHITE_IMG = ImageTk.PhotoImage(Image.new("RGB", (WIN_W, WIN_H), (255,255,255)))
BLACK_IMG = ImageTk.PhotoImage(Image.new("RGB", (WIN_W, WIN_H), (0,0,0)))
overlay_item = canvas.create_image(0, 0, anchor="nw", image=BLACK_IMG, state="hidden")

# Camera panels
cam0_item = canvas.create_image(0,       TOP_H, anchor="nw")
cam1_item = canvas.create_image(PANEL_W, TOP_H, anchor="nw")
cam0_photo = [None]; cam1_photo = [None]

# Header
header_bg = canvas.create_rectangle(0, 0, WIN_W, TOP_H, fill="#1a1a2e", outline="")

# Pipeline diagram text
pipeline_txt = (
  "ESP32 LED  ──→  cam0 detects LED  ──→  overlay wit/zwart op scherm  ──→  cam1 ziet scherm  ──→  LATENCY GEMETEN"
)
canvas.create_text(WIN_W//2, TOP_H//2 - 20, text=pipeline_txt,
                   fill="#4fc3f7", font=("monospace", 13), anchor="center")

# Divider
canvas.create_line(PANEL_W, TOP_H, PANEL_W, WIN_H, fill="#444", width=2)

# Cam labels
bold16 = ("monospace", 16, "bold")
bold12 = ("monospace", 12)

canvas.create_rectangle(0, TOP_H, PANEL_W, TOP_H+36, fill="#0d3b2e", outline="")
canvas.create_text(PANEL_W//2, TOP_H+18,
    text="CAM0  —  ziet LED  →  stuurt overlay",
    fill="#00e676", font=bold16, anchor="center")

canvas.create_rectangle(PANEL_W, TOP_H, WIN_W, TOP_H+36, fill="#2d1b4e", outline="")
canvas.create_text(PANEL_W + PANEL_W//2, TOP_H+18,
    text="CAM1  —  ziet LED + SCHERM  →  MEET latency",
    fill="#ce93d8", font=bold16, anchor="center")

# Status bar
status_bg = canvas.create_rectangle(0, WIN_H-32, WIN_W, WIN_H, fill="#000", outline="")
status_item = canvas.create_text(WIN_W//2, WIN_H-16, text=status_msg[0],
                                  fill="yellow", font=("monospace", 13), anchor="center")
keys_item = canvas.create_text(WIN_W-10, WIN_H-16,
    text="R=Gemini  T=threshold  SPACE=overlay  ESC=quit",
    fill="#555", font=("monospace", 12), anchor="e")

# Overlay status
overlay_status = canvas.create_text(10, 10, anchor="nw", state="hidden",
    fill="gray", font=("monospace", 14), text="")

# ── Render ────────────────────────────────────────────────────────────────────
CAM_LABEL_H = 36

def update():
    if mode[0] == "overlay":
        canvas.itemconfig(overlay_item, state="normal",
                          image=WHITE_IMG if cam0_on[0] else BLACK_IMG)
        canvas.itemconfig(cam0_item, state="hidden")
        canvas.itemconfig(cam1_item, state="hidden")
        canvas.itemconfig(header_bg, state="hidden")
        canvas.itemconfig(overlay_status, state="normal",
                          text=f"OVERLAY  fps={cam0_fps[0]:.0f}  led={'ON' if cam0_on[0] else 'OFF'}  [C=calibrate  ESC=quit]")
        canvas.itemconfig(status_item, state="hidden")
        canvas.itemconfig(keys_item,   state="hidden")
    else:
        canvas.itemconfig(overlay_item, state="hidden")
        canvas.itemconfig(overlay_status, state="hidden")
        canvas.itemconfig(cam0_item,   state="normal")
        canvas.itemconfig(cam1_item,   state="normal")
        canvas.itemconfig(header_bg,   state="normal")
        canvas.itemconfig(status_item, state="normal", text=status_msg[0])
        canvas.itemconfig(keys_item,   state="normal")

        # Draw cam0
        with cam0_lock:
            f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        result0 = draw_cam_panel(f0, led0, cam0_bright[0], cam0_on[0], cam0_fps[0],
                                  "CAM0", "stuurt overlay", (0,230,118))
        if isinstance(result0, tuple):
            img0, ox0, oy0, ratio0 = result0
            scale0["ox"], scale0["oy"], scale0["r"] = ox0, oy0, ratio0
        else:
            img0 = result0
        p0 = ImageTk.PhotoImage(img0)
        cam0_photo[0] = p0
        canvas.coords(cam0_item, 0, TOP_H + CAM_LABEL_H)
        canvas.itemconfig(cam0_item, image=p0)

        # Draw cam1
        with cam1_lock:
            f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None
        result1 = draw_cam_panel(f1, led1, cam1_bright[0], cam1_on[0], cam1_fps[0],
                                  "CAM1", "meet latency", (206,147,216))
        if isinstance(result1, tuple):
            img1, ox1, oy1, ratio1 = result1
            scale1["ox"], scale1["oy"], scale1["r"] = ox1, oy1, ratio1
        else:
            img1 = result1
        p1 = ImageTk.PhotoImage(img1)
        cam1_photo[0] = p1
        canvas.coords(cam1_item, PANEL_W, TOP_H + CAM_LABEL_H)
        canvas.itemconfig(cam1_item, image=p1)

    root.after(33, update)  # ~30fps UI refresh

scale0 = {"ox": 0, "oy": 0, "r": 1.0}
scale1 = {"ox": 0, "oy": 0, "r": 1.0}

# ── Mouse: click to move LED ──────────────────────────────────────────────────
def on_click(event):
    if mode[0] != "calibrate": return
    x, y = event.x, event.y
    panel_y = TOP_H + CAM_LABEL_H
    if y < panel_y: return
    py = y - panel_y
    if x < PANEL_W:  # cam0
        led = led0; sc = scale0
        px = x
    else:  # cam1
        led = led1; sc = scale1
        px = x - PANEL_W
    # Convert display coords to original frame coords
    frame_x = int((px - sc["ox"]) / sc["r"])
    frame_y = int((py - sc["oy"]) / sc["r"])
    led["cx"], led["cy"] = max(0, frame_x), max(0, frame_y)
    status_msg[0] = f"LED moved to ({led['cx']},{led['cy']})"

def on_scroll(event):
    if mode[0] != "calibrate": return
    delta = 1 if (event.delta > 0 or event.num == 4) else -1
    x = event.x
    led = led0 if x < PANEL_W else led1
    led["r"] = max(2, min(60, led["r"] + delta))

canvas.bind("<Button-1>", on_click)
canvas.bind("<MouseWheel>", on_scroll)
canvas.bind("<Button-4>", on_scroll)
canvas.bind("<Button-5>", on_scroll)

# ── Keys ──────────────────────────────────────────────────────────────────────
def on_key(event):
    k = event.keysym.lower()
    if   k == "escape": root.destroy()
    elif k == "space":  mode[0] = "overlay"
    elif k == "c":      mode[0] = "calibrate"
    elif k == "r":      threading.Thread(target=run_gemini, daemon=True).start()
    elif k == "t":      run_autothreshold()

root.bind("<Key>", on_key)

# Auto-detect on start after 2s
root.after(2000, lambda: threading.Thread(target=run_gemini, daemon=True).start())
root.after(33, update)

print("Camera Latency Meter started")
print(f"  cam0 TCP: {args.jetson}:{args.cam0_port}")
print(f"  cam1 URL: {args.cam1_url}")
print("Keys: SPACE=overlay  C=calibrate  R=Gemini  T=threshold  click=move LED  ESC=quit")
root.mainloop()
