"""Webcam switch using MediaPipe Face Mesh.

The capture loop lives on a background thread and never touches Tk.
The UI thread calls ``poll()`` and, when ``triggered`` is set, invokes
``trigger_select`` itself. That keeps OpenCV, MediaPipe, and
CustomTkinter on separate sides of the lock.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


log = logging.getLogger("ican.camera")

APP_DIR = Path(__file__).resolve().parent
MODEL_PATH = APP_DIR / "data" / "face_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
COOLDOWN_SECONDS = 0.8
CALIBRATION_SECONDS = 1.0
# A natural blink is over before this. A deliberate click is a closed hold
# that lasts into the 0.5–0.8s window, then the eyes open or the window ends.
NATURAL_BLINK_MAX = 0.40
INTENT_MIN = 0.50
INTENT_MAX = 0.80
EMERGENCY_HOLD = 2.5

# Face Mesh indices. Vertical brow-to-lid distance is the eyebrow switch.
# Mouth width, mouth opening, and cheek-to-corner distance are the twitch.
LEFT_BROW = 105
LEFT_LID = 159
RIGHT_BROW = 334
RIGHT_LID = 386
LEFT_EYE = 33
RIGHT_EYE = 263
MOUTH_LEFT = 61
MOUTH_RIGHT = 291
LIP_UPPER = 13
LIP_LOWER = 14
CHEEK_LEFT = 50
CHEEK_RIGHT = 280
FACE_LEFT = 234
FACE_RIGHT = 454
FOREHEAD = 10
CHIN = 152

# Six-point eye contours used for the Eye Aspect Ratio.
LEFT_EYE_EAR = (33, 160, 158, 133, 153, 144)
RIGHT_EYE_EAR = (263, 387, 385, 362, 380, 373)
BROW_DOTS = (70, 63, 105, 66, 107, 300, 293, 334, 296, 336, 33, 133, 159, 145, 263, 362, 386, 374)
MOUTH_DOTS = (61, 291, 13, 14, 78, 308, 0, 17, 50, 280, 187, 411, 234, 454)
BLINK_DOTS = (*LEFT_EYE_EAR, *RIGHT_EYE_EAR, 159, 145, 386, 374)
ALL_DOTS = tuple(dict.fromkeys((*BROW_DOTS, *MOUTH_DOTS, *BLINK_DOTS, 1)))


@dataclass
class TrackerSnapshot:
    """A copy of the latest camera state, safe to read on the UI thread."""

    frame: np.ndarray | None
    status: str
    signal: float
    triggered: bool
    flash: bool
    camera_ok: bool
    calibrating: bool
    face_visible: bool
    emergency: bool = False
    come_back: bool = False


class FaceTracker:
    """Background face switch with neutral-face calibration and a cooldown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._mode = "eyebrow"
        self._sensitivity = 0.65
        self._calib_request: str | None = None
        self._frame: np.ndarray | None = None
        self._status = "Starting camera…"
        self._signal = 0.0
        self._triggered = False
        self._flash_until = 0.0
        self._camera_ok = False
        self._calibrating = False
        self._face_visible = False
        self._emergency = False
        self._come_back = False
        self._return_watch = False
        self._reset_mouth = False
        self._suspended = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, name="ican-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def pause(self) -> None:
        """Stop reading gestures until resume. The camera thread stays alive."""
        with self._lock:
            self._suspended = True
            self._triggered = False
            self._emergency = False
            self._come_back = False

    def resume(self) -> None:
        with self._lock:
            self._suspended = False

    def set_mode(self, mode: str) -> None:
        text = mode.lower()
        if "blink" in text or "eye" in text:
            cleaned = "blink"
        elif "mouth" in text or "cheek" in text:
            cleaned = "mouth"
        else:
            cleaned = "eyebrow"
        with self._lock:
            if cleaned == self._mode:
                return
            self._mode = cleaned
            self._calib_request = "auto"

    def set_sensitivity(self, value: float) -> None:
        with self._lock:
            self._sensitivity = min(1.0, max(0.15, float(value)))

    def request_calibration(self) -> None:
        with self._lock:
            self._calib_request = "user"

    def set_return_watch(self, enabled: bool) -> None:
        """Count mouth movements and ask the app to return after the second one."""
        with self._lock:
            self._return_watch = bool(enabled)
            self._reset_mouth = True
            if not enabled:
                self._come_back = False

    def poll(self) -> TrackerSnapshot:
        with self._lock:
            triggered = self._triggered
            self._triggered = False
            frame = None if self._frame is None else self._frame.copy()
            emergency = self._emergency
            self._emergency = False
            come_back = self._come_back
            self._come_back = False
            return TrackerSnapshot(
                frame=frame,
                status=self._status,
                signal=self._signal,
                triggered=triggered,
                flash=time.monotonic() < self._flash_until,
                camera_ok=self._camera_ok,
                calibrating=self._calibrating,
                face_visible=self._face_visible,
                emergency=emergency,
                come_back=come_back,
            )

    def _publish(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key if key.startswith("_") else f"_{key}", value)

    def _fire(self) -> None:
        with self._lock:
            self._triggered = True
            self._flash_until = time.monotonic() + COOLDOWN_SECONDS

    def _fire_emergency(self) -> None:
        with self._lock:
            self._emergency = True
            self._flash_until = time.monotonic() + 1.2

    def _fire_return(self) -> None:
        with self._lock:
            self._come_back = True

    def _worker(self) -> None:
        while self._running:
            cap = _open_camera()
            if cap is None:
                self._publish(
                    frame=_blank("No camera"),
                    status="Camera unavailable — use Space",
                    signal=0.0,
                    camera_ok=False,
                    calibrating=False,
                    face_visible=False,
                )
                if not _sleep(1.5, lambda: self._running):
                    break
                continue
            detector = None
            try:
                self._publish(status="Starting face tracking…", camera_ok=True, frame=_blank("Starting"))
                try:
                    detector = _MeshDetector()
                except Exception as exc:
                    log.exception("face mesh unavailable")
                    self._publish(status=f"Face mesh unavailable — use Space ({exc.__class__.__name__})")
                self._read(cap, detector)
            except Exception:
                log.exception("camera loop failed")
                if not _sleep(1.2, lambda: self._running):
                    break
            finally:
                if detector is not None:
                    detector.close()
                cap.release()

    def _read(self, cap, detector) -> None:
        misses = 0
        state = _GestureState()
        with self._lock:
            if self._calib_request is None:
                self._calib_request = "auto"
        while self._running:
            with self._lock:
                suspended = self._suspended
            if suspended:
                if not _sleep(0.15, lambda: self._running):
                    return
                continue
            started = time.perf_counter()
            ok, frame = cap.read()
            if not ok or frame is None or not frame.size:
                misses += 1
                if misses > 40:
                    self._publish(
                        status="Camera stalled — use Space",
                        camera_ok=False,
                        face_visible=False,
                        calibrating=False,
                    )
                    return
                continue
            misses = 0
            rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
            landmarks = None
            if detector is not None:
                try:
                    landmarks = detector.detect(rgb)
                except Exception:
                    log.exception("landmark detection failed")
                    detector.close()
                    detector = None
            self._consume(rgb, landmarks, state)
            leftover = 0.045 - (time.perf_counter() - started)
            if leftover > 0 and not _sleep(leftover, lambda: self._running):
                return

    def _consume(self, rgb: np.ndarray, landmarks, state: _GestureState) -> None:
        features = _features(landmarks) if landmarks is not None else None
        with self._lock:
            mode = self._mode
            sensitivity = self._sensitivity
            request = self._calib_request
            self._calib_request = None
        if request:
            state.collecting = False
            state.samples.clear()
            state.pending = request
        if mode != state.mode:
            state.mode = mode
            state.baseline = None
            state.pending = state.pending or "auto"
            state.collecting = False
            state.closed_since = None
            state.blink_fired = False
            state.was_closed = False
            state.emergency_since = None
            state.emergency_fired = False
            state.held = False
            state.hot = 0
        if state.pending and not state.collecting and features is not None:
            state.collecting = True
            state.samples = []
            state.deadline = time.monotonic() + CALIBRATION_SECONDS
            state.kind = state.pending
            state.pending = None

        preview = cv2.resize(rgb, (320, 240), interpolation=cv2.INTER_AREA)
        _draw(preview, landmarks, mode, state.collecting, False)
        now = time.monotonic()

        if state.collecting:
            if features is not None:
                state.samples.append(features)
            if now >= state.deadline:
                self._finish_calibration(state)
            self._publish(
                frame=preview,
                status="Calibrating — hold a relaxed face",
                signal=0.0,
                camera_ok=True,
                calibrating=True,
                face_visible=features is not None,
            )
            return

        if features is None:
            state.hot = 0
            state.closed_since = None
            state.blink_fired = False
            state.was_closed = False
            state.emergency_since = None
            state.emergency_fired = False
            state.still_since = None
            state.anchor = None
            state.track.clear()
            state.mouth_hot = False
            state.meter *= 0.5
            if now >= state.cooldown_until:
                state.held = False
            status = state.message if now < state.message_until else "No face — look at the camera"
            if state.pending:
                status = "Look at the camera to calibrate"
            self._publish(
                frame=preview,
                status=status,
                signal=state.meter,
                camera_ok=True,
                calibrating=False,
                face_visible=False,
            )
            return

        if state.baseline is None:
            status = state.message if now < state.message_until else "Waiting for a neutral face"
            self._show(preview, landmarks, mode, status, 0.0, False, True, False, 0.0, False)
            state.prev = features
            return

        ear = features[4]
        closed = _eyes_closed(ear, state.baseline[4], sensitivity, state.was_closed)
        state.was_closed = closed
        if mode == "blink":
            click, progress = update_blink(state, closed, now)
            active = closed
            norm = progress
            threshold = 1.0
            shown = progress
        else:
            norm = _activation(features, state.baseline, state.prev, mode)
            threshold = _threshold(sensitivity)
            shown = max(0.0, min(1.0, norm / threshold if threshold else 0.0))
            above = norm >= threshold
            if above:
                state.hot += 1
            else:
                state.hot = 0
                if now >= state.cooldown_until:
                    state.held = False
            click = state.hot >= 2 and not state.held and now >= state.cooldown_until
            active = above
            progress = 0.0
        state.prev = features
        state.meter = state.meter * 0.45 + shown * 0.55

        if click:
            self._fire()
            state.held = True
            state.cooldown_until = now + COOLDOWN_SECONDS
        self._remember_face(state, landmarks, features, now)
        self._note_mouth(state, features, now)
        alarm = update_safety(state, _face_moved(state, features), hard_shake(state.track, now), now)
        if alarm:
            self._fire_emergency()
        if mode == "blink" and closed:
            bar, bar_alarm = progress, False
        else:
            bar, bar_alarm = 0.0, False

        if alarm:
            status = "Emergency — calling for help"
        elif click:
            status = "Selected — relax"
        elif now < state.message_until:
            status = state.message
        elif mode == "blink":
            status = _blink_status(state, closed, now)
        elif state.held:
            status = "Selected — relax your face"
        else:
            label = "Eyebrow" if mode == "eyebrow" else "Mouth"
            quiet = max(0.0, STILL_SECONDS - (now - state.still_since)) if state.still_since else STILL_SECONDS
            status = f"{label}  {norm:.2f} / {threshold:.2f}"
            if quiet < 30:
                status = f"Very still — help in {quiet:.0f}s if you cannot move"
        signal = 1.0 if click or alarm else (progress if mode == "blink" and closed else state.meter)
        self._show(preview, landmarks, mode, status, signal, False, True, click, bar, bar_alarm)

    def _show(
        self,
        preview,
        landmarks,
        mode: str,
        status: str,
        signal: float,
        calibrating: bool,
        face: bool,
        flash: bool,
        hold: float,
        alarm_bar: bool,
    ) -> None:
        _draw(preview, landmarks, mode, calibrating, flash)
        _paint_hold(preview, hold, alarm_bar)
        self._publish(
            frame=preview,
            status=status,
            signal=signal,
            camera_ok=True,
            calibrating=calibrating,
            face_visible=face,
        )

    def _remember_face(self, state: _GestureState, landmarks, features, now: float) -> None:
        with self._lock:
            if self._reset_mouth:
                self._reset_mouth = False
                state.mouth_hits = 0
                state.mouth_hot = False
        if landmarks is not None and len(landmarks) > NOSE:
            nose = landmarks[NOSE]
            state.track.append((now, float(nose.x), float(nose.y)))
            state.track = [item for item in state.track if now - item[0] <= 1.6]
        else:
            state.track.clear()

    def _note_mouth(self, state: _GestureState, features, now: float) -> None:
        opening = features[2]
        base = state.baseline[2] if state.baseline is not None else opening
        active = abs(opening - base) > 0.04
        if active and not state.mouth_hot and now - state.last_mouth > 0.5:
            state.mouth_hits += 1
            state.last_mouth = now
            with self._lock:
                watching = self._return_watch
            if watching and state.mouth_hits >= 2:
                state.mouth_hits = 0
                self._fire_return()
        state.mouth_hot = active

    def _watch_emergency(self, state: _GestureState, active: bool, now: float) -> bool:
        if not active:
            state.emergency_since = None
            state.emergency_fired = False
            return False
        if state.emergency_since is None:
            state.emergency_since = now
            return False
        if state.emergency_fired or now - state.emergency_since < EMERGENCY_HOLD:
            return False
        state.emergency_fired = True
        self._fire_emergency()
        return True

    def _finish_calibration(self, state: _GestureState) -> None:
        state.collecting = False
        if len(state.samples) >= 3:
            columns = list(zip(*state.samples))
            state.baseline = tuple(_median(column) for column in columns)
            state.samples = []
            state.hot = 0
            state.held = False
            state.prev = None
            state.closed_since = None
            state.blink_fired = False
            state.was_closed = False
            state.emergency_since = None
            state.emergency_fired = False
            state.message = "Neutral face saved"
            state.message_until = time.monotonic() + 1.6
            return
        state.samples = []
        state.message = "No clear face. Relax and calibrate again."
        state.message_until = time.monotonic() + 2.4
        if state.kind == "auto" and state.baseline is None:
            state.pending = "auto"


