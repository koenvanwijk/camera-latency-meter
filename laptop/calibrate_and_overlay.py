#!/usr/bin/env python3
"""
Calibration viewer — live camera feed + LED detection overlay.

Phase 1 (CALIBRATE): Shows live cam0 image with:
  - LED ROI circle (drag to reposition)
  - Real-time brightness value
  - ON/OFF indicator
  - Auto-threshold finder
  Press SPACE to confirm and switch to overlay mode.
  Press R to re-run Gemini detect.

Phase 2 (OVERLAY): Full-screen white/black overlay.
  Press ESC to quit, C to go back to calibrate.

Usage:
    DISPLAY=:1 python3 calibrate_and_overlay.py [--jetson IP] [--port 5001]
"""

import argparse, socket, time, threading, base64, json
import urllib.request, os
import numpy as np
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageDraw
import cv2

parser = argparse.ArgumentParser()
parser.add_argument("--jetson",  default="192.168.86.47")
parser.add_argument("--port",    type=int, default=5001)
parser.add_argument("--win-x",   type=int, default=0)
parser.add_argument("--win-y",   type=int, default=0)
args = parser.parse_args()

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "AIzaSyC2PBKX4PqU3nr7GRBdkUCkymll2ulesJ8")
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL   = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")

# ── Shared state ──────────────────────────────────────────────────────────────
mode         = ["calibrate"]   # "calibrate" | "overlay"
latest_frame = [None]          # latest raw BGR frame from cam0
frame_lock   = threading.Lock()

led = {"cx": 460, "cy": 164, "r": 12, "thr": 35.0}
led_brightness  = [0.0]
led_on          = [False]
fps_counter     = [0, time.time()]  # [count, t0]
current_fps     = [0.0]
gemini_running  = [False]
gemini_status   = [""]

# Display size for calibration view (scaled to fit screen)
DISP_W, DISP_H = 1280, 720

# ── TCP stream ────────────────────────────────────────────────────────────────
def stream_loop():
    while True:
        try:
            sock = socket.socket()
            sock.connect((args.jetson, args.port))
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

                    # LED detection
                    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
                    cx, cy, r = led["cx"], led["cy"], led["r"]
                    roi = gray[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
                    v = float(roi.mean()) if roi.size > 0 else 0.0
                    led_brightness[0] = v
                    led_on[0] = v > led["thr"]

                    with frame_lock:
                        latest_frame[0] = arr

                    # FPS
                    fps_counter[0] += 1
                    dt = time.time() - fps_counter[1]
                    if dt >= 1.0:
                        current_fps[0] = fps_counter[0] / dt
                        fps_counter[0] = 0
                        fps_counter[1] = time.time()
            sock.close()
        except Exception as ex:
            time.sleep(0.3)

threading.Thread(target=stream_loop, daemon=True).start()

# ── Gemini ────────────────────────────────────────────────────────────────────
def run_gemini():
    if gemini_running[0]: return
    gemini_running[0] = True
    gemini_status[0]  = "Gemini: asking..."

    def _run():
        with frame_lock:
            frame = latest_frame[0].copy() if latest_frame[0] is not None else None
        if frame is None:
            gemini_status[0] = "Gemini: no frame yet"
            gemini_running[0] = False
            return
        h, w = frame.shape[:2]
        jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
        b64 = base64.b64encode(jpg).decode()
        prompt = (
            f"Camera image {w}x{h}px, robotics latency setup. "
            "Find the small blinking LED and return ONLY JSON: "
            "{\"cx\": <x>, \"cy\": <y>}. No markdown."
        )
        body = json.dumps({"contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
        ]}]}).encode()
        req = urllib.request.Request(GEMINI_URL, data=body,
                                      headers={"Content-Type": "application/json"})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = text.strip("`").strip()
            if text.lower().startswith("json"): text = text[4:].strip()
            r = json.loads(text)
            raw_cx, raw_cy = int(r["cx"]), int(r["cy"])
            # Gemini normalizes to 0-1000 range — scale back to pixel coords
            if raw_cx <= 1000 and raw_cy <= 1000 and (raw_cx > w or raw_cy > h):
                cx = int(raw_cx / 1000 * w)
                cy = int(raw_cy / 1000 * h)
            else:
                cx, cy = raw_cx, raw_cy
            if 0 <= cx < w and 0 <= cy < h:
                led["cx"], led["cy"] = cx, cy
                gemini_status[0] = f"Gemini: LED @ ({cx},{cy}) ✓"
            else:
                gemini_status[0] = f"Gemini: out of frame ({cx},{cy})"
        except Exception as ex:
            gemini_status[0] = f"Gemini: {ex}"
        gemini_running[0] = False

    threading.Thread(target=_run, daemon=True).start()

