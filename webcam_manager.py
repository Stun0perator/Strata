import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Generator

logger = logging.getLogger("strata.webcam")

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    logger.warning("opencv-python not available — webcam features disabled")


RESOLUTIONS = {
    "480p": (640, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


class WebcamManager:
    def __init__(self, recording_dir: str = "/home/pi/recordings"):
        self._cap: Optional[object] = None
        self._recording = False
        self._writer: Optional[object] = None
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._running = False
        self._resolution = "720p"
        self._recording_dir = recording_dir
        self._recording_filename = ""
        self._recording_start: Optional[float] = None
        self._overlay_text = ""
        os.makedirs(recording_dir, exist_ok=True)

    @property
    def is_available(self) -> bool:
        return HAS_CV2

    @property
    def is_streaming(self) -> bool:
        return self._running and self._cap is not None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, device: int = 0, resolution: str = "720p") -> bool:
        if not HAS_CV2:
            return False
        with self._lock:
            if self._running:
                return True
            self._resolution = resolution
            w, h = RESOLUTIONS.get(resolution, (1280, 720))
            self._cap = cv2.VideoCapture(device)
            if not self._cap.isOpened():
                logger.error("Failed to open webcam device %d", device)
                self._cap = None
                return False
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            self._running = True

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="webcam-capture"
        )
        self._capture_thread.start()
        logger.info("Webcam started at %s", resolution)
        return True

    def stop(self):
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
        self.stop_recording()
        with self._lock:
            if self._cap:
                self._cap.release()
                self._cap = None
        logger.info("Webcam stopped")

    def set_resolution(self, resolution: str):
        if self._running:
            device = 0
            self.stop()
            self.start(device, resolution)
        else:
            self._resolution = resolution

    def set_overlay(self, text: str):
        self._overlay_text = text

    def _capture_loop(self):
        while self._running:
            with self._lock:
                if self._cap is None:
                    break
                ret, frame = self._cap.read()

            if not ret:
                time.sleep(0.01)
                continue

            if self._overlay_text:
                cv2.putText(
                    frame, self._overlay_text,
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2,
                )

            with self._lock:
                if self._recording and self._writer:
                    self._writer.write(frame)

            _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self._frame = jpeg.tobytes()
            time.sleep(0.033)  # ~30fps cap

    def get_frame(self) -> Optional[bytes]:
        return self._frame

    def generate_mjpeg(self) -> Generator[bytes, None, None]:
        # Wait briefly for the capture thread to produce the first frame so
        # the browser does not hang on an empty multipart stream.
        deadline = time.time() + 3.0
        while self._running and self._frame is None and time.time() < deadline:
            time.sleep(0.02)
        while self._running:
            frame = self._frame
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            time.sleep(0.033)

    def start_recording(self) -> str:
        if not HAS_CV2 or not self._running:
            return ""
        w, h = RESOLUTIONS.get(self._resolution, (1280, 720))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"plot_recording_{ts}.mp4"
        filepath = os.path.join(self._recording_dir, filename)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        with self._lock:
            self._writer = cv2.VideoWriter(filepath, fourcc, 30.0, (w, h))
            self._recording = True
            self._recording_filename = filename
            self._recording_start = time.time()
        logger.info("Recording started: %s", filename)
        return filename

    def stop_recording(self) -> dict:
        info = {}
        with self._lock:
            if self._writer:
                self._writer.release()
                self._writer = None
            if self._recording:
                duration = time.time() - self._recording_start if self._recording_start else 0
                info = {
                    "filename": self._recording_filename,
                    "duration_seconds": round(duration, 1),
                }
            self._recording = False
            self._recording_start = None
        if info:
            logger.info("Recording stopped: %s (%.1fs)", info["filename"], info["duration_seconds"])
        return info

    def list_recordings(self) -> list[dict]:
        recordings = []
        rec_dir = Path(self._recording_dir)
        if rec_dir.exists():
            for f in sorted(rec_dir.glob("*.mp4"), reverse=True):
                stat = f.stat()
                recordings.append({
                    "filename": f.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 1),
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                })
        return recordings