class _GestureState:
    def __init__(self) -> None:
        self.mode = "eyebrow"
        self.baseline: tuple[float, float, float, float] | None = None
        self.samples: list[tuple[float, float, float, float]] = []
        self.pending: str | None = None
        self.collecting = False
        self.kind = "auto"
        self.deadline = 0.0
        self.hot = 0
        self.held = False
        self.cooldown_until = 0.0
        self.prev: tuple[float, float, float, float] | None = None
        self.meter = 0.0
        self.message = ""
        self.message_until = 0.0
        self.closed_since: float | None = None
        self.blink_fired = False
        self.was_closed = False
        self.emergency_since: float | None = None
        self.emergency_fired = False
        self.still_since: float | None = None
        self.safety_fired = False
        self.safety_kind = ""
        self.safety_at = 0.0
        self.anchor: tuple[float, float, float] | None = None
        self.track: list[tuple[float, float, float]] = []
        self.mouth_hot = False
        self.mouth_hits = 0
        self.last_mouth = 0.0


class _MeshDetector:
    """MediaPipe Face Landmarker in video mode, used only on the camera thread."""

    def __init__(self) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode

        model = _ensure_model()
        options = FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
            running_mode=RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._mp = mp
        self._landmarker = FaceLandmarker.create_from_options(options)
        self._t0 = time.monotonic()
        self._last_ts = 0

    def detect(self, rgb: np.ndarray):
        timestamp = int((time.monotonic() - self._t0) * 1000)
        if timestamp <= self._last_ts:
            timestamp = self._last_ts + 1
        self._last_ts = timestamp
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=np.ascontiguousarray(rgb),
        )
        result = self._landmarker.detect_for_video(image, timestamp)
        faces = result.face_landmarks
        if not faces:
            return None
        return faces[0]

    def close(self) -> None:
        try:
            self._landmarker.close()
        except Exception:
            log.info("face landmarker was already closed")


