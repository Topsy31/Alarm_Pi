"""
O-KAM Camera - RTSP stream integration for O-KAM Pro cameras.

O-KAM cameras (Vstarcam/SZSINOCAM) expose an RTSP video stream on the
local network. This module provides:

  - Auto-discovery of the camera's IP via network scanning
  - RTSP stream connection and frame capture
  - Threaded background streaming (non-blocking)
  - Snapshot capture and saving
  - Motion detection (basic frame differencing)
  - Integration hooks for the AGSHome alarm system

RTSP URL format for O-KAM cameras:
  Main stream:  rtsp://<ip>:10555/TCP/av0_0
  Sub stream:   rtsp://<ip>:10555/TCP/av0_1

Some models may use different ports (554, 8554) or require credentials.
Use discover_camera.py to probe your specific camera.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning(
        "OpenCV (cv2) not installed. Install with: pip install opencv-python"
    )

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False


@dataclass
class CameraConfig:
    """Configuration for an O-KAM camera."""
    name: str = "O-KAM Camera"
    ip_address: str = ""
    rtsp_port: int = 10555
    username: str = ""          # Often blank for O-KAM
    password: str = ""          # Often blank for O-KAM
    stream_path: str = "TCP/av0_0"   # Main stream
    sub_stream_path: str = "TCP/av0_1"  # Lower resolution
    use_sub_stream: bool = False  # Use sub stream for lower bandwidth
    connection_timeout: int = 10
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 5

    @property
    def rtsp_url(self) -> str:
        """Build the full RTSP URL."""
        path = self.sub_stream_path if self.use_sub_stream else self.stream_path
        if self.username and self.password:
            return f"rtsp://{self.username}:{self.password}@{self.ip_address}:{self.rtsp_port}/{path}"
        return f"rtsp://{self.ip_address}:{self.rtsp_port}/{path}"


# Common RTSP URL patterns to try during discovery
RTSP_URL_PATTERNS = [
    # O-KAM / Vstarcam standard
    {"port": 10555, "path": "TCP/av0_0", "desc": "O-KAM main stream (port 10555)"},
    {"port": 10555, "path": "TCP/av0_1", "desc": "O-KAM sub stream (port 10555)"},
    # Standard RTSP port
    {"port": 554, "path": "stream1", "desc": "Standard RTSP (port 554)"},
    {"port": 554, "path": "", "desc": "Standard RTSP root (port 554)"},
    # Alternative ports seen on some models
    {"port": 8554, "path": "profile0", "desc": "Alt RTSP (port 8554)"},
    {"port": 8554, "path": "profile1", "desc": "Alt RTSP sub (port 8554)"},
]


class OKamCamera:
    """
    Interface to an O-KAM camera via RTSP on the local network.

    Usage:
        camera = OKamCamera(CameraConfig(ip_address="192.168.1.100"))
        camera.connect()
        frame = camera.snapshot()
        camera.start_stream()  # Background streaming thread
        ...
        camera.stop_stream()
        camera.disconnect()
    """

    def __init__(self, config: CameraConfig):
        self.config = config
        self._capture: Optional[Any] = None  # cv2.VideoCapture
        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._frame_count = 0
        self._motion_listeners: list[Callable] = []
        self._previous_frame_gray = None

    def connect(self) -> bool:
        """
        Connect to the camera's RTSP stream.

        Returns True on success.
        """
        if not CV2_AVAILABLE:
            logger.error("OpenCV not installed. Run: pip install opencv-python")
            return False

        url = self.config.rtsp_url
        logger.info(f"Connecting to camera at: {url}")

        # Set FFMPEG options for better RTSP handling
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.config.connection_timeout * 1000)
        capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000)

        if not capture.isOpened():
            logger.error(f"Failed to open RTSP stream at {url}")
            return False

        # Try reading a test frame
        ret, frame = capture.read()
        if not ret or frame is None:
            logger.error("Connected but could not read a frame")
            capture.release()
            return False

        self._capture = capture
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)

        logger.info(
            f"Connected! Resolution: {width}x{height}, FPS: {fps:.1f}"
        )
        return True

    def disconnect(self):
        """Release the camera connection."""
        self.stop_stream()
        if self._capture:
            self._capture.release()
            self._capture = None
            logger.info("Camera disconnected.")

    def is_connected(self) -> bool:
        """Check if the camera connection is alive."""
        return self._capture is not None and self._capture.isOpened()

    # --- Frame Capture ---

    def read_frame(self):
        """
        Read a single frame from the camera.

        Returns a numpy array (BGR image) or None on failure.
        """
        if not self.is_connected():
            return None

        ret, frame = self._capture.read()
        if ret and frame is not None:
            self._frame_count += 1
            with self._frame_lock:
                self._latest_frame = frame.copy()
            return frame
        return None

    def snapshot(self, save_path: str = None) -> Optional[str]:
        """
        Take a snapshot and optionally save to disk.

        Args:
            save_path: File path to save the image (e.g. "snapshot.jpg").
                       If None, generates a timestamped filename.

        Returns the file path of the saved image, or None on failure.
        """
        frame = self.read_frame()
        if frame is None:
            logger.error("Could not capture snapshot")
            return None

        if save_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = f"snapshot_{timestamp}.jpg"

        cv2.imwrite(save_path, frame)
        logger.info(f"Snapshot saved: {save_path}")
        return save_path

    def get_latest_frame(self):
        """Get the most recent frame (thread-safe, for use with background streaming)."""
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    # --- Background Streaming ---

    def start_stream(self, display: bool = False, fps_limit: float = 15.0):
        """
        Start background streaming thread.

        Args:
            display: If True, show a live video window (requires display).
            fps_limit: Max frames per second to process (saves CPU).
        """
        if self._streaming:
            logger.warning("Already streaming")
            return

        self._streaming = True
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            args=(display, fps_limit),
            daemon=True,
        )
        self._stream_thread.start()
        logger.info("Background streaming started")

    def stop_stream(self):
        """Stop the background streaming thread."""
        if self._streaming:
            self._streaming = False
            if self._stream_thread:
                self._stream_thread.join(timeout=5)
                self._stream_thread = None
            logger.info("Background streaming stopped")

    def _stream_loop(self, display: bool, fps_limit: float):
        """Internal streaming loop (runs in background thread)."""
        frame_interval = 1.0 / fps_limit if fps_limit > 0 else 0
        reconnect_attempts = 0

        while self._streaming:
            start_time = time.time()

            frame = self.read_frame()
            if frame is None:
                # Connection lost - attempt reconnect
                reconnect_attempts += 1
                if reconnect_attempts > self.config.max_reconnect_attempts:
                    logger.error("Max reconnect attempts reached. Stopping.")
                    self._streaming = False
                    break

                logger.warning(
                    f"Frame read failed. Reconnecting "
                    f"({reconnect_attempts}/{self.config.max_reconnect_attempts})..."
                )
                time.sleep(self.config.reconnect_delay)

                if self._capture:
                    self._capture.release()
                self._capture = cv2.VideoCapture(
                    self.config.rtsp_url, cv2.CAP_FFMPEG
                )
                continue

            reconnect_attempts = 0

            # Check for motion
            self._check_motion(frame)

            # Display window (if requested and on main thread... careful here)
            if display:
                cv2.imshow(self.config.name, frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    self._streaming = False
                    break

            # FPS limiting
            elapsed = time.time() - start_time
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

        if display:
            cv2.destroyWindow(self.config.name)

    # --- Motion Detection ---

    def add_motion_listener(self, callback: Callable[[float, Any], None]):
        """
        Register a callback for motion events.

        Callback signature: callback(motion_score, frame)
        motion_score is a float 0-100 indicating intensity of motion.
        """
        self._motion_listeners.append(callback)

    def _check_motion(self, frame, threshold: float = 5.0):
        """
        Simple motion detection via frame differencing.

        Compares the current frame to the previous one and calculates
        the mean absolute difference. If above threshold, fires listeners.
        """
        if not NUMPY_AVAILABLE or not self._motion_listeners:
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self._previous_frame_gray is None:
            self._previous_frame_gray = gray
            return

        diff = cv2.absdiff(self._previous_frame_gray, gray)
        motion_score = float(np.mean(diff))

        if motion_score > threshold:
            for listener in self._motion_listeners:
                try:
                    listener(motion_score, frame)
                except Exception as e:
                    logger.error(f"Motion listener error: {e}")

        self._previous_frame_gray = gray

    # --- Utilities ---

    @property
    def frame_count(self) -> int:
        """Total frames captured since connection."""
        return self._frame_count

    def __repr__(self) -> str:
        status = "connected" if self.is_connected() else "disconnected"
        streaming = ", streaming" if self._streaming else ""
        return (
            f"OKamCamera({self.config.name}, "
            f"ip={self.config.ip_address}, {status}{streaming})"
        )

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


def probe_camera_rtsp(ip_address: str, timeout: int = 5) -> Optional[dict]:
    """
    Probe a camera IP to find a working RTSP URL.

    Tries common URL patterns and returns the first one that works.
    Returns a dict with port, path, and full URL, or None if nothing works.
    """
    if not CV2_AVAILABLE:
        logger.error("OpenCV required for camera probing")
        return None

    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    for pattern in RTSP_URL_PATTERNS:
        url = f"rtsp://{ip_address}:{pattern['port']}/{pattern['path']}"
        logger.info(f"Trying: {pattern['desc']} -> {url}")

        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout * 1000)

            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    logger.info(f"SUCCESS: {pattern['desc']}")
                    return {
                        "port": pattern["port"],
                        "path": pattern["path"],
                        "url": url,
                        "description": pattern["desc"],
                    }
            else:
                cap.release()
        except Exception as e:
            logger.debug(f"Failed: {e}")

    return None
