"""The camera process: reads the webcam, finds hands with MediaPipe and sends Rafiq only the gesture
labels, never an image. Rafiq starts it (`python -m rafiq.tracker --post URL`, the token in
RAFIQ_GESTURE_TOKEN); `rafiq gestures demo` and `rafiq gestures calibrate` open it in a window that
shows the camera with the hand landmarks.

Needs `pip install rafiq[gestures]` (opencv-python, mediapipe) and a webcam.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Iterable

from .hands import Hand, Recognizer

WINDOW = "Rafiq hand gestures"


def track(images: Iterable, detect: Callable[[object], list[Hand]], send: Callable[[str], None],
          recognizer: Recognizer, clock: Callable[[], float] = time.monotonic) -> None:
    """For each camera image, the hands in it go through the recognizer; a finished gesture's label is sent."""
    for image in images:
        label = recognizer.step(detect(image), clock())
        if label:
            send(label)


def post_label(url: str, token: str, label: str) -> None:
    import httpx

    try:
        httpx.post(url, json={"gesture": label}, headers={"X-Rafiq-Gesture-Token": token}, timeout=2)
    except httpx.HTTPError:
        pass    # Rafiq stopped or is busy: the next gesture tries again


# ---------------------------------------------------------------------- camera and MediaPipe

class Camera:
    """Webcam frames, mirrored so moving your hand to your right is a swipe to the right."""

    def __init__(self, index: int = 0):
        import cv2

        self.cv2 = cv2
        self.capture = cv2.VideoCapture(index)
        if not self.capture.isOpened():
            raise RuntimeError(f"no webcam at index {index}")

    def __iter__(self):
        while True:
            ok, frame = self.capture.read()
            if not ok:
                return
            yield self.cv2.flip(frame, 1)

    def close(self) -> None:
        self.capture.release()


class MediaPipeHands:
    """Up to two hands per image, each as 21 (x, y) landmarks from 0 to 1."""

    def __init__(self):
        import cv2
        import mediapipe as mp

        self.cv2 = cv2
        self.solution = mp.solutions.hands
        self.drawing = mp.solutions.drawing_utils
        self.hands = self.solution.Hands(max_num_hands=2, min_detection_confidence=0.6, min_tracking_confidence=0.5)
        self.last = None

    def __call__(self, image) -> list[Hand]:
        self.last = self.hands.process(self.cv2.cvtColor(image, self.cv2.COLOR_BGR2RGB))
        found = self.last.multi_hand_landmarks or []
        return [[(p.x, p.y) for p in h.landmark] for h in found]

    def draw(self, image) -> None:
        for h in (self.last.multi_hand_landmarks or []) if self.last else []:
            self.drawing.draw_landmarks(image, h, self.solution.HAND_CONNECTIONS)


def window(camera_index: int, recognizer: Recognizer, calibrate: bool) -> int:
    """The demo: the camera with landmarks and the last gesture. With calibrate, two sliders set the
    hold and cooldown times; s saves them, q or Esc closes."""
    import cv2

    from . import gestures

    camera, detect = Camera(camera_index), MediaPipeHands()
    last = ["", 0.0]

    def shown(label: str) -> None:
        last[:] = [label, time.monotonic()]

    if calibrate:
        cv2.namedWindow(WINDOW)
        cv2.createTrackbar("hold ms", WINDOW, int(recognizer.hold.hold * 1000), 2000, lambda v: None)
        cv2.createTrackbar("cooldown ms", WINDOW, int(recognizer.hold.cooldown * 1000), 3000, lambda v: None)
    try:
        for image in camera:
            if calibrate:
                hold = max(0.1, cv2.getTrackbarPos("hold ms", WINDOW) / 1000)
                cooldown = max(0.1, cv2.getTrackbarPos("cooldown ms", WINDOW) / 1000)
                recognizer.hold.hold, recognizer.hold.cooldown = hold, cooldown
                recognizer.swipe.rest = recognizer.zoom.rest = cooldown
            track([image], detect, shown, recognizer)
            detect.draw(image)
            if time.monotonic() - last[1] < 1.5:
                cv2.putText(image, last[0], (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 220, 0), 3)
            if calibrate:
                cv2.putText(image, "s: save   q: close", (20, image.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (255, 255, 255), 2)
            cv2.imshow(WINDOW, image)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if calibrate and key == ord("s"):
                gestures.save_calibration(recognizer.hold.hold, recognizer.hold.cooldown)
                shown("saved")
    finally:
        camera.close()
        cv2.destroyAllWindows()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rafiq.tracker", description="hand gestures from the webcam")
    parser.add_argument("--post", help="Rafiq's /api/gesture address; the token is in RAFIQ_GESTURE_TOKEN")
    parser.add_argument("--hold", type=float, default=0.6)
    parser.add_argument("--cooldown", type=float, default=1.0)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--demo", action="store_true", help="show the camera with the hand landmarks")
    parser.add_argument("--calibrate", action="store_true", help="the demo, with sliders for the timing")
    args = parser.parse_args(argv)
    recognizer = Recognizer(args.hold, args.cooldown)
    if args.demo or args.calibrate:
        return window(args.camera, recognizer, args.calibrate)
    token = os.environ.get("RAFIQ_GESTURE_TOKEN", "")
    if not args.post or not token:
        print("rafiq.tracker: --post and RAFIQ_GESTURE_TOKEN are needed (Rafiq starts this itself)", file=sys.stderr)
        return 2
    camera = Camera(args.camera)
    try:
        track(camera, MediaPipeHands(), lambda label: post_label(args.post, token, label), recognizer)
    finally:
        camera.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