def _ensure_model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 500_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "ICan/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        MODEL_PATH.write_bytes(response.read())
    if MODEL_PATH.stat().st_size < 500_000:
        raise RuntimeError("face model download was incomplete")
    return MODEL_PATH


def _open_camera():
    backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if sys.platform == "win32" else [cv2.CAP_ANY]
    for backend in backends:
        for index in (0, 1):
            cap = cv2.VideoCapture(index, backend)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                return cap
            cap.release()
    return None


def _features(landmarks) -> tuple[float, float, float, float] | None:
    if len(landmarks) <= FACE_RIGHT:
        return None

    def xy(index: int) -> tuple[float, float]:
        point = landmarks[index]
        return float(point.x), float(point.y)

    def dist(a: int, b: int) -> float:
        ax, ay = xy(a)
        bx, by = xy(b)
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    eyes = dist(LEFT_EYE, RIGHT_EYE) or 1e-6
    face_w = dist(FACE_LEFT, FACE_RIGHT) or 1e-6
    face_h = dist(FOREHEAD, CHIN) or 1e-6
    brow = ((xy(LEFT_LID)[1] - xy(LEFT_BROW)[1]) + (xy(RIGHT_LID)[1] - xy(RIGHT_BROW)[1])) / (2 * eyes)
    width = dist(MOUTH_LEFT, MOUTH_RIGHT) / face_w
    opening = (xy(LIP_LOWER)[1] - xy(LIP_UPPER)[1]) / face_h
    cheek = (dist(CHEEK_LEFT, MOUTH_LEFT) + dist(CHEEK_RIGHT, MOUTH_RIGHT)) / (2 * face_w)
    ear = (_eye_aspect(dist, LEFT_EYE_EAR) + _eye_aspect(dist, RIGHT_EYE_EAR)) / 2
    return (brow, width, opening, cheek, ear)


