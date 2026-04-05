# Latency Reduction Ideas

This document lists concrete ideas to reduce end-to-end camera pipeline latency,
from sensor to display, ordered roughly by expected impact.

---

## 🔴 High impact

### 1. Replace Python MJPEG server with GStreamer TCP sink
**Current:** Python HTTP server reads GStreamer stdout → buffers → sends MJPEG  
**Better:** `tcpserversink` directly in GStreamer pipeline  
**Expected gain:** 50–150ms (eliminates Python HTTP buffering)  
**Status:** ✅ Implemented (`jetson/gst_cam0_tcp.sh`)

### 2. Replace browser/VLC viewer with native tkinter overlay
**Current:** Browser fetches MJPEG → decodes → renders (multiple buffer layers)  
**Better:** Python tkinter reads TCP directly → LED detect → flip canvas image  
**Expected gain:** 50–100ms  
**Status:** ✅ Implemented (`laptop/overlay.py`)

### 3. Reduce camera exposure time
**Current:** 15ms exposure  
**Better:** 5–8ms (bright LED, short exposure = faster sensor readout)  
**Tradeoff:** Darker image, need higher gain  
**Expected gain:** 5–15ms  
**Status:** Partially tested (gain artifacts at high values)

### 4. Use DRM/KMS direct framebuffer instead of X11/tkinter
**Current:** tkinter → X11 → compositor → HDMI  
**Better:** Write directly to `/dev/dri/card0` via `pydrm` or `drm` Python bindings  
**Expected gain:** 10–30ms (eliminates X11 compositing pipeline)  
**Implementation:**
```python
import drm
# or: subprocess mplayer -vo drm / mpv --vo=drm
```

### 5. GPU-accelerated JPEG decode on laptop
**Current:** `cv2.imdecode` (CPU)  
**Better:** `nvdec` via GStreamer on Jetson-side, or `turbojpeg` (libjpeg-turbo) on laptop  
**Expected gain:** 2–5ms per frame at 60fps  
**Implementation:**
```python
import turbojpeg
tj = turbojpeg.TurboJPEG()
arr = tj.decode(frame_bytes)
```

---

## 🟡 Medium impact

### 6. Move LED detection to Jetson (reduce what travels over network)
**Current:** Raw JPEG → laptop → decode → detect → draw  
**Better:** Jetson detects LED → sends 1 byte (0/1) → laptop draws  
**Expected gain:** Eliminates JPEG decode + most network bandwidth  
**Tradeoff:** Adds Jetson CPU load; less flexible  
**Implementation:** Small Python script on Jetson, sends `b'\x01'` or `b'\x00'` per frame

### 7. Reduce JPEG quality / resolution further
**Current:** 640×480 @ quality=70  
**Better:** 320×240 @ quality=50 (LED detection only needs enough pixels to see the LED)  
**Expected gain:** 2–5ms decode, ~30% less network bandwidth  
**Note:** Only viable if LED stays clearly visible at lower resolution

### 8. Use `SO_PRIORITY` / `IPTOS_LOWDELAY` on TCP socket
**Current:** Default TCP socket  
**Better:** Set `IP_TOS = IPTOS_LOWDELAY` on socket  
**Expected gain:** 1–5ms on congested networks  
**Implementation:**
```python
import socket
sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # IPTOS_LOWDELAY
```

### 9. Disable Nagle algorithm on TCP socket
**Current:** TCP may buffer small packets  
**Better:** `TCP_NODELAY = 1`  
**Expected gain:** 0–20ms (relevant if frames are small)  
**Implementation:**
```python
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
```

### 10. Use UDP instead of TCP for streaming
**Current:** TCP (retransmits add latency on packet loss)  
**Better:** UDP with sequence numbers, drop stale frames  
**Expected gain:** 0–30ms depending on network quality  
**Note:** Only worth it on lossy networks

---

## 🟢 Low impact / future ideas

### 11. Predictive display update
**Idea:** Measure LED blink period precisely → predict next ON/OFF → pre-flip display  
**Complexity:** High (requires stable ESP32 timing)  
**Expected gain:** Up to 8ms (one frame early)

### 12. Hardware GPIO trigger instead of camera-based detection
**Idea:** Wire ESP32 GPIO to Jetson GPIO → interrupt-driven detection (microsecond precision)  
**Tradeoff:** Bypasses camera latency (defeats the purpose of measuring it)  
**Use case:** Isolating specific pipeline stages

### 13. Dedicated display thread with `vsync` lock
**Idea:** Sync display flip to monitor vsync to avoid tearing and reduce display latency  
**Implementation:** Use `glfw` or `pygame` with vsync instead of tkinter

### 14. Replace Gemini with local YOLO-nano LED detector
**Idea:** Train a tiny YOLO-nano model to detect LED in <1ms locally  
**Use case:** Robust LED detection without API calls or manual calibration  
**Data needed:** ~100 frames with LED on/off labels

### 15. Use hardware encoder on Jetson (already done) + hardware decoder on laptop
**Current:** Jetson uses `nvjpegenc` (HW) ✅, laptop uses `cv2.imdecode` (CPU)  
**Better:** Use `nvjpegdec` on Jetson-class hardware, or `turbojpeg` on x86  

---

## Measurement setup improvements

- **Precise timestamp sync:** Use PTP/IEEE 1588 between Jetson and laptop for sub-ms clock sync
- **Higher fps cam1:** 120fps cam1 = 8ms resolution instead of 33ms
- **Log raw frame timestamps:** Add frame counter + timestamp to JPEG EXIF/comment field on Jetson

---

## Current bottleneck estimate

| Stage | Estimated latency |
|---|---|
| LED → cam0 sensor | 8–15ms (exposure) |
| ISP + nvjpegenc | 2–5ms |
| GStreamer pipeline | 1–3ms |
| TCP network | 0.5–2ms |
| Python recv + cv2 decode | 2–5ms |
| LED detection (numpy) | <1ms |
| tkinter canvas update | 1–5ms |
| X11 → compositor → HDMI | 5–20ms |
| Monitor response time | 1–5ms |
| cam1 sensor exposure | 8ms |
| cam1 MJPEG decode | 2ms |
| **Total estimate** | **~30–70ms** |
| **Measured (MJPEG HTTP)** | **76–258ms** (extra: HTTP buffering) |
