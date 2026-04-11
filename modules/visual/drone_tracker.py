import cv2
import time
import torch
from ultralytics import YOLO


class DroneDetectionSystem:
    def __init__(self, model_name, video_source=0, output_path="tracked_output.mp4"):
        print("Initializing video source...")
        self.cap = cv2.VideoCapture(video_source)

        if not self.cap.isOpened():
            raise RuntimeError("Could not open video source")

        self.model = YOLO(model_name)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)

        self.fps = 0
        self.fps_hist = []

        # -------- OUTPUT VIDEO SETUP --------
        self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.input_fps = self.cap.get(cv2.CAP_PROP_FPS)
        if self.input_fps == 0:
            self.input_fps = 25  # fallback

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(
            output_path,
            fourcc,
            self.input_fps,
            (self.frame_width, self.frame_height)
        )
        # -----------------------------------

        print(f"Running on {self.device.upper()}")
        print(f"Saving output to: {output_path}")

    def run(self):
        print("Starting YOLO + ByteTrack tracking (press 'q' to quit)\n")

        # -------- DISPLAY WINDOW --------
        window_name = "Drone Tracking - ByteTrack"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 540)
        # --------------------------------

        while True:
            start = time.time()
            ret, frame = self.cap.read()

            if not ret:
                print("End of video / stream")
                break

            # ---- YOLO + ByteTrack ----
            results = self.model.track(
                frame,
                conf=0.3,
                iou=0.45,
                persist=True,
                tracker="bytetrack.yaml",
                verbose=False
            )

            # ---- Draw detections ----
            for r in results:
                if r.boxes is None or r.boxes.id is None:
                    continue

                boxes = r.boxes.xyxy.cpu().numpy()
                ids = r.boxes.id.cpu().numpy().astype(int)
                confs = r.boxes.conf.cpu().numpy()
                classes = r.boxes.cls.cpu().numpy().astype(int)

                for box, track_id, conf, cls in zip(boxes, ids, confs, classes):
                    class_name = r.names[cls]
                    if class_name != "drone":
                        continue

                    x1, y1, x2, y2 = map(int, box)
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

                    label = f"Drone ID {track_id} ({conf:.2f})"
                    cv2.putText(
                        frame, label, (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 0), 2
                    )

                    cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)

            # ---- FPS ----
            fps = 1 / (time.time() - start)
            self.fps_hist.append(fps)
            if len(self.fps_hist) > 30:
                self.fps_hist.pop(0)
            self.fps = sum(self.fps_hist) / len(self.fps_hist)

            cv2.putText(
                frame, f"FPS: {self.fps:.1f}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 255, 0), 2
            )

            # -------- SAVE FULL-RES FRAME --------
            self.writer.write(frame)

            # -------- DISPLAY RESIZED --------
            display = cv2.resize(frame, (960, 540))
            cv2.imshow(window_name, display)
            # --------------------------------

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        self.cap.release()
        self.writer.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    system = DroneDetectionSystem(
        model_name="A:\Downloads\anti_drone_system (1)\anti_drone_system\models\weights\best.pt",
        video_source="multi_drone2.mp4",
        output_path="tracked_output.mp4"
    )
    system.run()