def _eye_aspect(dist, indices: tuple[int, ...]) -> float:
    outer, upper_a, upper_b, inner, lower_b, lower_a = indices
    vertical = dist(upper_a, lower_a) + dist(upper_b, lower_b)
    horizontal = dist(outer, inner) or 1e-6
    return vertical / (2 * horizontal)


def _eyes_closed(ear: float, baseline_ear: float, sensitivity: float, was_closed: bool) -> bool:
    """Closed when the aspect ratio falls well below the resting eye opening."""
    ratio = 0.46 + min(1.0, max(0.15, sensitivity)) * 0.28
    limit = max(0.11, baseline_ear * ratio)
    if was_closed:
        limit *= 1.2
    return ear < limit


def update_blink(state: _GestureState, closed: bool, now: float) -> tuple[bool, float]:
    """Return (click, progress). Progress fills across the 0.8s intentional window.

    Openings shorter than 0.4s are natural blinks. A click happens when the
    eyes open after a 0.5–0.8s hold, or when that hold reaches 0.8s.
    """
    if closed:
        if state.closed_since is None:
            state.closed_since = now
            state.blink_fired = False
        held = now - state.closed_since
        progress = max(0.0, min(1.0, held / INTENT_MAX))
        if state.blink_fired or held < INTENT_MAX or now < state.cooldown_until:
            return False, progress
        state.blink_fired = True
        return True, progress
    click = False
    if state.closed_since is not None and not state.blink_fired:
        held = now - state.closed_since
        deliberate = INTENT_MIN <= held <= INTENT_MAX + 0.05 and held >= NATURAL_BLINK_MAX
        if deliberate and now >= state.cooldown_until:
            click = True
    state.closed_since = None
    state.blink_fired = False
    return click, 0.0


