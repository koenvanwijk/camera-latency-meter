#!/usr/bin/env python3
"""
Camera Latency Meter — Calibration + Overlay + Live Latency

cam1 heeft twee ROIs:
  • LED1   (paars)  — directe LED zichtbaar op cam1
  • SCR1   (oranje) — het scherm (wit/zwart balk) zichtbaar op cam1

Latency = LED1 ON-flank → SCR1 ON-flank  (ms)

Keys:
  S           → ROI posities opslaan
  A           → auto-threshold alle ROIs (3s)
  T           → auto-threshold actieve ROI
  Tab         → wissel actieve cam1 ROI (LED1 ↔ SCR1)
  Linksklik   → verplaats actieve ROI
  Scroll      → vergroot/verklein ROI radius
  ↑ / ↓       → threshold ±1
  Shift+↑↓    → threshold ±10
  ESC         → afsluiten
"""

import argparse, socket, time, threading, collections, statistics, json, os, csv, datetime
import urllib.request
import numpy as np
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont
import cv2

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",    default="192.168.86.47")
parser.add_argument("--cam0-port", type=int, default=5001)
parser.add_argument("--cam1-port", type=int, default=5002)
parser.add_argument("--win-x",     type=int, default=0)
parser.add_argument("--win-y",     type=int, default=0)
parser.add_argument("--auto",      action="store_true", help="Auto-threshold + measure at startup")
parser.add_argument("--session",   default="", help="Label voor deze meting (voor vergelijking)")
parser.add_argument("--debounce",  type=int, default=25, help="Debounce ms (0=uit)")
parser.add_argument("--ui-hz",     type=int, default=60, help="UI refresh rate Hz")
args = parser.parse_args()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "roi_config.json")
CSV_FILE    = os.path.join(os.path.dirname(__file__), "latency_log.csv")

# ── Feature switches (live toggleable via keys 1-5) ───────────────────────────
features = {
    "debounce":  True,   # 1 — debounce flanken (25ms stabiel vereist)
    "outlier":   True,   # 2 — outlier filter (2.5×σ)
    "tcp_cam1":  True,   # 3 — cam1 via TCP (False = MJPEG HTTP fallback)
    "ui_60hz":   True,   # 4 — UI 60Hz (False = 20Hz)
    "csv":       True,   # 5 — CSV schrijven aan/uit
}

FEATURE_KEYS = {
    "1": "debounce",
    "2": "outlier",
    "3": "tcp_cam1",
    "4": "ui_60hz",
    "5": "csv",
}

def features_tag():
    """Genereer session tag op basis van actieve features."""
    parts = []
    if features["debounce"]:  parts.append("deb")
    if features["outlier"]:   parts.append("filt")
    if features["tcp_cam1"]:  parts.append("tcp1")
    if features["ui_60hz"]:   parts.append("60hz")
    return "-".join(parts) if parts else "bare"

