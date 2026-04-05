# Camera Latency Meter

End-to-end camera pipeline latency measurement tool for robotics/teleop setups.

## What it measures

```
ESP32 LED blinks → cam0 detects LED → overlay on screen → cam1 sees screen
└─────────────────── full end-to-end latency ───────────────────────────────┘
```

The full pipeline includes:
- Camera sensor capture latency
- ISP processing
- JPEG encoding
- Network transfer
- LED detection (Python/OpenCV)
- Display rendering (tkinter)
- Physical screen response time
- cam1 capture of the screen

## Hardware

- **Jetson Orin** (or any Linux box with CSI cameras)
- **cam0** — sees the ESP32 LED
- **cam1** — sees both the LED and the display
- **ESP32** — blinks an LED at a known interval
- **External display** — connected to measurement laptop

## Setup

```
[ESP32 LED] ──────────────────────────────────────────────┐
     │                                                      │
     └──→ cam0 ──→ Jetson ──→ TCP stream ──→ laptop        │
                                                ↓           │
                                           overlay.py       │
                                                ↓           │
                                          [HDMI screen]     │
                                                │           │
                                                └──→ cam1 ──┘
                                                      │
                                               latency_measure.py
```

## Quick start

### 1. Flash the ESP32

See `esp32/blink_led/` — blinks LED on GPIO 2, 500ms ON / 2000ms OFF.

### 2. Start Jetson streams

```bash
# cam0: raw TCP stream (60fps, low latency)
gst-launch-1.0 nvarguscamerasrc sensor-id=0 \
  gainrange="1 1" ispdigitalgainrange="1 1" exposuretimerange="15000000 15000000" \
  ! "video/x-raw(memory:NVMM),width=640,height=480,framerate=60/1" \
  ! nvjpegenc quality=70 ! multipartmux boundary=frame \
  ! tcpserversink host=0.0.0.0 port=5001 sync=false

# cam1: MJPEG HTTP stream
python3 jetson/mjpeg_cam1.py
```

### 3. Run the overlay + measurement

```bash
# On laptop (with external display at offset 0,0)
DISPLAY=:1 python3 laptop/overlay.py

# Measure latency
python3 laptop/latency_measure.py
```

## Results (initial measurements)

| Pipeline | Avg | Min | Max |
|---|---|---|---|
| MJPEG HTTP (Python server) | 170ms | 76ms | 258ms |
| TCP direct (GStreamer) | ~30ms | TBD | TBD |

## Ideas to reduce latency

See [LATENCY_IDEAS.md](LATENCY_IDEAS.md).

## Requirements

```
pip install numpy pillow opencv-python-headless
```

Jetson: GStreamer with nvarguscamerasrc, nvjpegenc (standard on JetPack).
