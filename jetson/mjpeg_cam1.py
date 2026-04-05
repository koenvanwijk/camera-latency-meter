#!/usr/bin/env python3
"""Robuuste MJPEG cam1 stream met auto-restart en watchdog."""
import subprocess, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

latest_frame = b""
frame_lock = threading.Lock()
frame_count = 0
last_frame_time = time.time()

def capture_loop():
    global latest_frame, frame_count, last_frame_time
    while True:
        try:
            print("GStreamer start...", flush=True)
            proc = subprocess.Popen([
                "gst-launch-1.0", "-q",
                "nvarguscamerasrc", "sensor-id=1",
                "gainrange=4 4",
                "ispdigitalgainrange=1 1",
                "exposuretimerange=8000000 8000000",
                "!", "video/x-raw(memory:NVMM),width=1280,height=720,framerate=60/1",
                "!", "nvjpegenc", "quality=80",
                "!", "fdsink", "fd=1"
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            while True:
                chunk = proc.stdout.read(65536)  # groot chunk
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        buf = buf[start:]
                        break
                    frame = buf[start:end+2]
                    buf = buf[end+2:]
                    with frame_lock:
                        latest_frame = frame
                        frame_count += 1
                        last_frame_time = time.time()
            proc.wait()
            print("GStreamer gestopt, herstart in 1s...", flush=True)
        except Exception as e:
            print(f"Fout: {e}", flush=True)
        time.sleep(1)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = -1
            try:
                while True:
                    with frame_lock:
                        f = latest_frame
                        fc = frame_count
                    if fc != last and f:
                        last = fc
                        self.wfile.write(
                            b"--frame\r\nContent-Type:image/jpeg\r\n\r\n" + f + b"\r\n"
                        )
                        self.wfile.flush()
                    else:
                        time.sleep(0.005)
            except:
                pass
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            with frame_lock:
                age = time.time() - last_frame_time
            self.wfile.write(f"frames={frame_count} age={age:.1f}s".encode())
        else:
            self.send_response(404)
            self.end_headers()

print("Cam1 MJPEG server poort 8091", flush=True)
threading.Thread(target=capture_loop, daemon=True).start()
HTTPServer(("0.0.0.0", 8091), Handler).serve_forever()