def _blink_status(state: _GestureState, closed: bool, now: float) -> str:
    if not closed or state.closed_since is None:
        return "Blink — close both eyes and hold"
    held = now - state.closed_since
    if held < INTENT_MIN:
        return f"Blink {held:.1f}s — keep holding"
    if held < INTENT_MAX:
        return f"Blink {held:.1f}s — open to select"
    return "Selected — you can open your eyes"


def _blink_bar(progress: float, hold: float, closed: bool) -> tuple[float, bool]:
    if not closed:
        return 0.0, False
    if hold <= INTENT_MAX:
        return progress, False
    alarm = min(1.0, (hold - INTENT_MAX) / (EMERGENCY_HOLD - INTENT_MAX))
    return alarm, True


def _hold_bar(hold: float, active: bool) -> tuple[float, bool]:
    if not active or hold < 0.45:
        return 0.0, False
    return min(1.0, hold / EMERGENCY_HOLD), True


def _activation(current, baseline, previous, mode: str) -> float:
    brow, width, opening, cheek = current[:4]
    base_brow, base_width, base_open, base_cheek = baseline[:4]
    if mode == "eyebrow":
        return max(0.0, brow - base_brow) / 0.07
    change = max(
        abs(width - base_width) / 0.08,
        abs(opening - base_open) / 0.06,
        abs(cheek - base_cheek) / 0.045,
    )
    if previous is not None:
        velocity = (
            abs(width - previous[1]) + abs(opening - previous[2]) + abs(cheek - previous[3])
        )
        change = max(change, velocity / 0.03)
    return change