SESSION_TAG  = [args.session or features_tag()]
DEBOUNCE_MS  = args.debounce   # initial value; overridden live
UI_INTERVAL  = [max(8, 1000 // args.ui_hz)]

# ── Control pipe (/tmp/calibrate_ctrl) ───────────────────────────────────────
CTRL_PIPE = "/tmp/calibrate_ctrl"
def _ctrl_listener():
    import os, stat
    if os.path.exists(CTRL_PIPE): os.remove(CTRL_PIPE)
    os.mkfifo(CTRL_PIPE)
    while True:
        with open(CTRL_PIPE) as f:
            for line in f:
                for ch in line.strip():
                    if ch in FEATURE_KEYS:
                        fname = FEATURE_KEYS[ch]
                        features[fname] = not features[fname]
                        SESSION_TAG[0] = features_tag()
                        UI_INTERVAL[0] = 16 if features["ui_60hz"] else 50
                        print(f"[ctrl] {fname}={'ON' if features[fname] else 'OFF'} → {SESSION_TAG[0]}", flush=True)
threading.Thread(target=_ctrl_listener, daemon=True).start()

WIN_W, WIN_H  = 1920, 1080
PANEL_W       = WIN_W // 2
HEADER_H      = 60
LABEL_H       = 30
CAM_H         = 390
SIGNAL_H      = 50
GRAPH_H       = 220
EXPLAIN_H     = 80
STATUS_H      = 30
CAM_Y         = HEADER_H + LABEL_H
SIGNAL_Y      = CAM_Y + CAM_H
GRAPH_Y       = SIGNAL_Y + SIGNAL_H
EXPLAIN_Y     = GRAPH_Y + GRAPH_H
STATUS_Y      = EXPLAIN_Y + EXPLAIN_H
GRAPH_HISTORY = 300

# ── ROI definitions ───────────────────────────────────────────────────────────
led0 = {"cx": 460, "cy": 164, "r": 12, "thr": 35.0, "label": "LED0", "col": (0, 230, 80)}
led1 = {"cx": 869, "cy": 349, "r": 12, "thr": 30.0, "label": "LED1", "col": (180, 100, 255)}
scr1 = {"cx": 400, "cy": 200, "r": 20, "thr": 128.0,"label": "SCR1", "col": (255, 160, 40)}

def save_config():
    """Save ROI positions + radii (NOT thresholds — those are lighting-dependent)."""
    cfg = {
        "led0": {"cx": led0["cx"], "cy": led0["cy"], "r": led0["r"]},
        "led1": {"cx": led1["cx"], "cy": led1["cy"], "r": led1["r"]},
        "scr1": {"cx": scr1["cx"], "cy": scr1["cy"], "r": scr1["r"]},
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    status_msg[0] = f"Config opgeslagen → {CONFIG_FILE}"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for roi, src in [(led0,"led0"),(led1,"led1"),(scr1,"scr1")]:
            if src in cfg:
                roi["cx"] = cfg[src]["cx"]
                roi["cy"] = cfg[src]["cy"]
                roi["r"]  = cfg[src]["r"]
        print(f"Config geladen: {CONFIG_FILE}")
    except Exception as e:
        print(f"Config load error: {e}")

load_config()

cam1_active_roi = ["led"]

# ── Shared state ──────────────────────────────────────────────────────────────
cam0_frame = [None]; cam0_lock = threading.Lock()
cam1_frame = [None]; cam1_lock = threading.Lock()

cam0_bright = [0.0]; cam0_on = [False]; cam0_fps = [0.0]
cam1_led_bright = [0.0]; cam1_led_on = [False]
cam1_scr_bright = [0.0]; cam1_scr_on = [False]; cam1_fps = [0.0]

hist0     = collections.deque(maxlen=GRAPH_HISTORY)
hist1_led = collections.deque(maxlen=GRAPH_HISTORY)
hist1_scr = collections.deque(maxlen=GRAPH_HISTORY)

# ── Latency tracking ──────────────────────────────────────────────────────────
DEBOUNCE_MS   = DEBOUNCE_MS   # initial; live reads features["debounce"]
OUTLIER_SIGMA = 2.5   # samples beyond N×stddev are ignored

lat_led1_rise  = [None]
lat_led1_fall  = [None]
lat_scr1_rise  = [None]
lat_scr1_fall  = [None]

# debounce state: (pending_state, pending_since)
deb_led = [None, 0.0]
deb_scr = [None, 0.0]

lat_samples    = collections.deque(maxlen=100)   # rise measurements
lat_samples_f  = collections.deque(maxlen=100)   # fall measurements
lat_lock       = threading.Lock()

lat_display = {
    "last": None, "mean": None, "min": None, "max": None,
    "stddev": None, "n": 0,
    "last_f": None, "mean_f": None, "n_f": 0,
}

def _append_sample(samples, dt):
    """Add dt, filter outliers if enabled, return filtered list."""
    samples.append(dt)
    s = list(samples)
    if features["outlier"] and len(s) >= 4:
        m = statistics.mean(s)
        sd = statistics.stdev(s)
        s_filt = [v for v in s if abs(v-m) <= OUTLIER_SIGMA*sd]
        return s_filt if len(s_filt) >= 2 else s
    return s

def _write_csv(edge, dt):
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "session", "edge", "latency_ms",
                        "cam0_fps", "cam1_fps", "debounce_ms", "ui_hz"])
        if features["csv"]:
            w.writerow([datetime.datetime.now().isoformat(), SESSION_TAG[0], edge,
                        f"{dt:.2f}", f"{cam0_fps[0]:.1f}", f"{cam1_fps[0]:.1f}",
                        25 if features["debounce"] else 0,
                        60 if features["ui_60hz"] else 20])

def debounce(deb, new_state, t):
    """Returns confirmed new state after DEBOUNCE_MS stable, else None."""
    deb_ms = 25 if features["debounce"] else 0
    if deb[0] != new_state:
        deb[0] = new_state
        deb[1] = t
        return None
    if (t - deb[1]) * 1000 >= deb_ms:
        return new_state
    return None

def update_latency(raw_led_on, raw_scr_on, t):
    prev_led = cam1_led_on[0]
    prev_scr = cam1_scr_on[0]

    # Debounce
    conf_led = debounce(deb_led, raw_led_on, t)
    conf_scr = debounce(deb_scr, raw_scr_on, t)
    new_led = conf_led if conf_led is not None else prev_led
    new_scr = conf_scr if conf_scr is not None else prev_scr

    led_rise = new_led and not prev_led
    led_fall = not new_led and prev_led
    scr_rise = new_scr and not prev_scr
    scr_fall = not new_scr and prev_scr

    # Rising edge: LED1 ON → SCR1 ON
    if led_rise:
        with lat_lock: lat_led1_rise[0] = t
    if scr_rise and lat_led1_rise[0] is not None:
        dt = (t - lat_led1_rise[0]) * 1000
        if 0 < dt < 3000:
            with lat_lock:
                lat_led1_rise[0] = None
                s = _append_sample(lat_samples, dt)
                _write_csv("rise", dt)
                lat_display["last"]   = dt
                lat_display["mean"]   = statistics.mean(s)
                lat_display["min"]    = min(s)
                lat_display["max"]    = max(s)
                lat_display["stddev"] = statistics.stdev(s) if len(s)>1 else 0
                lat_display["n"]      = len(s)
                # Auto-recalibreer elke 10 metingen
                if len(s) % 10 == 0:
                    threading.Thread(target=run_autothreshold,
                                     kwargs={"all_rois": True}, daemon=True).start()

    # Falling edge: LED1 OFF → SCR1 OFF
    if led_fall:
        with lat_lock: lat_led1_fall[0] = t
    if scr_fall and lat_led1_fall[0] is not None:
        dt = (t - lat_led1_fall[0]) * 1000
        if 0 < dt < 3000:
            with lat_lock:
                lat_led1_fall[0] = None
                s = _append_sample(lat_samples_f, dt)
                _write_csv("fall", dt)
                lat_display["last_f"] = dt
                lat_display["mean_f"] = statistics.mean(s)
                lat_display["n_f"]    = len(s)

    status_parts = []
    if lat_display["last"] is not None:
        status_parts.append(
            f"⏱ rise={lat_display['last']:.0f}ms "
            f"avg={lat_display['mean']:.0f}±{lat_display['stddev']:.0f}ms "
            f"[{lat_display['min']:.0f}–{lat_display['max']:.0f}] n={lat_display['n']}")
    if lat_display["last_f"] is not None:
        status_parts.append(f"fall={lat_display['last_f']:.0f}ms avg={lat_display['mean_f']:.0f}ms n={lat_display['n_f']}")
    if status_parts:
        status_msg[0] = "  |  ".join(status_parts)

    return new_led, new_scr

status_msg = ["Streams verbinden..."]

# ── Stream loops ──────────────────────────────────────────────────────────────
def measure_roi(gray, cfg):
    cx, cy, r = cfg["cx"], cfg["cy"], cfg["r"]
    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
    return float(roi.mean()) if roi.size > 0 else 0.0

def update_latency_OLD(): pass  # replaced — see new update_latency above

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
                    v = measure_roi(gray, led0)
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
            if features["tcp_cam1"]:
                sock = socket.socket()
                sock.connect((args.jetson, args.cam1_port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(3)
                buf = b""
                def _read():
                    return sock.recv(65536)
                def _close(): sock.close()
            else:
                req = urllib.request.urlopen(f"http://{args.jetson}:8091/stream", timeout=10)
                buf = b""
                def _read(): return req.read(32768)
                def _close(): req.close()

            while True:
                chunk = _read()
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

                    vl = measure_roi(gray, led1)
                    vs = measure_roi(gray, scr1)
                    new_led = vl > led1["thr"]
                    new_scr = vs > scr1["thr"]

                    conf_led, conf_scr = update_latency(new_led, new_scr, now)

                    cam1_led_bright[0] = vl; cam1_led_on[0] = conf_led
                    cam1_scr_bright[0] = vs; cam1_scr_on[0] = conf_scr
                    hist1_led.append((now, vl))
                    hist1_scr.append((now, vs))

                    with cam1_lock: cam1_frame[0] = arr
                    fc += 1
                    if fc % 30 == 0:
                        cam1_fps[0] = 30 / max(0.01, time.time()-t0)
                        t0 = time.time(); fc = 0
            _close()
        except: time.sleep(0.3)

threading.Thread(target=cam0_loop, daemon=True).start()
threading.Thread(target=cam1_loop, daemon=True).start()

# ── Auto-threshold ────────────────────────────────────────────────────────────
def run_autothreshold(all_rois=False, on_done=None):
    """Measure 3s and set thresholds. all_rois=True does all three at once."""
    def _run():
        targets = [(led0, lambda: cam0_bright[0]),
                   (led1, lambda: cam1_led_bright[0]),
                   (scr1, lambda: cam1_scr_bright[0])] if all_rois else \
                  [(led0, lambda: cam0_bright[0]),
                   (active_cam1_cfg(), lambda: cam1_led_bright[0] if cam1_active_roi[0]=="led" else cam1_scr_bright[0])]
        status_msg[0] = "Auto-threshold: collecting 3s..."
        samples = {id(cfg): [] for cfg, _ in targets}
        t = time.time()
        while time.time()-t < 3.5:
            for cfg, getter in targets:
                samples[id(cfg)].append(getter())
            time.sleep(0.02)
        parts = []
        for cfg, _ in targets:
            vals = samples[id(cfg)]
            mn, mx = min(vals), max(vals)
            if mx-mn > 4:
                cfg["thr"] = (mn+mx)/2
            parts.append(f"{cfg['label']}={cfg['thr']:.0f}")
        status_msg[0] = "Threshold ✓  " + "  ".join(parts)
        if on_done: on_done()
    threading.Thread(target=_run, daemon=True).start()

def save_screenshot():
    """Composite screenshot: cam0, cam1, graph, explain panels."""
    with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
    with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None
    parts = [draw_cam0(f0), draw_cam1(f1)]
    top = Image.new("RGB", (WIN_W, CAM_H))
    top.paste(parts[0], (0, 0)); top.paste(parts[1], (PANEL_W, 0))
    graph = draw_triple_graph(list(hist0), list(hist1_led), list(hist1_scr))
    explain = draw_explain()
    full = Image.new("RGB", (WIN_W, CAM_H + GRAPH_H + EXPLAIN_H))
    full.paste(top, (0, 0))
    full.paste(graph, (0, CAM_H))
    full.paste(explain, (0, CAM_H + GRAPH_H))
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(os.path.dirname(__file__), f"screenshot_{ts}.png")
    full.save(path)
    status_msg[0] = f"Screenshot → {path}"


    """Wait for streams, auto-threshold all ROIs, then start measuring."""
    def _run():
        status_msg[0] = "Auto-start: wachten op streams..."
        deadline = time.time() + 10
        while time.time() < deadline:
            if cam0_frame[0] is not None and cam1_frame[0] is not None:
                break
            time.sleep(0.2)
        if cam0_frame[0] is None or cam1_frame[0] is None:
            status_msg[0] = "Auto-start: streams niet bereikbaar — handmatig calibreren"
            return
        status_msg[0] = "Auto-start: streams OK, threshold kalibreren..."
        time.sleep(0.5)
        done = threading.Event()
        run_autothreshold(all_rois=True, on_done=done.set)
        done.wait(timeout=8)
        status_msg[0] = "Auto-start klaar — meting loopt. Thresholds: " + \
                        f"LED0={led0['thr']:.0f} LED1={led1['thr']:.0f} SCR1={scr1['thr']:.0f}"
    threading.Thread(target=_run, daemon=True).start()

# ── Draw camera panels ─────────────────────────────────────────────────────────
def _draw_roi(draw, cfg, bright, on, ratio, ox, oy, active=True):
    cx_d = int(cfg["cx"]*ratio) + ox
    cy_d = int(cfg["cy"]*ratio) + oy
    r_d  = max(5, int(cfg["r"]*ratio))
    col  = cfg["col"] if active else tuple(int(c*0.45) for c in cfg["col"])
    lw   = 3 if active else 1
    draw.ellipse([cx_d-r_d, cy_d-r_d, cx_d+r_d, cy_d+r_d], outline=col, width=lw)
    if active:
        draw.line([(cx_d-r_d-10, cy_d), (cx_d+r_d+10, cy_d)], fill=col, width=1)
        draw.line([(cx_d, cy_d-r_d-10), (cx_d, cy_d+r_d+10)], fill=col, width=1)
    state_col = col if on else tuple(int(c*0.6) for c in col)
    draw.text((cx_d+r_d+5, cy_d-18), cfg["label"], fill=col)
    draw.text((cx_d+r_d+5, cy_d- 4), f"{bright:.1f} {'ON' if on else 'off'}", fill=state_col)

def _render_frame(frame_bgr, w_out, h_out, scale):
    img = Image.new("RGB", (w_out, h_out), (25,25,25))
    if frame_bgr is None:
        ImageDraw.Draw(img).text((20, h_out//2), "Waiting for stream...", fill=(100,100,100))
        scale["ox"] = scale["oy"] = 0; scale["r"] = 1.0
        return img
    h, w = frame_bgr.shape[:2]
    ratio = min(w_out/w, h_out/h)
    nw, nh = int(w*ratio), int(h*ratio)
    ox = (w_out-nw)//2; oy = (h_out-nh)//2
    scale["ox"] = ox; scale["oy"] = oy; scale["r"] = ratio
    img.paste(Image.fromarray(cv2.cvtColor(cv2.resize(frame_bgr,(nw,nh)), cv2.COLOR_BGR2RGB)), (ox,oy))
    return img, ratio, ox, oy

scale0 = {"ox":0,"oy":0,"r":1.0}
scale1 = {"ox":0,"oy":0,"r":1.0}

def draw_cam0(frame_bgr):
    res = _render_frame(frame_bgr, PANEL_W, CAM_H, scale0)
    if isinstance(res, tuple):
        img, ratio, ox, oy = res
        _draw_roi(ImageDraw.Draw(img), led0, cam0_bright[0], cam0_on[0], ratio, ox, oy)
    else: img = res
    return img

def draw_cam1(frame_bgr):
    res = _render_frame(frame_bgr, PANEL_W, CAM_H, scale1)
    if isinstance(res, tuple):
        img, ratio, ox, oy = res
        draw = ImageDraw.Draw(img)
        if cam1_active_roi[0] == "led":
            _draw_roi(draw, scr1, cam1_scr_bright[0], cam1_scr_on[0], ratio, ox, oy, active=False)
            _draw_roi(draw, led1, cam1_led_bright[0], cam1_led_on[0], ratio, ox, oy, active=True)
        else:
            _draw_roi(draw, led1, cam1_led_bright[0], cam1_led_on[0], ratio, ox, oy, active=False)
            _draw_roi(draw, scr1, cam1_scr_bright[0], cam1_scr_on[0], ratio, ox, oy, active=True)
        active = led1 if cam1_active_roi[0]=="led" else scr1
        draw.text((8,8), f"[Tab] actief: {active['label']}", fill=active["col"])
    else: img = res
    return img

# ── 3-track graph ──────────────────────────────────────────────────────────────
def draw_triple_graph(h0, h_led, h_scr):
    W = WIN_W
    img = Image.new("RGB", (W, GRAPH_H), (12,12,18))
    draw = ImageDraw.Draw(img)

    PAD_L, PAD_R, PAD_T, PAD_B = 64, 12, 6, 20
    gw = W - PAD_L - PAD_R
    gh = GRAPH_H - PAD_T - PAD_B
    TH = gh // 3

    TRACKS = [
        (h0,    led0, cam0_bright[0],     cam0_on[0],     "CAM0 LED0", (0,220,80)),
        (h_led, led1, cam1_led_bright[0], cam1_led_on[0], "CAM1 LED1", (170,90,255)),
        (h_scr, scr1, cam1_scr_bright[0], cam1_scr_on[0], "CAM1 SCR1", (255,155,30)),
    ]

    all_pts = list(h0) + list(h_led) + list(h_scr)
    if not all_pts:
        draw.text((PAD_L+10, PAD_T+10), "Collecting data...", fill=(80,80,80))
        return img

    t_end  = max(t for t,_ in all_pts)
    t_span = max(5.0, t_end - min(t for t,_ in all_pts))

    def xx(t): return PAD_L + int(((t-(t_end-t_span))/t_span)*gw)

    # Time grid
    for i in range(6):
        x = PAD_L + int(i*gw/5)
        draw.line([(x, PAD_T), (x, PAD_T+gh)], fill=(35,35,45), width=1)
        draw.text((x-12, PAD_T+gh+3), f"-{t_span*(1-i/5):.1f}s", fill=(50,50,55))

    for ti, (hist, cfg, bright_now, on_now, label, col) in enumerate(TRACKS):
        tt = PAD_T + ti*TH
        tb = tt + TH - 2
        tmid = (tt+tb)//2

        draw.rectangle([PAD_L, tt, W-PAD_R, tb], fill=(18,18,25))
        draw.line([(0, tt), (W, tt)], fill=(40,40,50))
        draw.text((2, tmid-8), label, fill=tuple(int(c*0.7) for c in col))

        pts = list(hist)
        if len(pts) < 2:
            draw.text((PAD_L+10, tmid-6), "collecting...", fill=(60,60,60))
            continue

        vals = [v for _,v in pts]
        thr  = cfg["thr"]
        vmin = max(0,   min(vals+[thr])-5)
        vmax = min(255, max(vals+[thr])+5)
        vr   = max(vmax-vmin, 8)

        def yx(v, _tt=tt, _tb=tb, _vm=vmin, _vr=vr):
            return _tt + int((1-(v-_vm)/_vr)*(_tb-_tt))

        thr_y = yx(thr)
        draw.rectangle([PAD_L, tt,    W-PAD_R, thr_y], fill=(0,25,10))
        draw.rectangle([PAD_L, thr_y, W-PAD_R, tb],    fill=(25,8,8))
        draw.line([(PAD_L, thr_y), (W-PAD_R, thr_y)], fill=(200,160,0), width=1)
        draw.text((PAD_L-62, thr_y-7), f"thr={thr:.0f}", fill=(180,140,0))

        pts_vis = [(xx(t), yx(v)) for t,v in pts if PAD_L <= xx(t) <= W-PAD_R]
        if len(pts_vis) >= 2:
            col_on  = col
            col_off = tuple(int(c*0.4) for c in col)
            for i in range(len(pts_vis)-1):
                draw.line([pts_vis[i], pts_vis[i+1]],
                          fill=col_on if vals[i]>thr else col_off, width=2)
        if pts_vis:
            lx, ly = pts_vis[-1]
            dc = col if on_now else tuple(int(c*0.4) for c in col)
            draw.ellipse([lx-4,ly-4,lx+4,ly+4], fill=dc)
            draw.text((lx+6, ly-8), f"{bright_now:.1f}", fill=dc)
        draw.text((W-PAD_R-50, tmid-7),
                  "● ON" if on_now else "○ off",
                  fill=col if on_now else (80,80,80))

    # Latency annotation + mini histogram
    ld = lat_display
    if ld["last"] is not None:
        txt = (f"rise: last={ld['last']:.0f}ms  "
               f"avg={ld['mean']:.0f}±{ld['stddev']:.0f}ms  "
               f"[{ld['min']:.0f}–{ld['max']:.0f}]  n={ld['n']}")
        if ld["last_f"] is not None:
            txt += f"   fall: {ld['last_f']:.0f}ms avg={ld['mean_f']:.0f}ms"
        draw.text((PAD_L, GRAPH_H-PAD_B+2), txt, fill=(255,220,60))

        # Mini histogram (right side, last 100 rise samples)
        samples = list(lat_samples)
        if len(samples) >= 3:
            HW, HH = 140, gh - 4   # histogram width/height
            HX = W - PAD_R - HW - 4
            HY = PAD_T + 2
            draw.rectangle([HX, HY, HX+HW, HY+HH], fill=(14,14,22))
            draw.rectangle([HX, HY, HX+HW, HY+HH], outline=(40,40,60))
            bins = 14
            lo, hi = min(samples), max(samples)
            if hi > lo:
                bw = (hi - lo) / bins
                counts = [0] * bins
                for v in samples:
                    bi = min(bins-1, int((v-lo)/bw))
                    counts[bi] += 1
                mc = max(counts)
                bar_w = max(1, HW // bins)
                for bi, cnt in enumerate(counts):
                    bh = int((cnt/mc) * (HH-14)) if mc > 0 else 0
                    bx = HX + bi * bar_w
                    by = HY + HH - bh - 2
                    c = (255,200,40) if bh > 0 else (30,30,40)
                    draw.rectangle([bx+1, by, bx+bar_w-1, HY+HH-2], fill=c)
                # mean line
                mx_pos = HX + int((ld['mean']-lo)/(hi-lo)*HW)
                draw.line([(mx_pos, HY+2), (mx_pos, HY+HH-2)], fill=(100,200,255), width=1)
            draw.text((HX+2, HY+1), f"hist n={len(samples)}", fill=(80,80,100))
    else:
        draw.text((PAD_L, GRAPH_H-PAD_B+2), "Wachten op LED1→SCR1 transitie...", fill=(80,80,80))

    return img

# ── Explanation panel ──────────────────────────────────────────────────────────
def draw_explain():
    img = Image.new("RGB", (WIN_W, EXPLAIN_H), (8, 8, 14))
    draw = ImageDraw.Draw(img)
    ld = lat_display

    cam0_frame_ms = 1000/max(1, cam0_fps[0])
    cam1_frame_ms = 1000/max(1, cam1_fps[0])

    components = [
        ("cam0 capture",  cam0_frame_ms,  (0, 200, 80)),
        ("TCP stream",    None,           (80, 160, 255)),
        ("UI poll",       16.0,           (200, 200, 60)),
        ("cam1 capture",  cam1_frame_ms,  (170, 90, 255)),
        ("cam1 MJPEG",    cam1_frame_ms,  (255, 140, 30)),
    ]
    est_total = sum(v for _, v, _ in components if v is not None)

    # ── Left: pipeline breakdown
    draw.text((8, 4), "Pipeline (geschat):", fill=(130,130,150))
    x2 = 8
    for label, val, col in components:
        val_str = f"{val:.0f}ms" if val is not None else "?"
        draw.text((x2, 18), f"• {label}", fill=col)
        draw.text((x2, 30), f"  {val_str}", fill=tuple(int(c*0.8) for c in col))
        x2 += 155
    draw.text((8, 46), f"Σ geschat ≈ {est_total:.0f}ms  |  cam0:{cam0_fps[0]:.0f}fps  cam1:{cam1_fps[0]:.0f}fps",
              fill=(160,160,80))
    csv_n = len(lat_samples) + len(lat_samples_f)
    feat_str = "  ".join(f"[{'ON' if v else '--'}] {k}" for k,v in features.items())
    draw.text((8, 60), f"sessie: {SESSION_TAG[0]}   {feat_str}", fill=(80,80,120))

    # ── Middle: live measurements
    MX = WIN_W//2 - 60
    draw.line([(MX, 3), (MX, EXPLAIN_H-3)], fill=(35,35,45), width=1)
    if ld["last"] is not None:
        rows = [
            ("RISE  laatste",  f"{ld['last']:.1f} ms",   (255,240,80)),
            ("RISE  gem ± σ",  f"{ld['mean']:.1f} ± {ld['stddev']:.1f} ms", (100,200,255)),
            ("RISE  min/max",  f"{ld['min']:.1f} / {ld['max']:.1f} ms",     (160,160,210)),
            ("RISE  n (gefilterd)", f"{ld['n']}",                            (100,100,130)),
        ]
        if ld["last_f"] is not None:
            rows.append(("FALL  laatste", f"{ld['last_f']:.1f} ms  avg={ld['mean_f']:.1f}ms  n={ld['n_f']}", (200,160,255)))
        draw.text((MX+8, 4), "Gemeten latency:", fill=(180,180,200))
        for i, (lbl, val, col) in enumerate(rows):
            draw.text((MX+8,  18+i*13), lbl, fill=(120,120,140))
            draw.text((MX+145, 18+i*13), val, fill=col)
    else:
        draw.text((MX+8, 24), "Wacht op eerste meting...", fill=(80,80,100))
        draw.text((MX+8, 40), "Positioneer SCR1 op de balk, druk T", fill=(70,70,90))

    # ── Right: what is being measured
    RX = WIN_W*3//4
    draw.line([(RX, 3), (RX, EXPLAIN_H-3)], fill=(35,35,45), width=1)
    draw.text((RX+8,  4), "Wat meet je  (end-to-end):", fill=(150,150,170))
    draw.text((RX+8, 18), "ESP32 LED ON  →  cam0 detecteert flank", fill=(80,200,100))
    draw.text((RX+8, 31), "→  Python zet balk WIT op scherm", fill=(200,200,200))
    draw.text((RX+8, 44), "→  cam1 ziet balk (SCR1 stijgt)", fill=(200,140,255))
    draw.text((RX+8, 57), "→  Δt = volledige pipeline latency", fill=(255,210,60))

    return img

# ── Tkinter ───────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Camera Latency Meter")
root.geometry(f"{WIN_W}x{WIN_H}+{args.win_x}+{args.win_y}")
root.attributes("-fullscreen", True)
root.configure(bg="#111")

canvas = tk.Canvas(root, bg="#111", highlightthickness=0, cursor="crosshair")
canvas.pack(fill="both", expand=True)

# Header
canvas.create_rectangle(0, 0, WIN_W, HEADER_H, fill="#0d1b2a", outline="")
canvas.create_text(WIN_W//2, HEADER_H//2,
    text="ESP32 LED  →  cam0 detecteert LED  →  wit/zwart balk op scherm  →  cam1 ziet LED + BALK  →  LATENCY",
    fill="#4fc3f7", font=("monospace", 13, "bold"), anchor="center")

canvas.create_line(PANEL_W, HEADER_H, PANEL_W, SIGNAL_Y+SIGNAL_H, fill="#333", width=2)

canvas.create_rectangle(0,       HEADER_H, PANEL_W, HEADER_H+LABEL_H, fill="#0d3b2e", outline="")
canvas.create_text(PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM0  —  detecteert LED  →  stuurt balk naar scherm",
    fill="#00e676", font=("monospace", 13, "bold"), anchor="center")

canvas.create_rectangle(PANEL_W, HEADER_H, WIN_W, HEADER_H+LABEL_H, fill="#2d1b4e", outline="")
canvas.create_text(PANEL_W+PANEL_W//2, HEADER_H+LABEL_H//2,
    text="CAM1  —  ziet LED (paars) + BALK (oranje)  →  MEET latency",
    fill="#ce93d8", font=("monospace", 13, "bold"), anchor="center")

cam0_item  = canvas.create_image(0,       CAM_Y, anchor="nw")
cam1_item  = canvas.create_image(PANEL_W, CAM_Y, anchor="nw")

signal_bar = canvas.create_rectangle(0, SIGNAL_Y, WIN_W, SIGNAL_Y+SIGNAL_H,
                                      fill="#000000", outline="")
signal_txt = canvas.create_text(WIN_W//2, SIGNAL_Y+SIGNAL_H//2,
    text="○ LED OFF", fill="#ffffff", font=("monospace", 20, "bold"), anchor="center")

graph_item   = canvas.create_image(0, GRAPH_Y,   anchor="nw")
canvas.create_line(0, GRAPH_Y, WIN_W, GRAPH_Y, fill="#333", width=1)
explain_item = canvas.create_image(0, EXPLAIN_Y, anchor="nw")
canvas.create_line(0, EXPLAIN_Y, WIN_W, EXPLAIN_Y, fill="#222", width=1)

canvas.create_rectangle(0, STATUS_Y, WIN_W, WIN_H, fill="#0a0a0a", outline="")
status_item = canvas.create_text(WIN_W//2, STATUS_Y+STATUS_H//2,
    text="", fill="yellow", font=("monospace", 13), anchor="center")
canvas.create_text(WIN_W-10, STATUS_Y+STATUS_H//2,
    text="S=opslaan  P=screenshot  T=threshold  Tab=ROI  "
         "1=debounce  2=outlier  3=tcp_cam1  4=ui_60hz  5=csv  ESC",
    fill="#444", font=("monospace", 11), anchor="e")

cam0_ph=[None]; cam1_ph=[None]; graph_ph=[None]; explain_ph=[None]

# ── Render loop ───────────────────────────────────────────────────────────────
def update():
    canvas.itemconfig(status_item, state="normal", text=status_msg[0])
    canvas.itemconfig(signal_bar,  state="normal")
    canvas.itemconfig(signal_txt,  state="normal")

    if cam0_on[0]:
        canvas.itemconfig(signal_bar, fill="#ffffff")
        canvas.itemconfig(signal_txt, text="● LED ON",  fill="#000000")
    else:
        canvas.itemconfig(signal_bar, fill="#000000")
        canvas.itemconfig(signal_txt, text="○ LED OFF", fill="#ffffff")

    with cam0_lock: f0 = cam0_frame[0].copy() if cam0_frame[0] is not None else None
    with cam1_lock: f1 = cam1_frame[0].copy() if cam1_frame[0] is not None else None

    p0 = ImageTk.PhotoImage(draw_cam0(f0)); cam0_ph[0] = p0
    canvas.itemconfig(cam0_item, image=p0, state="normal")

    p1 = ImageTk.PhotoImage(draw_cam1(f1)); cam1_ph[0] = p1
    canvas.itemconfig(cam1_item, image=p1, state="normal")

    gp = ImageTk.PhotoImage(draw_triple_graph(list(hist0), list(hist1_led), list(hist1_scr)))
    graph_ph[0] = gp
    canvas.itemconfig(graph_item, image=gp, state="normal")

    ep = ImageTk.PhotoImage(draw_explain())
    explain_ph[0] = ep
    canvas.itemconfig(explain_item, image=ep, state="normal")

    root.after(UI_INTERVAL[0], update)

# ── Mouse ─────────────────────────────────────────────────────────────────────
def on_click(event):
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
    status_msg[0] = f"{cfg['label']} → ({cfg['cx']},{cfg['cy']}) — threshold herberekenen..."
    run_autothreshold(all_rois=True)

def on_scroll(event):
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
    elif k == "s":      save_config()
    elif k == "p":      threading.Thread(target=save_screenshot, daemon=True).start()
    elif k in FEATURE_KEYS:
        fname = FEATURE_KEYS[k]
        features[fname] = not features[fname]
        SESSION_TAG[0] = features_tag()
        UI_INTERVAL[0] = 16 if features["ui_60hz"] else 50
        status_msg[0] = (f"[{k}] {fname} = {'ON' if features[fname] else 'OFF'}"
                         f"  →  sessie: {SESSION_TAG[0]}")
    elif k == "t":      run_autothreshold(all_rois=True)
    elif k == "tab":
        cam1_active_roi[0] = "scr" if cam1_active_roi[0]=="led" else "led"
        status_msg[0] = f"Actieve cam1 ROI: {active_cam1_cfg()['label']}"

root.bind("<Key>", on_key)
root.after(50, update)

if args.auto:
    root.after(500, auto_start_sequence)
elif os.path.exists(CONFIG_FILE):
    # Config gevonden: posities laden, thresholds nog kalibreren
    root.after(500, lambda: run_autothreshold(all_rois=True))

print(f"Camera Latency Meter | cam0 TCP {args.jetson}:{args.cam0_port} | cam1 TCP {args.jetson}:{args.cam1_port}")
print("S=opslaan  A=thr(alles)  T=thr(actief)  Tab=ROI  SPACE=overlay  C=calib  ESC=quit")
root.mainloop()
