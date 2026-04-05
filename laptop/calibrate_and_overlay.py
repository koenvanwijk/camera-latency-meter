#!/usr/bin/env python3
"""
Camera Latency Meter — Calibration + Overlay

cam1 heeft twee ROIs:
  • LED1   (paars)  — directe LED zichtbaar op cam1
  • SCR1   (oranje) — het scherm zichtbaar op cam1 (cam0 overlay)

De latency = tijdsverschil LED1-ON → SCR1-ON

Keys:
  SPACE       → overlay mode (wit/zwart fullscreen)
  C           → terug naar calibratie
  R           → Gemini LED auto-detect (beide cameras)
  T           → auto-threshold (3s meten, actieve ROI)
  Tab         → wissel actieve cam1 ROI (LED1 ↔ SCR1)
  Linksklik   → verplaats actieve ROI
  Scroll      → vergroot/verklein ROI radius
  ↑ / ↓       → threshold ±1  (actieve ROI)
  Shift+↑↓    → threshold ±10
  ESC         → afsluiten
"""

import argparse, socket, time, threading, base64, json, collections
import urllib.request, os
import numpy as np
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw
import cv2

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",    default="192.168.86.47")
parser.add_argument("--cam0-port", type=int, default=5001)
parser.add_argument("--cam1-url",  default="http://192.168.86.47:8091/stream")
parser.add_argument("--win-x",     type=int, default=0)
parser.add_argument("--win-y",     type=int, default=0)
args = parser.parse_args()

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyC2PBKX4PqU3nr7GRBdkUCkymll2ulesJ8")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-flash-lite-latest:generateContent?key=" + GEMINI_KEY)

WIN_W, WIN_H  = 1920, 1080
PANEL_W       = WIN_W // 2
HEADER_H      = 60
LABEL_H       = 30
CAM_H         = 460
SIGNAL_H      = 50   # groen/rood vlak onder cameras, boven grafiek
GRAPH_H       = 220
STATUS_H      = 30
CAM_Y         = HEADER_H + LABEL_H
SIGNAL_Y      = CAM_Y + CAM_H
GRAPH_Y       = SIGNAL_Y + SIGNAL_H
STATUS_Y      = GRAPH_Y + GRAPH_H
GRAPH_HISTORY = 300

# ── ROI definitions ───────────────────────────────────────────────────────────
# cam0: one LED ROI
led0 = {"cx": 460, "cy": 164, "r": 12, "thr": 35.0, "label": "LED0", "col": (0, 230, 80)}

# cam1: two ROIs
led1 = {"cx": 869, "cy": 349, "r": 12, "thr": 30.0, "label": "LED1", "col": (180, 100, 255)}
scr1 = {"cx": 400, "cy": 200, "r": 20, "thr": 128.0, "label": "SCR1", "col": (255, 160, 40)}

# Which cam1 ROI is "active" for clicks/threshold keys
cam1_active_roi = ["led"]   # "led" | "scr"

# ── Shared state ──────────────────────────────────────────────────────────────
mode = ["calibrate"]

cam0_frame = [None]; cam0_lock = threading.Lock()
cam1_frame = [None]; cam1_lock = threading.Lock()

cam0_bright = [0.0]; cam0_on = [False]; cam0_fps = [0.0]
cam1_led_bright = [0.0]; cam1_led_on = [False]
cam1_scr_bright = [0.0]; cam1_scr_on = [False]; cam1_fps = [0.0]

hist0     = collections.deque(maxlen=GRAPH_HISTORY)
hist1_led = collections.deque(maxlen=GRAPH_HISTORY)
hist1_scr = collections.deque(maxlen=GRAPH_HISTORY)

status_msg    = ["Auto-detecting LEDs with Gemini..."]
gemini_running = [False]

scale0 = {"ox": 0, "oy": 0, "r": 1.0}
scale1 = {"ox": 0, "oy": 0, "r": 1.0}