def _threshold(sensitivity: float) -> float:
    return 0.30 + (1.0 - min(1.0, max(0.15, sensitivity))) * 1.05


STILL_SECONDS = 180.0
NOSE = 1


def hard_shake(samples, now: float) -> bool:
    """Violent back-and-forth motion. A smooth drift, as in a car, is not a shake."""
    window = [item for item in samples if now - item[0] <= 1.3]
    if len(window) < 8 or window[-1][0] - window[0][0] < 0.7:
        return False

    def turns(values: list[float]) -> int:
        count = 0
        previous = 0
        for earlier, later in zip(values, values[1:]):
            delta = later - earlier
            sign = 1 if delta > 0.006 else -1 if delta < -0.006 else 0
            if sign and previous and sign != previous:
                count += 1
            if sign:
                previous = sign
        return count

    xs = [item[1] for item in window]
    ys = [item[2] for item in window]
    amplitude = max(max(xs) - min(xs), max(ys) - min(ys))
    return amplitude >= 0.045 and max(turns(xs), turns(ys)) >= 6


def update_safety(state, moved: bool, shaking: bool, now: float) -> bool:
    """One alarm for hard shaking, or for three minutes with no facial movement."""
    if shaking:
        state.still_since = now
        if state.safety_fired:
            return False
        state.safety_fired = True
        state.safety_kind = "shake"
        state.safety_at = now
        return True
    if moved:
        state.still_since = now
        if state.safety_fired and now - state.safety_at > 30:
            state.safety_fired = False
        return False
    if state.still_since is None:
        state.still_since = now
    if state.safety_fired or now - state.still_since < STILL_SECONDS:
        return False
    state.safety_fired = True
    state.safety_kind = "still"
    state.safety_at = now
    return True


def _face_moved(state, features) -> bool:
    brow, _width, opening, _cheek, ear = features
    if state.anchor is None:
        state.anchor = (brow, opening, ear)
        return False
    base_brow, base_open, base_ear = state.anchor
    moved = abs(brow - base_brow) > 0.02 or abs(opening - base_open) > 0.018 or abs(ear - base_ear) > 0.04
    if moved:
        state.anchor = (brow, opening, ear)
    return moved


def _median(values) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float(ordered[mid - 1] + ordered[mid]) / 2


def _blank(text: str) -> np.ndarray:
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    image[:] = (14, 18, 28)
    cv2.putText(
        image,
        text,
        (16, 124),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (186, 196, 214),
        1,
        cv2.LINE_AA,
    )
    return image


