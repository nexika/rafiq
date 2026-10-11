"""Hand shapes and movements from MediaPipe's 21 hand landmarks, as labels: open_palm, thumbs_up, fist,
fingers_1 to fingers_3, and the movements swipe_left, swipe_right (one hand) and zoom_in, zoom_out (two
hands). Plain arithmetic, so it is tested without a camera.

Landmarks are (x, y) in the mirrored camera image, 0 to 1, y going down: 0 is the wrist, 1-4 the
thumb, 5-8 the index, 9-12 the middle finger, 13-16 the ring finger, 17-20 the little finger.
"""

from __future__ import annotations

import math
from collections import deque

Point = tuple[float, float]
Hand = list[Point]

SHAPES = ("open_palm", "thumbs_up", "fist", "fingers_1", "fingers_2", "fingers_3")
MOVES = ("swipe_left", "swipe_right", "zoom_in", "zoom_out")
LABELS = frozenset(SHAPES + MOVES)

FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))    # (tip, middle joint) of index, middle, ring, little
PALM = (0, 5, 9, 13, 17)
EPSILON = 1e-9


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def extended(hand: Hand) -> tuple[bool, list[bool]]:
    """Whether the thumb, and each of the four fingers, is stretched out. A finger is out when its tip
    is farther from the wrist than its middle joint; the thumb, when its tip is farther from the base
    of the middle finger than its own base is. Distances, not directions, so a tilted hand still counts."""
    wrist = hand[0]
    fingers = [_dist(wrist, hand[tip]) > _dist(wrist, hand[joint]) for tip, joint in FINGERS]
    thumb = _dist(hand[4], hand[9]) > _dist(hand[2], hand[9])
    return thumb, fingers


def classify(hand: Hand) -> str | None:
    """The shape of one hand, or None when it is not one of SHAPES."""
    if len(hand) != 21:
        return None
    thumb, fingers = extended(hand)
    count = sum(fingers)
    if count == 4:
        return "open_palm" if thumb else None
    if count == 0 and not thumb:
        return "fist"
    if count == 0:   # thumb out: a thumbs-up only when the thumb tip is the highest point of the hand
        return "thumbs_up" if hand[4][1] < min(y for i, (_, y) in enumerate(hand) if i != 4) else None
    return None if thumb else f"fingers_{count}"


def center(hand: Hand) -> Point:
    return (sum(hand[i][0] for i in PALM) / len(PALM), sum(hand[i][1] for i in PALM) / len(PALM))


class HoldFilter:
    """A shape counts once it is held for hold_seconds, and only once until it changes; after a gesture,
    the next waits cooldown_seconds."""

    def __init__(self, hold_seconds: float, cooldown_seconds: float):
        self.hold, self.cooldown = hold_seconds, cooldown_seconds
        self.label: str | None = None
        self.since = 0.0
        self.fired = False
        self.last = -math.inf

    def reset(self) -> None:
        self.label, self.fired = None, False

    def push(self, label: str | None, now: float) -> str | None:
        if label != self.label:
            self.label, self.since, self.fired = label, now, False
        if (label is None or self.fired or now - self.since < self.hold - EPSILON
                or now - self.last < self.cooldown - EPSILON):
            return None
        self.fired, self.last = True, now
        return label


class Motion:
    """A value that moves by at least `distance` within `seconds`: +1 up, -1 down, else 0. After a move
    it rests for `rest` seconds, so one sweep of the hand is one gesture."""

    def __init__(self, distance: float, seconds: float, rest: float):
        self.distance, self.seconds, self.rest = distance, seconds, rest
        self.window: deque[tuple[float, float]] = deque()
        self.quiet_until = -math.inf

    def reset(self) -> None:
        self.window.clear()

    def push(self, value: float, now: float) -> int:
        if now < self.quiet_until:
            return 0
        self.window.append((now, value))
        while now - self.window[0][0] > self.seconds + EPSILON:
            self.window.popleft()
        change = value - self.window[0][1]
        if abs(change) < self.distance:
            return 0
        self.window.clear()
        self.quiet_until = now + self.rest
        return 1 if change > 0 else -1


class Recognizer:
    """Hands seen in each camera frame in, a label out when a gesture is complete."""

    def __init__(self, hold_seconds: float = 0.6, cooldown_seconds: float = 1.0):
        self.hold = HoldFilter(hold_seconds, cooldown_seconds)
        self.swipe = Motion(distance=0.25, seconds=0.5, rest=cooldown_seconds)
        self.zoom = Motion(distance=0.15, seconds=0.7, rest=cooldown_seconds)

    def step(self, hands: list[Hand], now: float) -> str | None:
        if len(hands) == 2:
            self.hold.reset()
            self.swipe.reset()
            a, b = (center(h) for h in hands)
            move = self.zoom.push(_dist(a, b), now)
            return {1: "zoom_in", -1: "zoom_out"}.get(move)
        self.zoom.reset()
        if len(hands) != 1:
            self.hold.reset()
            self.swipe.reset()
            return None
        move = self.swipe.push(center(hands[0])[0], now)
        if move:
            self.hold.reset()
            return "swipe_right" if move > 0 else "swipe_left"
        return self.hold.push(classify(hands[0]), now)
