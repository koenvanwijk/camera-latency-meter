#!/bin/bash
# Start cam0 raw TCP stream on port 5001
# GStreamer pipeline: nvarguscamerasrc → nvjpegenc → tcpserversink
# No Python overhead — direct hardware encode to TCP

GAIN="${GAIN:-1}"
ISP_GAIN="${ISP_GAIN:-1}"
EXPOSURE_US="${EXPOSURE_US:-15000000}"  # 15ms
WIDTH="${WIDTH:-640}"
HEIGHT="${HEIGHT:-480}"
FPS="${FPS:-60}"
QUALITY="${QUALITY:-70}"
PORT="${PORT:-5001}"

echo "Starting cam0 TCP stream: ${WIDTH}x${HEIGHT} @ ${FPS}fps exposure=${EXPOSURE_US}us port=${PORT}"

exec gst-launch-1.0 -q \
  nvarguscamerasrc sensor-id=0 \
    gainrange="${GAIN} ${GAIN}" \
    ispdigitalgainrange="${ISP_GAIN} ${ISP_GAIN}" \
    exposuretimerange="${EXPOSURE_US} ${EXPOSURE_US}" \
  ! "video/x-raw(memory:NVMM),width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1" \
  ! nvjpegenc quality=${QUALITY} \
  ! multipartmux boundary=frame \
  ! tcpserversink host=0.0.0.0 port=${PORT} sync=false recover-policy=keyframe