def _draw(image: np.ndarray, landmarks, mode: str, calibrating: bool, flash: bool) -> None:
    height, width = image.shape[:2]
    if landmarks is not None and len(landmarks) > FACE_RIGHT:
        if mode == "blink":
            active = set(BLINK_DOTS)
        elif mode == "eyebrow":
            active = set(BROW_DOTS)
        else:
            active = set(MOUTH_DOTS)

        def point(index: int) -> tuple[int, int]:
            landmark = landmarks[index]
            x = int(max(0, min(width - 1, landmark.x * width)))
            y = int(max(0, min(height - 1, landmark.y * height)))
            return x, y

        for index in ALL_DOTS:
            if index >= len(landmarks):
                continue
            color = (255, 209, 92) if index in active else (130, 168, 186)
            cv2.circle(image, point(index), 2 if index in active else 1, color, -1, cv2.LINE_AA)
        gold = (255, 209, 92)
        if mode == "eyebrow":
            cv2.line(image, point(LEFT_BROW), point(LEFT_LID), gold, 1, cv2.LINE_AA)
            cv2.line(image, point(RIGHT_BROW), point(RIGHT_LID), gold, 1, cv2.LINE_AA)
        elif mode == "blink":
            for indices in (LEFT_EYE_EAR, RIGHT_EYE_EAR):
                outer, upper_a, upper_b, inner, lower_b, lower_a = indices
                cv2.line(image, point(outer), point(inner), gold, 1, cv2.LINE_AA)
                cv2.line(image, point(upper_a), point(lower_a), gold, 1, cv2.LINE_AA)
                cv2.line(image, point(upper_b), point(lower_b), gold, 1, cv2.LINE_AA)
        else:
            cv2.line(image, point(MOUTH_LEFT), point(MOUTH_RIGHT), gold, 1, cv2.LINE_AA)
            cv2.line(image, point(CHEEK_LEFT), point(MOUTH_LEFT), gold, 1, cv2.LINE_AA)
            cv2.line(image, point(CHEEK_RIGHT), point(MOUTH_RIGHT), gold, 1, cv2.LINE_AA)
    if calibrating:
        cv2.rectangle(image, (2, 2), (width - 3, height - 3), (255, 200, 87), 2)
    elif flash:
        cv2.rectangle(image, (2, 2), (width - 3, height - 3), (80, 220, 160), 2)


def _paint_hold(image: np.ndarray, amount: float, alarm: bool) -> None:
    """A thin bar along the bottom of the preview. Gold is a blink hold; red is the alarm."""
    if amount <= 0.04:
        return
    height, width = image.shape[:2]
    left, right = 8, width - 8
    top, bottom = height - 12, height - 5
    cv2.rectangle(image, (left, top), (right, bottom), (36, 44, 58), -1, cv2.LINE_AA)
    filled = left + int((right - left) * max(0.0, min(1.0, amount)))
    color = (220, 64, 64) if alarm else (255, 196, 87)
    cv2.rectangle(image, (left, top), (max(left + 2, filled), bottom), color, -1, cv2.LINE_AA)
    if not alarm:
        mark = left + int((right - left) * (INTENT_MIN / INTENT_MAX))
        cv2.line(image, (mark, top - 2), (mark, bottom + 2), (230, 236, 244), 1, cv2.LINE_AA)


def _sleep(seconds: float, running) -> bool:
    end = time.monotonic() + seconds
    while running() and time.monotonic() < end:
        time.sleep(0.02)
    return bool(running())


def _self_check() -> None:
    """Timing rules for a deliberate blink, without a camera."""

    def play(intervals: list[tuple[bool, float]]) -> list[float]:
        state = _GestureState()
        clicks = []
        now = 0.0
        for closed, step in intervals:
            now += step
            click, _progress = update_blink(state, closed, now)
            if click:
                clicks.append(round(now, 3))
                state.cooldown_until = now + COOLDOWN_SECONDS
        return clicks

    short = play([(True, 0.05)] * 6 + [(False, 0.05)])  # 0.30s closed
    assert short == [], short
    medium = play([(True, 0.05)] * 9 + [(False, 0.05)])  # 0.45s closed
    assert medium == [], medium
    deliberate = play([(True, 0.05)] * 13 + [(False, 0.05)])  # 0.65s then open
    assert deliberate == [0.7], deliberate
    held = play([(True, 0.1)] * 12)  # stays closed; the click is the moment the hold reaches 0.8s
    assert len(held) == 1 and held[0] >= INTENT_MAX, held
    state = _GestureState()
    assert update_safety(state, False, False, 10) is False
    assert update_safety(state, False, False, 189) is False
    assert update_safety(state, False, False, 190) is True
    assert update_safety(state, False, False, 200) is False
    state = _GestureState()
    assert update_safety(state, True, False, 50) is False
    assert update_safety(state, True, False, 400) is False
    shake = [(index * 0.08, 0.03 * ((-1) ** index), 0.0) for index in range(16)]
    assert hard_shake(shake, shake[-1][0]) is True
    drift = [(index * 0.1, index * 0.01, 0.0) for index in range(12)]
    assert hard_shake(drift, drift[-1][0]) is False


if __name__ == "__main__":
    _self_check()
    print("blink timing ok")