# ── Auto threshold ─────────────────────────────────────────────────────────────
def auto_threshold():
    """Collect 3s of brightness samples, find bimodal threshold."""
    vals = []
    t = time.time()
    while time.time()-t < 3.5:
        vals.append(led_brightness[0])
        time.sleep(0.02)
    if not vals: return
    mn, mx = min(vals), max(vals)
    if mx - mn > 4:
        led["thr"] = (mn + mx) / 2
        gemini_status[0] = f"Threshold: {led['thr']:.0f} (range {mn:.0f}–{mx:.0f}) ✓"
    else:
        gemini_status[0] = f"Threshold: spread too small ({mx-mn:.1f}) — LED blinking?"

# ── Tkinter UI ────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Camera Latency Calibration")
root.geometry(f"1920x1080+{args.win_x}+{args.win_y}")
root.attributes("-fullscreen", True)
root.configure(bg="#111")

canvas = tk.Canvas(root, bg="#111", highlightthickness=0, cursor="crosshair")
canvas.pack(fill="both", expand=True)

# Pre-bake overlay images
WHITE_IMG = ImageTk.PhotoImage(Image.new("RGB", (1920, 1080), (255,255,255)))
BLACK_IMG = ImageTk.PhotoImage(Image.new("RGB", (1920, 1080), (0,0,0)))
overlay_img_item = canvas.create_image(0, 0, anchor="nw", image=BLACK_IMG)
canvas.itemconfigure(overlay_img_item, state="hidden")

# Camera view items
cam_img_item = canvas.create_image(0, 0, anchor="nw")

# LED circle
led_circle = canvas.create_oval(0,0,1,1, outline="lime", width=3, dash=(4,3))
led_dot    = canvas.create_oval(0,0,1,1, fill="lime", outline="")

# Info panel background
info_bg = canvas.create_rectangle(0, 0, 420, 260, fill="#000000", outline="", stipple="gray50")

# Text labels
mono = tkfont.Font(family="monospace", size=15, weight="bold")
lbl_fps    = canvas.create_text(20, 20,  anchor="nw", fill="white",  font=mono, text="fps: —")
lbl_bright = canvas.create_text(20, 50,  anchor="nw", fill="white",  font=mono, text="brightness: —")
lbl_thr    = canvas.create_text(20, 80,  anchor="nw", fill="white",  font=mono, text="threshold: —")
lbl_state  = canvas.create_text(20, 115, anchor="nw", fill="lime",   font=tkfont.Font(family="monospace", size=28, weight="bold"), text="LED: —")
lbl_pos    = canvas.create_text(20, 160, anchor="nw", fill="#aaa",   font=mono, text="pos: —")
lbl_gemini = canvas.create_text(20, 190, anchor="nw", fill="yellow", font=mono, text="")
lbl_keys   = canvas.create_text(20, 230, anchor="nw", fill="#888",
    font=tkfont.Font(family="monospace", size=12),
    text="[SPACE] overlay  [R] Gemini  [T] threshold  [ESC] quit  [C] calibrate")

cam_photo = [None]

# Scale factor from original frame to display
scale = [1.0, 1.0]

def render_calibrate(frame_bgr):
    h, w = frame_bgr.shape[:2]
    # Scale to fit 1920x1080 keeping aspect ratio
    sw = min(1920, w); sh = min(1080, h)
    ratio = min(sw/w, sh/h)
    nw, nh = int(w*ratio), int(h*ratio)
    scale[0] = ratio; scale[1] = ratio
    ox = (1920 - nw) // 2
    oy = (1080 - nh) // 2

    # Resize frame
    disp = cv2.resize(frame_bgr, (nw, nh))

    # Draw LED ROI on frame
    cx_d = int(led["cx"] * ratio) + ox
    cy_d = int(led["cy"] * ratio) + oy
    r_d  = max(4, int(led["r"] * ratio))

    img = Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    # LED circle
    col = (0, 255, 0) if led_on[0] else (255, 80, 80)
    draw.ellipse([cx_d-r_d-2, cy_d-r_d-2, cx_d+r_d+2, cy_d+r_d+2],
                 outline=col, width=3)
    # Crosshair lines
    draw.line([(cx_d-r_d-10, cy_d), (cx_d+r_d+10, cy_d)], fill=col, width=1)
    draw.line([(cx_d, cy_d-r_d-10), (cx_d, cy_d+r_d+10)], fill=col, width=1)

    photo = ImageTk.PhotoImage(img)
    cam_photo[0] = photo
    canvas.coords(cam_img_item, ox, oy)
    canvas.itemconfig(cam_img_item, image=photo)
    canvas.itemconfig(cam_img_item, state="normal")

    # Update text
    v = led_brightness[0]
    on = led_on[0]
    canvas.itemconfig(lbl_fps,    text=f"fps: {current_fps[0]:.0f}")
    canvas.itemconfig(lbl_bright, text=f"brightness: {v:.1f}")
    canvas.itemconfig(lbl_thr,    text=f"threshold:  {led['thr']:.1f}")
    canvas.itemconfig(lbl_state,  text=f"LED: {'ON ✓' if on else 'OFF'}",
                      fill="lime" if on else "#f44")
    canvas.itemconfig(lbl_pos,    text=f"pos: ({led['cx']},{led['cy']}) r={led['r']}")
    canvas.itemconfig(lbl_gemini, text=gemini_status[0])