# ── Stream loops ──────────────────────────────────────────────────────────────
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
                    hist0.append((time.time(), v))
                    with cam0_lock: cam0_frame[0] = arr
                    fc += 1
                    if fc % 30 == 0:
                        cam0_fps[0] = 30 / max(0.01, time.time()-t0)
                        t0 = time.time(); fc = 0
            sock.close()
        except: time.sleep(0.3)

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
                    now = time.time()

                    cx, cy, r = led1["cx"], led1["cy"], led1["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    vl = float(roi.mean()) if roi.size > 0 else 0.0
                    cam1_led_bright[0] = vl
                    cam1_led_on[0] = vl > led1["thr"]
                    hist1_led.append((now, vl))

                    cx, cy, r = scr1["cx"], scr1["cy"], scr1["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    vs = float(roi.mean()) if roi.size > 0 else 0.0
                    cam1_scr_bright[0] = vs
                    cam1_scr_on[0] = vs > scr1["thr"]
                    hist1_scr.append((now, vs))

                    with cam1_lock: cam1_frame[0] = arr
                    fc += 1
                    if fc % 30 == 0:
                        cam1_fps[0] = 30 / max(0.01, time.time()-t0)
                        t0 = time.time(); fc = 0
            req.close()
        except: time.sleep(0.3)

threading.Thread(target=cam0_loop, daemon=True).start()
threading.Thread(target=cam1_loop, daemon=True).start()

# ── Gemini ────────────────────────────────────────────────────────────────────
def gemini_detect(img_bgr, prompt_extra=""):
    h, w = img_bgr.shape[:2]
    jpg = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    b64 = base64.b64encode(jpg).decode()
    body = json.dumps({"contents": [{"parts": [
        {"text": (f"Camera {w}x{h}px robotics latency setup. "
                  f"Find the blinking LED{prompt_extra}. Return ONLY JSON: "
                  "{\"cx\": <0-1000>, \"cy\": <0-1000>} normalized coords. No markdown.")},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip().strip("`").strip()
    if text.lower().startswith("json"): text = text[4:].strip()
    r = json.loads(text)
    return int(int(r["cx"]) / 1000 * w), int(int(r["cy"]) / 1000 * h)

def run_gemini():
    if gemini_running[0]: return
    gemini_running[0] = True
    def _run():
        status_msg[0] = "Gemini: detecting LEDs..."
        with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None
        errs = []
        if f0 is not None:
            try: led0["cx"], led0["cy"] = gemini_detect(f0)
            except Exception as ex: errs.append(f"cam0:{ex}")
        if f1 is not None:
            try: led1["cx"], led1["cy"] = gemini_detect(f1)
            except Exception as ex: errs.append(f"cam1:{ex}")
        if errs:
            status_msg[0] = "Gemini errors: " + " | ".join(str(e) for e in errs)
        else:
            status_msg[0] = (f"Gemini ✓  cam0=({led0['cx']},{led0['cy']})  "
                             f"LED1=({led1['cx']},{led1['cy']})")
        gemini_running[0] = False
    threading.Thread(target=_run, daemon=True).start()

def run_autothreshold():
    active = led1 if cam1_active_roi[0] == "led" else scr1
    def _run():
        status_msg[0] = f"Auto-threshold {active['label']}: collecting 3s..."
        v0, va = [], []
        t = time.time()
        while time.time()-t < 3.5:
            v0.append(cam0_bright[0])
            va.append(cam1_led_bright[0] if cam1_active_roi[0] == "led" else cam1_scr_bright[0])
            time.sleep(0.02)
        for vals, cfg in [(v0, led0), (va, active)]:
            mn, mx = min(vals), max(vals)
            if mx-mn > 4: cfg["thr"] = (mn+mx)/2
        status_msg[0] = (f"Threshold ✓  cam0={led0['thr']:.0f}  "
                         f"{active['label']}={active['thr']:.0f}")
    threading.Thread(target=_run, daemon=True).start()

# ── Draw camera panel ──────────────────────────────────────────────────────────
def draw_cam0(frame_bgr):
    img = Image.new("RGB", (PANEL_W, CAM_H), (25, 25, 25))
    if frame_bgr is None:
        ImageDraw.Draw(img).text((20, CAM_H//2), "Waiting for stream...", fill=(100,100,100))
        scale0["ox"] = scale0["oy"] = 0; scale0["r"] = 1.0
        return img
    h, w = frame_bgr.shape[:2]
    ratio = min(PANEL_W/w, CAM_H/h)
    nw, nh = int(w*ratio), int(h*ratio)
    ox = (PANEL_W-nw)//2; oy = (CAM_H-nh)//2
    scale0["ox"] = ox; scale0["oy"] = oy; scale0["r"] = ratio
    img.paste(Image.fromarray(cv2.cvtColor(cv2.resize(frame_bgr,(nw,nh)), cv2.COLOR_BGR2RGB)), (ox,oy))
    draw = ImageDraw.Draw(img)
    _draw_roi(draw, led0, cam0_bright[0], cam0_on[0], ratio, ox, oy)
    return img

def draw_cam1(frame_bgr):
    img = Image.new("RGB", (PANEL_W, CAM_H), (25, 25, 25))
    if frame_bgr is None:
        ImageDraw.Draw(img).text((20, CAM_H//2), "Waiting for stream...", fill=(100,100,100))
        scale1["ox"] = scale1["oy"] = 0; scale1["r"] = 1.0
        return img
    h, w = frame_bgr.shape[:2]
    ratio = min(PANEL_W/w, CAM_H/h)
    nw, nh = int(w*ratio), int(h*ratio)
    ox = (PANEL_W-nw)//2; oy = (CAM_H-nh)//2
    scale1["ox"] = ox; scale1["oy"] = oy; scale1["r"] = ratio
    img.paste(Image.fromarray(cv2.cvtColor(cv2.resize(frame_bgr,(nw,nh)), cv2.COLOR_BGR2RGB)), (ox,oy))
    draw = ImageDraw.Draw(img)
    # Draw inactive ROI first (dimmer)
    if cam1_active_roi[0] == "led":
        _draw_roi(draw, scr1, cam1_scr_bright[0], cam1_scr_on[0], ratio, ox, oy, alpha=0.4, active=False)
        _draw_roi(draw, led1, cam1_led_bright[0], cam1_led_on[0], ratio, ox, oy, active=True)
    else:
        _draw_roi(draw, led1, cam1_led_bright[0], cam1_led_on[0], ratio, ox, oy, alpha=0.4, active=False)
        _draw_roi(draw, scr1, cam1_scr_bright[0], cam1_scr_on[0], ratio, ox, oy, active=True)
    # Active ROI label
    active = led1 if cam1_active_roi[0] == "led" else scr1
    draw.text((8, 8), f"[Tab] actief: {active['label']}", fill=active["col"])
    return img

def _draw_roi(draw, cfg, bright, on, ratio, ox, oy, alpha=1.0, active=True):
    cx_d = int(cfg["cx"]*ratio) + ox
    cy_d = int(cfg["cy"]*ratio) + oy
    r_d  = max(5, int(cfg["r"]*ratio))
    base = cfg["col"]
    col = tuple(int(c * (0.5 if not active else 1.0)) for c in base)
    lw = 3 if active else 1
    draw.ellipse([cx_d-r_d, cy_d-r_d, cx_d+r_d, cy_d+r_d], outline=col, width=lw)
    if active:
        draw.line([(cx_d-r_d-10, cy_d), (cx_d+r_d+10, cy_d)], fill=col, width=1)
        draw.line([(cx_d, cy_d-r_d-10), (cx_d, cy_d+r_d+10)], fill=col, width=1)
    # Label + brightness
    label_col = col if on else tuple(int(c*0.6) for c in col)
    state = "ON" if on else "off"
    draw.text((cx_d+r_d+5, cy_d-18), cfg["label"], fill=col)
    draw.text((cx_d+r_d+5, cy_d-4),  f"{bright:.1f} {state}", fill=label_col)

# ── Draw brightness graph ──────────────────────────────────────────────────────
def draw_triple_graph(h0, h_led, h_scr):
    """Full-width 3-track timeline. Shared time axis. W x GRAPH_H."""
    W = WIN_W
    img = Image.new("RGB", (W, GRAPH_H), (12, 12, 18))
    draw = ImageDraw.Draw(img)

    PAD_L, PAD_R, PAD_T, PAD_B = 64, 12, 6, 20
    gw  = W - PAD_L - PAD_R
    gh  = GRAPH_H - PAD_T - PAD_B
    TH  = gh // 3          # height per track
    TRACKS = [
        (h0,     led0, cam0_bright[0],     cam0_on[0],     "CAM0 LED0",  (0,220,80)),
        (h_led,  led1, cam1_led_bright[0], cam1_led_on[0], "CAM1 LED1",  (170,90,255)),
        (h_scr,  scr1, cam1_scr_bright[0], cam1_scr_on[0], "CAM1 SCR1",  (255,155,30)),
    ]

    # Shared time window
    all_pts = list(h0) + list(h_led) + list(h_scr)
    if not all_pts:
        draw.text((PAD_L+10, PAD_T+10), "Collecting data...", fill=(80,80,80))
        return img
    t_end  = max(t for t,_ in all_pts)
    t_span = max(5.0, t_end - min(t for t,_ in all_pts))

    def xx(t): return PAD_L + int(((t-(t_end-t_span))/t_span)*gw)

    # Vertical grid lines (time)
    for i in range(6):
        x = PAD_L + int(i * gw / 5)
        draw.line([(x, PAD_T), (x, PAD_T+gh)], fill=(35,35,45), width=1)
        t_label = t_span * (1 - i/5)
        draw.text((x-12, PAD_T+gh+3), f"-{t_label:.1f}s", fill=(50,50,55))

    # Draw each track
    for ti, (hist, cfg, bright_now, on_now, label, col) in enumerate(TRACKS):
        track_top = PAD_T + ti * TH
        track_bot = track_top + TH - 2
        tmid = (track_top + track_bot) // 2

        # Track bg + separator
        draw.rectangle([PAD_L, track_top, W-PAD_R, track_bot], fill=(18,18,25))
        draw.line([(0, track_top), (W, track_top)], fill=(40,40,50), width=1)

        # Label left
        col_dim = tuple(int(c*0.7) for c in col)
        draw.text((2, tmid-8), label, fill=col_dim)

        pts = list(hist)
        if len(pts) < 2:
            draw.text((PAD_L+10, tmid-6), "collecting...", fill=(60,60,60))
            continue

        vals   = [v for _,v in pts]
        thr    = cfg["thr"]
        vmin   = max(0,   min(vals+[thr]) - 5)
        vmax   = min(255, max(vals+[thr]) + 5)
        vrange = max(vmax-vmin, 8)

        def yx(v, tt=track_top, tb=track_bot):
            return tt + int((1-(v-vmin)/vrange)*(tb-tt))

        # Threshold line
        thr_y = yx(thr)
        draw.line([(PAD_L, thr_y), (W-PAD_R, thr_y)],
                  fill=(255,200,0), width=1)
        draw.text((PAD_L-62, thr_y-7), f"thr={thr:.0f}", fill=(200,160,0))

        # ON region tint
        draw.rectangle([PAD_L, track_top, W-PAD_R, thr_y],   fill=(0,25,10))
        draw.rectangle([PAD_L, thr_y,     W-PAD_R, track_bot], fill=(25,8,8))
        # Re-draw threshold on top of tint
        draw.line([(PAD_L, thr_y), (W-PAD_R, thr_y)], fill=(200,160,0), width=1)

        # Signal
        line_pts = []
        for t, v in pts:
            x = xx(t)
            if PAD_L <= x <= W-PAD_R:
                line_pts.append((x, yx(v)))
        if len(line_pts) >= 2:
            col_on  = col
            col_off = tuple(int(c*0.45) for c in col)
            for i in range(len(line_pts)-1):
                c = col_on if vals[i] > thr else col_off
                draw.line([line_pts[i], line_pts[i+1]], fill=c, width=2)

        # Current value dot + label
        if line_pts:
            lx, ly = line_pts[-1]
            dc = col if on_now else tuple(int(c*0.45) for c in col)
            draw.ellipse([lx-4,ly-4,lx+4,ly+4], fill=dc)
            draw.text((lx+6, ly-8), f"{bright_now:.1f}", fill=dc)

        # ON/OFF state badge
        state_col = col if on_now else (100,100,100)
        draw.text((W-PAD_R-55, tmid-7),
                  "● ON " if on_now else "○ off",
                  fill=state_col)

    draw.text((PAD_L, GRAPH_H-PAD_B+2), "← ouder", fill=(50,50,55))
    draw.text((W-PAD_R-40, GRAPH_H-PAD_B+2), "nu →", fill=(50,50,55))
    return img

# ── Tkinter ───────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Camera Latency Meter")
root.geometry(f"{WIN_W}x{WIN_H}+{args.win_x}+{args.win_y}")
root.attributes("-fullscreen", True)
root.configure(bg="#111")

canvas = tk.Canvas(root, bg="#111", highlightthickness=0, cursor="crosshair")
canvas.pack(fill="both", expand=True)

def make_cross_img(color, bg=(0,0,0)):
    img = Image.new("RGB", (WIN_W, WIN_H), bg)
    draw = ImageDraw.Draw(img)
    cx, cy = WIN_W//2, WIN_H//2
    arm = 350; thick = 80
    draw.rectangle([cx-thick//2, cy-arm, cx+thick//2, cy+arm], fill=color)
    draw.rectangle([cx-arm, cy-thick//2, cx+arm, cy+thick//2], fill=color)
    return img

GREEN_FULL = ImageTk.PhotoImage(make_cross_img((0, 220, 60)))
RED_FULL   = ImageTk.PhotoImage(make_cross_img((220, 30, 30)))
# Fullscreen overlay (SPACE mode)
overlay_item = canvas.create_image(0, 0, anchor="nw", image=RED_FULL, state="hidden")

# Signal bar (always visible in calibrate mode, under cameras)
signal_bar = canvas.create_rectangle(0, SIGNAL_Y, WIN_W, SIGNAL_Y+SIGNAL_H,
                                      fill="#330000", outline="")
signal_txt = canvas.create_text(WIN_W//2, SIGNAL_Y+SIGNAL_H//2,
    text="LED OFF", fill="white", font=("monospace", 20, "bold"), anchor="center")

# Header
canvas.create_rectangle(0, 0, WIN_W, HEADER_H, fill="#0d1b2a", outline="")
canvas.create_text(WIN_W//2, HEADER_H//2,
    text="ESP32 LED  →  cam0 detecteert LED  →  overlay wit/zwart op scherm  →  cam1 ziet LED + SCHERM  →  LATENCY",
    fill="#4fc3f7", font=("monospace", 13, "bold"), anchor="center")

canvas.create_line(PANEL_W, HEADER_H, PANEL_W, WIN_H-STATUS_H, fill="#333", width=2)

canvas.create_rectangle(0,       HEADER_H, PANEL_W, HEADER_H+LABEL_H, fill="#0d3b2e", outline="")
canvas.create_text(PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM0  —  detecteert LED  →  stuurt overlay naar scherm",
    fill="#00e676", font=("monospace", 13, "bold"), anchor="center")

canvas.create_rectangle(PANEL_W, HEADER_H, WIN_W, HEADER_H+LABEL_H, fill="#2d1b4e", outline="")
canvas.create_text(PANEL_W+PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM1  —  ziet LED (paars) + SCHERM (oranje)  →  MEET latency",
    fill="#ce93d8", font=("monospace", 13, "bold"), anchor="center")

cam0_item   = canvas.create_image(0,       CAM_Y, anchor="nw")
cam1_item   = canvas.create_image(PANEL_W, CAM_Y, anchor="nw")
graph_item  = canvas.create_image(0, GRAPH_Y, anchor="nw")

canvas.create_line(0, GRAPH_Y, WIN_W, GRAPH_Y, fill="#333", width=1)

canvas.create_rectangle(0, STATUS_Y, WIN_W, WIN_H, fill="#0a0a0a", outline="")
status_item = canvas.create_text(WIN_W//2, STATUS_Y+STATUS_H//2,
    text="", fill="yellow", font=("monospace", 13), anchor="center")
canvas.create_text(WIN_W-10, STATUS_Y+STATUS_H//2,
    text="R=Gemini  T=auto-thr  ↑↓=thr±1  Shift+↑↓=±10  Tab=wissel ROI  SPACE=overlay  C=calib  ESC",
    fill="#444", font=("monospace", 11), anchor="e")

overlay_status = canvas.create_text(10, 10, anchor="nw", state="hidden",
    fill="#888", font=("monospace", 14), text="")

cam0_ph=[None]; cam1_ph=[None]; graph_ph=[None]

# ── Render loop ───────────────────────────────────────────────────────────────
def update():
    if mode[0] == "overlay":
        canvas.itemconfig(overlay_item, state="normal",
                          image=GREEN_FULL if cam0_on[0] else RED_FULL)
        for it in [cam0_item, cam1_item, graph_item, status_item, signal_bar, signal_txt]:
            canvas.itemconfig(it, state="hidden")
        canvas.itemconfig(overlay_status, state="normal",
            text=f"OVERLAY  fps={cam0_fps[0]:.0f}  led={'ON' if cam0_on[0] else 'OFF'}  [C=calibrate  ESC=quit]")
    else:
        canvas.itemconfig(overlay_item, state="hidden")
        canvas.itemconfig(overlay_status, state="hidden")
        canvas.itemconfig(status_item, state="normal", text=status_msg[0])
        canvas.itemconfig(signal_bar, state="normal")
        canvas.itemconfig(signal_txt, state="normal")

        # Update signal bar colour
        if cam0_on[0]:
            canvas.itemconfig(signal_bar, fill="#ffffff")
            canvas.itemconfig(signal_txt, text="● LED ON", fill="#000000")
        else:
            canvas.itemconfig(signal_bar, fill="#000000")
            canvas.itemconfig(signal_txt, text="○ LED OFF", fill="#ffffff")

        with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None

        p0 = ImageTk.PhotoImage(draw_cam0(f0)); cam0_ph[0]=p0
        canvas.itemconfig(cam0_item, image=p0, state="normal")

        p1 = ImageTk.PhotoImage(draw_cam1(f1)); cam1_ph[0]=p1
        canvas.itemconfig(cam1_item, image=p1, state="normal")

        gp = ImageTk.PhotoImage(draw_triple_graph(list(hist0), list(hist1_led), list(hist1_scr)))
        graph_ph[0] = gp
        canvas.itemconfig(graph_item, image=gp, state="normal")

    root.after(50, update)

# ── Mouse ─────────────────────────────────────────────────────────────────────
def on_click(event):
    if mode[0] != "calibrate": return
    x, y = event.x, event.y
    if y < CAM_Y or y > CAM_Y+CAM_H: return
    py = y - CAM_Y
    if x < PANEL_W:
        cfg, sc, px = led0, scale0, x
    else:
        cfg = led1 if cam1_active_roi[0]=="led" else scr1
        sc, px = scale1, x-PANEL_W
    cfg["cx"] = max(0, int((px-sc["ox"])/sc["r"]))
    cfg["cy"] = max(0, int((py-sc["oy"])/sc["r"]))
    status_msg[0] = f"{cfg['label']} moved to ({cfg['cx']},{cfg['cy']})"

def on_scroll(event):
    if mode[0] != "calibrate": return
    delta = 1 if (event.delta > 0 or event.num == 4) else -1
    if event.x < PANEL_W:
        led0["r"] = max(2, min(60, led0["r"]+delta))
    else:
        cfg = led1 if cam1_active_roi[0]=="led" else scr1
        cfg["r"] = max(2, min(80, cfg["r"]+delta))

canvas.bind("<Button-1>", on_click)
canvas.bind("<MouseWheel>", on_scroll)
canvas.bind("<Button-4>", on_scroll)
canvas.bind("<Button-5>", on_scroll)

# ── Keys ──────────────────────────────────────────────────────────────────────
def active_cam1_cfg():
    return led1 if cam1_active_roi[0]=="led" else scr1

def on_key(event):
    k = event.keysym.lower()
    if   k == "escape": root.destroy()
    elif k == "space":  mode[0] = "overlay"
    elif k == "c":      mode[0] = "calibrate"
    elif k == "r":      threading.Thread(target=run_gemini, daemon=True).start()
    elif k == "t":      run_autothreshold()
    elif k == "tab":
        cam1_active_roi[0] = "scr" if cam1_active_roi[0]=="led" else "led"
        active = active_cam1_cfg()
        status_msg[0] = f"Actieve cam1 ROI: {active['label']}"
    elif k in ("up", "down"):
        step = 10 if (event.state & 1) else 1
        delta = step if k=="up" else -step
        led0["thr"]   = max(0, min(255, led0["thr"]   + delta))
        active = active_cam1_cfg()
        active["thr"] = max(0, min(255, active["thr"] + delta))
        status_msg[0] = (f"Threshold {'↑' if delta>0 else '↓'}  "
                         f"cam0={led0['thr']:.0f}  {active['label']}={active['thr']:.0f}")

root.bind("<Key>", on_key)

root.after(2000, lambda: threading.Thread(target=run_gemini, daemon=True).start())
root.after(50, update)

print(f"Camera Latency Meter | cam0 TCP {args.jetson}:{args.cam0_port} | cam1 {args.cam1_url}")
print("Tab=wissel cam1 ROI (LED1/SCR1)  SPACE=overlay  R=Gemini  T=threshold  ESC=quit")
root.mainloop()
