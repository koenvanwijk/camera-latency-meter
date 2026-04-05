#!/usr/bin/env python3
"""
Camera Latency Meter — Calibration + Overlay

Layout:
  ┌────────────────────────────────────────────────────────────┐  ← 60px header
  │  ESP32 LED → cam0 detects LED → overlay → cam1 ziet scherm │
  ├─────────────────────────┬──────────────────────────────────┤  ← 30px label
  │  CAM0  stuurt overlay   │  CAM1  MEET latency              │
  │  [live camera 960x520]  │  [live camera 960x520]           │  ← 520px cam
  ├─────────────────────────┼──────────────────────────────────┤
  │  [brightness graph]     │  [brightness graph]              │  ← 150px graph
  ├─────────────────────────┴──────────────────────────────────┤  ← 30px status
  │  status  |  R=Gemini  T=threshold  SPACE=overlay  ESC      │
  └────────────────────────────────────────────────────────────┘

Keys:
  SPACE  → overlay mode (wit/zwart fullscreen)
  C      → terug naar calibratie
  R      → Gemini LED auto-detect (beide cameras)
  T      → auto-threshold (3s meten)
  Klik   → verplaats LED cirkel
  Scroll → ROI groter/kleiner
  ESC    → afsluiten
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

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "AIzaSyC2PBKX4PqU3nr7GRBdkUCkymll2ulesJ8")
GEMINI_URL   = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-flash-lite-latest:generateContent?key=" + GEMINI_KEY)

WIN_W, WIN_H  = 1920, 1080
PANEL_W       = WIN_W // 2   # 960
HEADER_H      = 60
LABEL_H       = 30
CAM_H         = 520
GRAPH_H       = 150
STATUS_H      = 30
# Sanity: HEADER_H + LABEL_H + CAM_H + GRAPH_H + STATUS_H = 790; rest is padding
CAM_Y         = HEADER_H + LABEL_H
GRAPH_Y       = CAM_Y + CAM_H
STATUS_Y      = GRAPH_Y + GRAPH_H

GRAPH_HISTORY = 300  # samples in graph

# ── Shared state ──────────────────────────────────────────────────────────────
mode = ["calibrate"]

cam0_frame = [None]; cam0_lock = threading.Lock()
cam1_frame = [None]; cam1_lock = threading.Lock()

led0 = {"cx": 460, "cy": 164, "r": 12, "thr": 35.0}
led1 = {"cx": 869, "cy": 349, "r": 12, "thr": 30.0}

cam0_bright = [0.0]; cam0_on = [False]; cam0_fps = [0.0]
cam1_bright = [0.0]; cam1_on = [False]; cam1_fps = [0.0]

hist0 = collections.deque(maxlen=GRAPH_HISTORY)  # (time, brightness)
hist1 = collections.deque(maxlen=GRAPH_HISTORY)

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
                    cx, cy, r = led1["cx"], led1["cy"], led1["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    v = float(roi.mean()) if roi.size > 0 else 0.0
                    cam1_bright[0] = v
                    cam1_on[0] = v > led1["thr"]
                    hist1.append((time.time(), v))
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
def gemini_detect(img_bgr, label):
    h, w = img_bgr.shape[:2]
    jpg = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
    b64 = base64.b64encode(jpg).decode()
    body = json.dumps({"contents": [{"parts": [
        {"text": (f"Camera {w}x{h}px robotics latency setup. "
                  "Find the blinking LED. Return ONLY JSON: "
                  "{\"cx\": <0-1000>, \"cy\": <0-1000>} normalized. No markdown.")},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
    ]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=body,
                                  headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip().strip("`").strip()
    if text.lower().startswith("json"): text = text[4:].strip()
    r = json.loads(text)
    cx = int(int(r["cx"]) / 1000 * w)
    cy = int(int(r["cy"]) / 1000 * h)
    print(f"Gemini {label}: ({cx},{cy})", flush=True)
    return cx, cy

def run_gemini():
    if gemini_running[0]: return
    gemini_running[0] = True
    def _run():
        status_msg[0] = "Gemini: detecting LEDs..."
        with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None
        errs = []
        if f0 is not None:
            try:
                led0["cx"], led0["cy"] = gemini_detect(f0, "cam0")
            except Exception as ex: errs.append(f"cam0:{ex}")
        if f1 is not None:
            try:
                led1["cx"], led1["cy"] = gemini_detect(f1, "cam1")
            except Exception as ex: errs.append(f"cam1:{ex}")
        if errs:
            status_msg[0] = "Gemini errors: " + " | ".join(str(e) for e in errs)
        else:
            status_msg[0] = (f"Gemini ✓  cam0=({led0['cx']},{led0['cy']})  "
                             f"cam1=({led1['cx']},{led1['cy']})")
        gemini_running[0] = False
    threading.Thread(target=_run, daemon=True).start()

def run_autothreshold():
    def _run():
        status_msg[0] = "Auto-threshold: collecting 3s..."
        v0, v1 = [], []
        t = time.time()
        while time.time()-t < 3.5:
            v0.append(cam0_bright[0]); v1.append(cam1_bright[0])
            time.sleep(0.02)
        for vals, led, name in [(v0, led0, "cam0"), (v1, led1, "cam1")]:
            mn, mx = min(vals), max(vals)
            if mx-mn > 4: led["thr"] = (mn+mx)/2
        status_msg[0] = f"Threshold ✓  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}"
    threading.Thread(target=_run, daemon=True).start()

# ── Draw camera panel ──────────────────────────────────────────────────────────
def draw_cam(frame_bgr, led_cfg, bright, on, scale):
    img = Image.new("RGB", (PANEL_W, CAM_H), (25, 25, 25))
    if frame_bgr is None:
        draw = ImageDraw.Draw(img)
        draw.text((PANEL_W//2 - 70, CAM_H//2), "Waiting for stream...", fill=(120,120,120))
        scale["ox"] = scale["oy"] = 0; scale["r"] = 1.0
        return img

    h, w = frame_bgr.shape[:2]
    ratio = min(PANEL_W/w, CAM_H/h)
    nw, nh = int(w*ratio), int(h*ratio)
    ox = (PANEL_W-nw)//2; oy = (CAM_H-nh)//2
    scale["ox"] = ox; scale["oy"] = oy; scale["r"] = ratio

    cam_img = Image.fromarray(cv2.cvtColor(
        cv2.resize(frame_bgr, (nw, nh)), cv2.COLOR_BGR2RGB))
    img.paste(cam_img, (ox, oy))
    draw = ImageDraw.Draw(img)

    # LED circle
    cx_d = int(led_cfg["cx"]*ratio) + ox
    cy_d = int(led_cfg["cy"]*ratio) + oy
    r_d  = max(5, int(led_cfg["r"]*ratio))
    col = (0,255,80) if on else (255,60,60)
    draw.ellipse([cx_d-r_d, cy_d-r_d, cx_d+r_d, cy_d+r_d], outline=col, width=3)
    draw.line([(cx_d-r_d-10, cy_d), (cx_d+r_d+10, cy_d)], fill=col, width=1)
    draw.line([(cx_d, cy_d-r_d-10), (cx_d, cy_d+r_d+10)], fill=col, width=1)
    # Brightness label
    draw.text((cx_d+r_d+5, cy_d-9), f"{bright:.1f}", fill=col)

    return img

# ── Draw brightness graph ──────────────────────────────────────────────────────
def draw_graph(history, led_cfg, on, label_color):
    """Returns PIL image PANEL_W x GRAPH_H with time-series + threshold lines."""
    img = Image.new("RGB", (PANEL_W, GRAPH_H), (15, 15, 20))
    draw = ImageDraw.Draw(img)

    PAD_L, PAD_R, PAD_T, PAD_B = 50, 10, 10, 25
    gw = PANEL_W - PAD_L - PAD_R
    gh = GRAPH_H - PAD_T - PAD_B

    # Background grid
    for i in range(5):
        y = PAD_T + int(i * gh / 4)
        draw.line([(PAD_L, y), (PANEL_W-PAD_R, y)], fill=(40,40,50), width=1)

    if len(history) < 2:
        draw.text((PAD_L+5, PAD_T+5), "Collecting data...", fill=(80,80,80))
        return img

    pts = list(history)
    vals = [v for _, v in pts]
    t_end = pts[-1][0]
    t_span = max(5.0, pts[-1][0] - pts[0][0])

    # Y range: show range around threshold with some margin
    thr = led_cfg["thr"]
    all_vals = vals + [thr]
    vmin = max(0, min(all_vals) - 10)
    vmax = min(255, max(all_vals) + 10)
    vrange = max(vmax - vmin, 10)

    def yx(v):
        return PAD_T + int((1 - (v - vmin) / vrange) * gh)
    def xx(t):
        return PAD_L + int(((t - (t_end - t_span)) / t_span) * gw)

    # Threshold band (filled area between ON zone and threshold)
    thr_y = yx(thr)
    # Subtle fill for ON region
    draw.rectangle([PAD_L, PAD_T, PANEL_W-PAD_R, thr_y], fill=(0, 40, 20))
    draw.rectangle([PAD_L, thr_y, PANEL_W-PAD_R, PAD_T+gh], fill=(40, 10, 10))

    # Threshold line
    draw.line([(PAD_L, thr_y), (PANEL_W-PAD_R, thr_y)], fill=(255, 200, 0), width=2)
    draw.text((PAD_L - 44, thr_y - 8), f"thr={thr:.0f}", fill=(255, 200, 0))

    # Upper / lower labels
    draw.text((PAD_L - 38, PAD_T + 2),  f"{vmax:.0f}", fill=(80, 80, 80))
    draw.text((PAD_L - 38, PAD_T+gh-12), f"{vmin:.0f}", fill=(80, 80, 80))

    # Signal line
    line_pts = []
    for t, v in pts:
        x = xx(t); y = yx(v)
        if PAD_L <= x <= PANEL_W-PAD_R:
            line_pts.append((x, y))
    if len(line_pts) >= 2:
        # Color segments ON/OFF
        for i in range(len(line_pts)-1):
            col = (0, 230, 80) if vals[i] > thr else (255, 80, 80)
            draw.line([line_pts[i], line_pts[i+1]], fill=col, width=2)

    # Current value dot
    if line_pts:
        lx, ly = line_pts[-1]
        dot_col = (0,255,80) if on else (255,60,60)
        draw.ellipse([lx-4, ly-4, lx+4, ly+4], fill=dot_col)
        draw.text((lx+6, ly-8), f"{vals[-1]:.1f}", fill=dot_col)

    # X axis label
    draw.text((PAD_L, GRAPH_H-PAD_B+3), "← 5s", fill=(60,60,60))
    draw.text((PANEL_W-PAD_R-25, GRAPH_H-PAD_B+3), "now", fill=(60,60,60))

    return img

# ── Tkinter ───────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Camera Latency Meter")
root.geometry(f"{WIN_W}x{WIN_H}+{args.win_x}+{args.win_y}")
root.attributes("-fullscreen", True)
root.configure(bg="#111")

canvas = tk.Canvas(root, bg="#111", highlightthickness=0, cursor="crosshair")
canvas.pack(fill="both", expand=True)

WHITE_IMG = ImageTk.PhotoImage(Image.new("RGB", (WIN_W, WIN_H), (255,255,255)))
BLACK_IMG = ImageTk.PhotoImage(Image.new("RGB", (WIN_W, WIN_H), (0,0,0)))
overlay_item = canvas.create_image(0, 0, anchor="nw", image=BLACK_IMG, state="hidden")

# Header
canvas.create_rectangle(0, 0, WIN_W, HEADER_H, fill="#0d1b2a", outline="")
pipeline_txt = "ESP32 LED  →  cam0 detecteert LED  →  overlay wit/zwart op scherm  →  cam1 ziet scherm  →  LATENCY"
canvas.create_text(WIN_W//2, HEADER_H//2, text=pipeline_txt,
                   fill="#4fc3f7", font=("monospace", 13, "bold"), anchor="center")

# Divider
canvas.create_line(PANEL_W, HEADER_H, PANEL_W, WIN_H-STATUS_H, fill="#333", width=2)

# Cam labels
canvas.create_rectangle(0,       HEADER_H, PANEL_W, HEADER_H+LABEL_H, fill="#0d3b2e", outline="")
canvas.create_text(PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM0  —  detecteert LED  →  stuurt overlay naar scherm",
    fill="#00e676", font=("monospace", 13, "bold"), anchor="center")

canvas.create_rectangle(PANEL_W, HEADER_H, WIN_W, HEADER_H+LABEL_H, fill="#2d1b4e", outline="")
canvas.create_text(PANEL_W+PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM1  —  ziet LED + SCHERM  →  MEET volledige latency",
    fill="#ce93d8", font=("monospace", 13, "bold"), anchor="center")

# Camera image slots
cam0_item  = canvas.create_image(0,       CAM_Y, anchor="nw")
cam1_item  = canvas.create_image(PANEL_W, CAM_Y, anchor="nw")

# Graph slots
graph0_item = canvas.create_image(0,       GRAPH_Y, anchor="nw")
graph1_item = canvas.create_image(PANEL_W, GRAPH_Y, anchor="nw")

# Graph label line
canvas.create_line(0, GRAPH_Y, WIN_W, GRAPH_Y, fill="#333", width=1)

# Status bar
canvas.create_rectangle(0, STATUS_Y, WIN_W, WIN_H, fill="#0a0a0a", outline="")
status_item = canvas.create_text(WIN_W//2, STATUS_Y + STATUS_H//2,
    text="", fill="yellow", font=("monospace", 13), anchor="center")
canvas.create_text(WIN_W-10, STATUS_Y + STATUS_H//2,
    text="R=Gemini  T=auto-thr  ↑↓=threshold±1  Shift+↑↓=±10  SPACE=overlay  C=calib  ESC=quit",
    fill="#444", font=("monospace", 12), anchor="e")

# Overlay status
overlay_status = canvas.create_text(10, 10, anchor="nw", state="hidden",
    fill="#888", font=("monospace", 14), text="")

cam0_photo = [None]; cam1_photo = [None]
graph0_photo = [None]; graph1_photo = [None]

# ── Render loop ───────────────────────────────────────────────────────────────
def update():
    if mode[0] == "overlay":
        canvas.itemconfig(overlay_item, state="normal",
                          image=WHITE_IMG if cam0_on[0] else BLACK_IMG)
        for item in [cam0_item, cam1_item, graph0_item, graph1_item, status_item]:
            canvas.itemconfig(item, state="hidden")
        canvas.itemconfig(overlay_status, state="normal",
                          text=f"OVERLAY  fps={cam0_fps[0]:.0f}  led={'ON' if cam0_on[0] else 'OFF'}  [C=calibrate  ESC=quit]")
    else:
        canvas.itemconfig(overlay_item,   state="hidden")
        canvas.itemconfig(overlay_status, state="hidden")
        canvas.itemconfig(status_item,    state="normal", text=status_msg[0])

        with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
        with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None

        # Camera panels
        img0 = draw_cam(f0, led0, cam0_bright[0], cam0_on[0], scale0)
        p0 = ImageTk.PhotoImage(img0)
        cam0_photo[0] = p0
        canvas.itemconfig(cam0_item, image=p0, state="normal")

        img1 = draw_cam(f1, led1, cam1_bright[0], cam1_on[0], scale1)
        p1 = ImageTk.PhotoImage(img1)
        cam1_photo[0] = p1
        canvas.itemconfig(cam1_item, image=p1, state="normal")

        # Graphs
        g0 = draw_graph(list(hist0), led0, cam0_on[0], (0,230,80))
        gp0 = ImageTk.PhotoImage(g0)
        graph0_photo[0] = gp0
        canvas.itemconfig(graph0_item, image=gp0, state="normal")

        g1 = draw_graph(list(hist1), led1, cam1_on[0], (206,147,216))
        gp1 = ImageTk.PhotoImage(g1)
        graph1_photo[0] = gp1
        canvas.itemconfig(graph1_item, image=gp1, state="normal")

    root.after(50, update)  # 20fps UI (camera runs faster in background)

# ── Mouse ─────────────────────────────────────────────────────────────────────
def on_click(event):
    if mode[0] != "calibrate": return
    x, y = event.x, event.y
    if y < CAM_Y or y > CAM_Y + CAM_H: return
    py = y - CAM_Y
    if x < PANEL_W:
        led, sc, px = led0, scale0, x
    else:
        led, sc, px = led1, scale1, x - PANEL_W
    led["cx"] = max(0, int((px - sc["ox"]) / sc["r"]))
    led["cy"] = max(0, int((py - sc["oy"]) / sc["r"]))
    status_msg[0] = f"LED moved to ({led['cx']},{led['cy']})"

def on_scroll(event):
    if mode[0] != "calibrate": return
    delta = 1 if (event.delta > 0 or event.num == 4) else -1
    led = led0 if event.x < PANEL_W else led1
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
    elif k == "up":
        led0["thr"] = min(255, led0["thr"] + 1)
        led1["thr"] = min(255, led1["thr"] + 1)
        status_msg[0] = f"Threshold ↑  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}"
    elif k == "down":
        led0["thr"] = max(0, led0["thr"] - 1)
        led1["thr"] = max(0, led1["thr"] - 1)
        status_msg[0] = f"Threshold ↓  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}"
    elif k == "shift_l" or k == "shift_r":
        pass  # modifier only
    # Shift+arrow = grote stap (10)
    elif event.keysym == "Up" and event.state & 1:
        led0["thr"] = min(255, led0["thr"] + 10)
        led1["thr"] = min(255, led1["thr"] + 10)
        status_msg[0] = f"Threshold ↑↑  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}"
    elif event.keysym == "Down" and event.state & 1:
        led0["thr"] = max(0, led0["thr"] - 10)
        led1["thr"] = max(0, led1["thr"] - 10)
        status_msg[0] = f"Threshold ↓↓  cam0={led0['thr']:.0f}  cam1={led1['thr']:.0f}"

root.bind("<Key>", on_key)

root.after(2000, lambda: threading.Thread(target=run_gemini, daemon=True).start())
root.after(50, update)

print(f"Camera Latency Meter | cam0 TCP {args.jetson}:{args.cam0_port} | cam1 {args.cam1_url}")
print("Keys: SPACE=overlay  C=calibrate  R=Gemini  T=threshold  click=move LED  ESC=quit")
root.mainloop()
