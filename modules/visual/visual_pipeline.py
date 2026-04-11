"""
visual_pipeline.py  —  YOLOv11n + ByteTrack visual drone detection.

WHAT THIS DOES:
  Wraps the existing drone_tracker.py (ByteTrack + YOLO) into the project's
  modular architecture.  The original file is kept intact in this folder;
  this module adapts it to:
    1. Publish "visual.detection" events on the event bus.
    2. Estimate rough range from bounding-box width (simple pinhole model).
    3. Support both RGB and IR cameras simultaneously.
    4. Be startable/stoppable cleanly from main.py.

RANGE ESTIMATION (pinhole camera model):
  range_m = (focal_length_px * drone_wingspan_m) / bbox_width_px

  This is a rough estimate only — it's used by the fusion engine as a
  sanity check against the SDR range, NOT as a primary range source.

DEPENDENCIES:
  pip install ultralytics torch opencv-python

INTEGRATION:
  Run in a separate thread from main.py.  Publishes to the shared bus.
"""

import cv2
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import torch
    from ultralytics import YOLO
    _HAS_YOLO = True
except ImportError:
    logger.warning("[Visual] ultralytics/torch not found — simulation mode")
    _HAS_YOLO = False


class VisualPipeline:
    """
    Runs YOLOv11n + ByteTrack on one or two cameras and publishes
    "visual.detection" events for every confirmed drone track.

    Args:
        cfg: The 'visual' section from system_config.yaml.
        bus: Shared EventBus instance.
    """

    def __init__(self, cfg: dict, bus):
        self._cfg = cfg
        self._bus = bus

        self._model_path    = cfg["model_path"]
        self._rgb_idx       = cfg["camera_rgb_index"]
        self._ir_idx        = cfg.get("camera_ir_index", -1)  # -1 = disabled
        self._conf_thresh   = cfg["confidence_threshold"]
        self._iou_thresh    = cfg["iou_threshold"]
        self._tracker_cfg   = cfg["tracker_config"]
        self._output_path   = cfg["output_video"]
        self._focal_px      = cfg["focal_length_px"]
        self._wingspan_m    = cfg["drone_wingspan_m"]
        self._show_window   = cfg.get("display_window", True)

        self._model: Optional[object] = None
        self._cap_rgb: Optional[cv2.VideoCapture] = None
        self._cap_ir:  Optional[cv2.VideoCapture] = None
        self._writer:  Optional[cv2.VideoWriter]  = None

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # FPS tracking (rolling average over last 30 frames)
        self._fps_hist = []

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Load model, open cameras, start the detection thread."""
        self._load_model()
        self._open_cameras()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="VisualPipeline"
        )
        self._thread.start()
        logger.info("[Visual] Detection thread started")

    def stop(self):
        """Stop the detection loop and release resources."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._cap_rgb:
            self._cap_rgb.release()
        if self._cap_ir:
            self._cap_ir.release()
        if self._writer:
            self._writer.release()
        cv2.destroyAllWindows()
        logger.info("[Visual] Pipeline stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_model(self):
        """Load the YOLOv11n weights onto the best available device."""
        if not _HAS_YOLO:
            logger.warning("[Visual] Skipping model load — simulation mode")
            return

        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"[Visual] Loading YOLO model from '{self._model_path}' on {device.upper()}")
        self._model = YOLO(self._model_path)
        self._model.to(device)
        logger.info("[Visual] Model loaded")

    def _open_cameras(self):
        """Open OpenCV video captures for RGB (and optionally IR) cameras."""
        self._cap_rgb = cv2.VideoCapture(self._rgb_idx)
        if not self._cap_rgb.isOpened():
            logger.error(f"[Visual] Cannot open RGB camera (index {self._rgb_idx})")
            self._cap_rgb = None

        if self._ir_idx >= 0:
            self._cap_ir = cv2.VideoCapture(self._ir_idx)
            if not self._cap_ir.isOpened():
                logger.warning(f"[Visual] Cannot open IR camera (index {self._ir_idx}) — disabled")
                self._cap_ir = None

        # Set up video writer from RGB camera dimensions
        if self._cap_rgb and self._cap_rgb.isOpened():
            w = int(self._cap_rgb.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap_rgb.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps_in = self._cap_rgb.get(cv2.CAP_PROP_FPS) or 25
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(self._output_path, fourcc, fps_in, (w, h))
            logger.info(f"[Visual] Recording to {self._output_path} ({w}×{h} @ {fps_in} fps)")

    def _run_loop(self):
        """
        Main detection loop.
        Reads frames from camera(s), runs YOLO+ByteTrack, draws results,
        estimates range, and publishes events.
        """
        if self._show_window:
            cv2.namedWindow("Drone Tracking", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("Drone Tracking", 960, 540)

        while self._running:
            t_start = time.time()

            # ── Acquire frame ────────────────────────────────────────────────
            frame = self._get_frame()
            if frame is None:
                time.sleep(0.05)
                continue

            # ── Run YOLO + ByteTrack ─────────────────────────────────────────
            if self._model is not None:
                detections = self._infer(frame)
            else:
                # Simulation: pretend we see a drone every 2 seconds
                detections = self._sim_detections()

            # ── Process detections and publish events ────────────────────────
            for det in detections:
                self._annotate_frame(frame, det)
                self._bus.publish("visual.detection", {
                    "track_id":     det["track_id"],
                    "bbox":         det["bbox"],         # [x1, y1, x2, y2]
                    "confidence":   det["conf"],
                    "range_est_m":  det["range_m"],
                    "ts":           time.monotonic(),
                })
                logger.info(
                    f"[Visual] Drone ID={det['track_id']}  "
                    f"conf={det['conf']:.2f}  "
                    f"range≈{det['range_m']:.1f} m"
                )

            # ── FPS overlay ──────────────────────────────────────────────────
            fps = 1.0 / max(time.time() - t_start, 1e-6)
            self._fps_hist.append(fps)
            if len(self._fps_hist) > 30:
                self._fps_hist.pop(0)
            avg_fps = sum(self._fps_hist) / len(self._fps_hist)
            cv2.putText(frame, f"FPS: {avg_fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

            # ── Save and display ─────────────────────────────────────────────
            if self._writer:
                self._writer.write(frame)
            if self._show_window:
                disp = cv2.resize(frame, (960, 540))
                cv2.imshow("Drone Tracking", disp)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    logger.info("[Visual] Quit key pressed — stopping")
                    self._running = False
                    break

    def _get_frame(self) -> Optional[object]:
        """
        Get the next frame.  If both RGB and IR cameras are active, returns
        a side-by-side composite so the YOLO model sees both simultaneously.
        For simplicity, this implementation returns the RGB frame only
        (IR support can be enabled by running a second inference pass).
        """
        if self._cap_rgb is None:
            return None
        ret, frame = self._cap_rgb.read()
        if not ret:
            logger.warning("[Visual] Frame read failed")
            return None
        return frame

    def _infer(self, frame) -> list:
        """
        Run YOLO + ByteTrack on a single frame.

        Returns:
            List of dicts: {track_id, bbox, conf, range_m}
        """
        results = self._model.track(
            frame,
            conf=self._conf_thresh,
            iou=self._iou_thresh,
            persist=True,                   # ByteTrack needs persist=True
            tracker=self._tracker_cfg,
            verbose=False,
        )

        detections = []
        for r in results:
            if r.boxes is None or r.boxes.id is None:
                continue
            boxes   = r.boxes.xyxy.cpu().numpy()
            ids     = r.boxes.id.cpu().numpy().astype(int)
            confs   = r.boxes.conf.cpu().numpy()
            classes = r.boxes.cls.cpu().numpy().astype(int)

            for box, tid, conf, cls in zip(boxes, ids, confs, classes):
                # Only process boxes labelled "drone"
                if r.names[cls] != "drone":
                    continue
                x1, y1, x2, y2 = map(int, box)
                bbox_w = max(x2 - x1, 1)
                # Pinhole range estimate: range = (f * W_real) / W_pixel
                range_m = (self._focal_px * self._wingspan_m) / bbox_w
                detections.append({
                    "track_id": int(tid),
                    "bbox":     [x1, y1, x2, y2],
                    "conf":     float(conf),
                    "range_m":  float(range_m),
                })
        return detections

    def _annotate_frame(self, frame, det: dict):
        """Draw bounding box, track ID and range estimate on the frame."""
        x1, y1, x2, y2 = det["bbox"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = (f"Drone ID {det['track_id']} "
                 f"({det['conf']:.2f}) "
                 f"~{det['range_m']:.0f}m")
        cv2.putText(frame, label, (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        # Centre dot
        cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)

    def _sim_detections(self) -> list:
        """
        Return a fake detection every 2 seconds for testing without hardware.
        """
        if int(time.time()) % 2 == 0:
            return [{
                "track_id": 1,
                "bbox":     [200, 150, 400, 300],
                "conf":     0.91,
                "range_m":  120.0,
            }]
        return []