def update():
    if mode[0] == "calibrate":
        canvas.itemconfig(overlay_img_item, state="hidden")
        canvas.itemconfig(cam_img_item,     state="normal")
        canvas.itemconfig(led_circle,        state="normal")
        canvas.itemconfig(lbl_fps,           state="normal")
        canvas.itemconfig(lbl_bright,        state="normal")
        canvas.itemconfig(lbl_thr,           state="normal")
        canvas.itemconfig(lbl_state,         state="normal")
        canvas.itemconfig(lbl_pos,           state="normal")
        canvas.itemconfig(lbl_gemini,        state="normal")
        canvas.itemconfig(lbl_keys,          state="normal")
        canvas.itemconfig(info_bg,           state="normal")

        with frame_lock:
            frame = latest_frame[0]
        if frame is not None:
            render_calibrate(frame)

    else:  # overlay
        canvas.itemconfig(cam_img_item,  state="hidden")
        canvas.itemconfig(led_circle,    state="hidden")
        canvas.itemconfig(lbl_fps,       state="hidden")
        canvas.itemconfig(lbl_bright,    state="hidden")
        canvas.itemconfig(lbl_thr,       state="hidden")
        canvas.itemconfig(lbl_pos,       state="hidden")
        canvas.itemconfig(lbl_gemini,    state="hidden")
        canvas.itemconfig(lbl_keys,      state="hidden")
        canvas.itemconfig(info_bg,       state="hidden")
        canvas.itemconfig(overlay_img_item, state="normal")

        img = WHITE_IMG if led_on[0] else BLACK_IMG
        canvas.itemconfig(overlay_img_item, image=img)
        # Small status in corner
        canvas.itemconfig(lbl_state, state="normal",
                          text=f"fps={current_fps[0]:.0f} led={'ON' if led_on[0] else 'OFF'}",
                          fill="gray")
        canvas.coords(lbl_state, 10, 10)

    root.after(16, update)  # ~60Hz

# ── Click to move LED ─────────────────────────────────────────────────────────
def on_click(event):
    if mode[0] != "calibrate": return
    with frame_lock:
        frame = latest_frame[0]
    if frame is None: return
    h, w = frame.shape[:2]
    ratio = scale[0]
    ox = (1920 - int(w*ratio)) // 2
    oy = (1080 - int(h*ratio)) // 2
    cx = int((event.x - ox) / ratio)
    cy = int((event.y - oy) / ratio)
    if 0 <= cx < w and 0 <= cy < h:
        led["cx"], led["cy"] = cx, cy
        gemini_status[0] = f"Manual: LED moved to ({cx},{cy})"

def on_scroll(event):
    if mode[0] != "calibrate": return
    delta = 1 if event.delta > 0 or event.num == 4 else -1
    led["r"] = max(2, min(50, led["r"] + delta))

canvas.bind("<Button-1>", on_click)
canvas.bind("<MouseWheel>", on_scroll)
canvas.bind("<Button-4>", on_scroll)
canvas.bind("<Button-5>", on_scroll)

# ── Keys ──────────────────────────────────────────────────────────────────────
def on_key(event):
    k = event.keysym.lower()
    if k == "escape":
        root.destroy()
    elif k == "space":
        mode[0] = "overlay"
        canvas.coords(lbl_state, 10, 10)
    elif k == "c":
        mode[0] = "calibrate"
        canvas.coords(lbl_state, 20, 115)
    elif k == "r":
        threading.Thread(target=run_gemini, daemon=True).start()
    elif k == "t":
        threading.Thread(target=auto_threshold, daemon=True).start()
        gemini_status[0] = "Auto-threshold: collecting 3s..."

root.bind("<Key>", on_key)

# ── Boot ──────────────────────────────────────────────────────────────────────
# Auto-run Gemini on start
root.after(2000, lambda: threading.Thread(target=run_gemini, daemon=True).start())
root.after(16, update)

print(f"Calibration viewer started on DISPLAY={os.environ.get('DISPLAY','?')}")
print("Keys: SPACE=overlay  R=Gemini  T=threshold  click=move LED  scroll=resize ROI  C=calibrate  ESC=quit")
root.mainloop()
