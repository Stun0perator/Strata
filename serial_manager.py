"""
Strata serial control: direct EBB protocol over pyserial (9600 8N1, ASCII + \\r).
Motion/step math aligned with saxi (AxiDraw v3: 5 logical steps/mm × 16 microstepping = 80 microsteps/mm for SM/LM/XM).
No pyaxidraw / axicli / subprocess.
"""
from __future__ import annotations

import glob
import logging
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional, Tuple

import serial
from serial.tools import list_ports as serial_list_ports

from config_manager import ConfigManager
from plotter_state import PlotState, PlotterState

logger = logging.getLogger("strata.serial")

# Saxi planning: stepsPerMm = 5 at motor resolution; 16× microstepping → SM/LM/XM use 80 microsteps/mm
STEPS_PER_MM_LOGICAL = 5
MICROSTEP_FACTOR = 16
MICROSTEPS_PER_MM = STEPS_PER_MM_LOGICAL * MICROSTEP_FACTOR  # 80

QM_POLL_INTERVAL_S = 0.05
QM_MAX_WAIT_S = 30.0
MAX_DRAW_SPEED_MM_S = 300.0
MAX_TRAVEL_SPEED_MM_S = 400.0
MAX_SPEED_MM_S = MAX_TRAVEL_SPEED_MM_S
MAX_ACCEL_MM_S2 = 800.0

# EBB rate scale (saxi ebb.ts)
_LM_RATE_SCALE = 0x80000000 / 25000.0
PLANNER_EPSILON = 1e-9
SAXI_CORNER_FACTOR_MM = 0.127

# Saxi exact defaults (planning.ts defaultPlanOptions + massager.ts replan)
SAXI_DRAW_ACCEL_MM_S2 = 200.0
SAXI_TRAVEL_ACCEL_MM_S2 = 400.0
SAXI_DRAW_CORNER_FACTOR_MM = 0.127
SAXI_TRAVEL_CORNER_FACTOR_MM = 0.0

# Douglas-Peucker tolerance for path simplification before planning.
# SVG paths sampled at 0.5 mm create thousands of near-collinear points;
# each becomes a planner segment with near-zero corner velocity, causing
# constant accel/decel (the "buzzy" symptom).  We fix it by simplifying here.
PATH_SIMPLIFY_TOLERANCE_MM = 0.02

DEFAULT_PEN_MOTION_MS = 120

PEN_SERVO_MIN = 7500   # pen down (100% "down" in saxi)
PEN_SERVO_MAX = 28000  # pen up


class Priority(IntEnum):
    EMERGENCY = 1
    MANUAL = 2
    STREAM = 3


@dataclass(order=True)
class SerialCommand:
    priority: int
    seq: int
    command: str = field(compare=False)
    callback: Optional[Callable] = field(default=None, compare=False)
    tag: str = field(default="", compare=False)


class SerialManager:
    """
    Priority queue + serial worker + button poll thread.
    All ``Serial.write`` / reads that touch the port use ``_serial_lock``.
    """

    def __init__(self, config: ConfigManager, state: PlotterState):
        self.config = config
        self.state = state
        self._ser: Optional[serial.Serial] = None
        self._serial_lock = threading.Lock()
        self._cmd_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq_counter = 0
        self._seq_lock = threading.Lock()

        self.live_overrides: dict = {}
        self._overrides_lock = threading.Lock()

        self._running = False
        self._paused = False
        self._pause_lock = threading.Lock()
        self._emergency_stop = False
        self._cancel_requested = False

        self._serial_thread: Optional[threading.Thread] = None
        self._button_thread: Optional[threading.Thread] = None

        self.on_state_change: Optional[Callable] = None
        self.on_button_press: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

        self._ebb_version: Tuple[int, int, int] = (0, 0, 0)
        self._ebb_lm_capable = False
        self._ebb_sr_capable = False
        self._last_pen_servo_pos: int = PEN_SERVO_MAX
        self._commanded_x: float = 0.0
        self._commanded_y: float = 0.0
        self._stream_motion_error = False
        self._step_error_x: float = 0.0
        self._step_error_y: float = 0.0

    # ---- ports ----

    @staticmethod
    def list_ports() -> list[str]:
        try:
            return sorted({p.device for p in serial_list_ports.comports()})
        except Exception:
            pass
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
        if not ports:
            ports = glob.glob("/dev/tty.*") + glob.glob("COM*")
        return sorted(ports)

    # ---- raw serial (must hold _serial_lock) ----

    def _write_line(self, line: str) -> None:
        if self._ser is None or not self._ser.is_open:
            return
        if not line.endswith("\r"):
            line = line + "\r"
        self._ser.write(line.encode("ascii", errors="replace"))

    def _read_bytes_until(
        self,
        end_time: float,
        stop_pred: Callable[[bytes], bool],
    ) -> bytes:
        buf = b""
        while time.time() < end_time:
            if self._ser is None or not self._ser.is_open:
                break
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n)
                if stop_pred(buf):
                    break
            else:
                time.sleep(0.01)
        return buf

    def _read_response_line(self, timeout: float = 2.0) -> str:
        """Single line (\\r or \\n terminated)."""
        if self._ser is None or not self._ser.is_open:
            return ""
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n)
                if b"\r" in buf or b"\n" in buf:
                    return buf.decode("ascii", errors="replace").strip()
            time.sleep(0.01)
        return buf.decode("ascii", errors="replace").strip()

    def _read_response_until_ok(self, timeout: float = 2.0) -> str:
        """Multi-line EBB reply containing a line ``OK`` (QB, V, S2, …)."""
        if self._ser is None or not self._ser.is_open:
            return ""
        end = time.time() + timeout

        def done(b: bytes) -> bool:
            try:
                text = b.decode("ascii", errors="replace")
            except Exception:
                return False
            return re.search(r"(?im)^OK\s*$", text) is not None

        raw = self._read_bytes_until(end, done)
        return raw.decode("ascii", errors="replace").strip()

    def _exchange(self, line: str, read_timeout: float = 2.0, multiline_ok: bool = False) -> str:
        self._write_line(line)
        if multiline_ok:
            return self._read_response_until_ok(timeout=read_timeout)
        return self._read_response_line(timeout=read_timeout)

    # ---- EBB helpers ----

    @staticmethod
    def _parse_ebb_version(text: str) -> Tuple[int, int, int]:
        """
        Parse 'EBBv13_and_above EB Firmware Version 2.8.1' → (2, 8, 1).
        """
        try:
            words = text.strip().split()
            major, minor, patch = words[-1].split(".")
            return int(major), int(minor), int(patch)
        except Exception:
            return (0, 0, 0)

    @staticmethod
    def _ver_gte(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> bool:
        return a >= b if len(a) == len(b) else False

    @staticmethod
    def pen_pct_to_pos(pct: float) -> int:
        """Saxi penPctToPos: blend pen down (7500) and up (28000)."""
        p = max(0.0, min(100.0, float(pct)))
        return int(round(7500.0 * (p / 100.0) + 28000.0 * (1.0 - p / 100.0)))

    def _microsteps_xy(self, dx_mm: float, dy_mm: float) -> Tuple[int, int]:
        sx = round(dx_mm * MICROSTEPS_PER_MM)
        sy = round(dy_mm * MICROSTEPS_PER_MM)
        return int(sx), int(sy)

    def _corexy_axes(self, sx: int, sy: int) -> Tuple[int, int]:
        return sx + sy, sx - sy

    def _pen_motion_duration_ms(self, raise_pen: bool) -> int:
        """Default 120 ms; profile rates shorten/lengthen motion."""
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        if raise_pen:
            r = float(o.get("pen_rate_raise", profile.get("pen_rate_raise", 75)))
        else:
            r = float(o.get("pen_rate_lower", profile.get("pen_rate_lower", 50)))
        # Higher rate → faster → shorter duration (bounded)
        return max(20, int(DEFAULT_PEN_MOTION_MS * (100.0 / max(5.0, r))))

    def _s2_pen_to(self, target_pos: int, duration_ms: int) -> None:
        """S2,pos,channel,rate,delay — saxi executePenMotion style."""
        diff = abs(target_pos - self._last_pen_servo_pos)
        rate = int(round((diff * 24) / max(1, duration_ms)))
        cmd = f"S2,{target_pos},4,{rate},0"
        self._exchange(cmd, read_timeout=2.0, multiline_ok=True)
        self._last_pen_servo_pos = target_pos

    def _s2_pen_height(self, target_pos: int, rate: int = 1000, delay_ms: int = 1000) -> None:
        """Saxi pre-plot style pen positioning."""
        cmd = f"S2,{target_pos},4,{int(rate)},{int(delay_ms)}"
        self._exchange(cmd, read_timeout=max(2.0, delay_ms / 1000.0 + 1.0), multiline_ok=True)
        self._last_pen_servo_pos = target_pos

    # ---- connection ----

    def connect(self, port: str) -> bool:
        port = str(port).strip()
        try:
            ser = serial.Serial(port, 9600, timeout=2)
            time.sleep(0.15)
            ser.reset_input_buffer()
            with self._serial_lock:
                self._ser = ser
                self._write_line("V")
                raw = self._read_response_until_ok(timeout=3.0)
            if "EBB" not in raw.upper():
                logger.error("EBB not detected on %s (response: %r)", port, raw[:300])
                try:
                    ser.close()
                except Exception:
                    pass
                self._ser = None
                return False

            self._ebb_version = self._parse_ebb_version(raw)
            self._ebb_lm_capable = self._ver_gte(self._ebb_version, (2, 5, 3))
            self._ebb_sr_capable = self._ver_gte(self._ebb_version, (2, 6, 0))
            logger.info(
                "EBB firmware %s.%s.%s — LM=%s SR=%s",
                self._ebb_version[0],
                self._ebb_version[1],
                self._ebb_version[2],
                self._ebb_lm_capable,
                self._ebb_sr_capable,
            )

            # Initial pen position = up (matches UI pen_is_down=False after connect)
            profile = self._active_profile()
            with self._overrides_lock:
                o = dict(self.live_overrides)
            pu = float(o.get("pen_pos_up", profile.get("pen_pos_up", 60)))
            self._last_pen_servo_pos = self.pen_pct_to_pos(pu)

            fw_ver = raw.replace("\r", " ").replace("\n", " ").strip()[:200]
            self.state.update(
                connected=True,
                firmware_version=fw_ver,
                serial_port=port,
                position_known=True,
            )
            self.config.set("last_serial_port", port)
            self._running = True
            self._emergency_stop = False
            self._paused = False
            self._serial_thread = threading.Thread(
                target=self._serial_loop, daemon=True, name="serial-worker"
            )
            self._button_thread = threading.Thread(
                target=self._button_poll_loop, daemon=True, name="button-poll"
            )
            self._serial_thread.start()
            self._button_thread.start()
            logger.info("Connected to EBB on %s @ 9600", port)

            if self.config.get("auto_home_on_connect"):
                self.walk_home()

            return True
        except Exception as e:
            logger.error("Connection failed: %s", e, exc_info=True)
            self._close_serial()
            return False

    def _close_serial(self):
        if self._ser is not None:
            try:
                with self._serial_lock:
                    try:
                        self._write_line("EM,0,0")
                        if self._ebb_sr_capable:
                            self._write_line("SR,60000,0")
                    except Exception:
                        pass
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def disconnect(self):
        self._running = False
        self.state.update(connected=False, plot_state=PlotState.IDLE)
        if self._serial_thread:
            self._serial_thread.join(timeout=3)
        if self._button_thread:
            self._button_thread.join(timeout=1)
        self._close_serial()
        self.state.update(serial_port="", firmware_version="")
        self._flush_queue()
        logger.info("Disconnected")

    # ---- profile / speeds ----

    def _active_profile(self) -> dict:
        name = self.config.get("last_profile", "Default")
        p = self.config.get_profile(name)
        return p if p else self.config.get_profile("Default") or {}

    def _speed_value_mm_s(self, value: float, max_speed: float) -> float:
        return max(1.0, min(float(value), max_speed))

    def _accel_pct(self) -> float:
        """Return the accel slider value as a fraction (0..1+). Default 100%."""
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        return max(0.05, float(o.get("accel", profile.get("accel", 100))) / 100.0)

    def _draw_accel_mm_s2(self) -> float:
        """Saxi penDownAcceleration default 200 mm/s², scaled by user slider."""
        return max(20.0, self._accel_pct() * SAXI_DRAW_ACCEL_MM_S2)

    def _travel_accel_mm_s2(self) -> float:
        """Saxi penUpAcceleration default 400 mm/s², scaled by user slider."""
        return max(20.0, self._accel_pct() * SAXI_TRAVEL_ACCEL_MM_S2)

    def _speed_mm_s_for_tag(self, tag: str, swirl_sp: Optional[float] = None) -> float:
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        if swirl_sp is not None:
            return self._speed_value_mm_s(float(swirl_sp), MAX_DRAW_SPEED_MM_S)
        if tag in ("draw", "swirl"):
            speed = float(o.get("speed_pendown", profile.get("speed_pendown", 50)))
            return self._speed_value_mm_s(speed, MAX_DRAW_SPEED_MM_S)
        else:
            speed = float(o.get("speed_penup", profile.get("speed_penup", 200)))
            return self._speed_value_mm_s(speed, MAX_TRAVEL_SPEED_MM_S)

    def _lm_axis_rates(self, steps: int, initial_steps_per_sec: float, final_steps_per_sec: float) -> Tuple[int, int]:
        """Saxi-style LM axis rate computation (ebb.ts axisRate)."""
        if steps == 0:
            return 0, 0
        initial_rate = round(initial_steps_per_sec * _LM_RATE_SCALE)
        final_rate = round(final_steps_per_sec * _LM_RATE_SCALE)
        move_time = (2.0 * abs(steps)) / (initial_steps_per_sec + final_steps_per_sec) if (initial_steps_per_sec + final_steps_per_sec) != 0 else 0
        if move_time == 0:
            return 0, 0
        delta_r = round((final_rate - initial_rate) / (move_time * 25000.0))
        return int(initial_rate), int(delta_r)

    # ---- path simplification (Douglas-Peucker) ----

    @staticmethod
    def _perpendicular_distance(pt, line_start, line_end):
        dx = line_end[0] - line_start[0]
        dy = line_end[1] - line_start[1]
        mag_sq = dx * dx + dy * dy
        if mag_sq < 1e-18:
            return math.hypot(pt[0] - line_start[0], pt[1] - line_start[1])
        t = max(0.0, min(1.0, ((pt[0] - line_start[0]) * dx + (pt[1] - line_start[1]) * dy) / mag_sq))
        proj_x = line_start[0] + t * dx
        proj_y = line_start[1] + t * dy
        return math.hypot(pt[0] - proj_x, pt[1] - proj_y)

    @staticmethod
    def _simplify_path(points, tolerance=PATH_SIMPLIFY_TOLERANCE_MM):
        """Ramer-Douglas-Peucker to remove near-collinear points.

        SVG paths sampled at 0.5 mm produce hundreds of collinear points per
        straight segment.  Each becomes a planner segment with a near-zero
        corner velocity, so the motors accel/decel constantly = buzz.
        This reduces a 200-point line to 2 points while preserving curves.
        """
        if len(points) <= 2:
            return list(points)
        keep = [False] * len(points)
        keep[0] = keep[-1] = True
        stack = [(0, len(points) - 1)]
        while stack:
            start_idx, end_idx = stack.pop()
            max_dist = 0.0
            farthest = start_idx
            sp = points[start_idx]
            ep = points[end_idx]
            for i in range(start_idx + 1, end_idx):
                d = SerialManager._perpendicular_distance(points[i], sp, ep)
                if d > max_dist:
                    max_dist = d
                    farthest = i
            if max_dist > tolerance:
                keep[farthest] = True
                if farthest - start_idx > 1:
                    stack.append((start_idx, farthest))
                if end_idx - farthest > 1:
                    stack.append((farthest, end_idx))
        return [p for i, p in enumerate(points) if keep[i]]

    @staticmethod
    def _vsub(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        return a[0] - b[0], a[1] - b[1]

    @staticmethod
    def _vadd(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        return a[0] + b[0], a[1] + b[1]

    @staticmethod
    def _vmul(a: tuple[float, float], s: float) -> tuple[float, float]:
        return a[0] * s, a[1] * s

    @staticmethod
    def _vlen(a: tuple[float, float]) -> float:
        return math.hypot(a[0], a[1])

    def _vnorm(self, a: tuple[float, float]) -> tuple[float, float]:
        length = self._vlen(a)
        if length <= PLANNER_EPSILON:
            return 0.0, 0.0
        return a[0] / length, a[1] / length

    @staticmethod
    def _vdot(a: tuple[float, float], b: tuple[float, float]) -> float:
        return a[0] * b[0] + a[1] * b[1]

    def _corner_velocity(self, seg1, seg2, max_vel: float, accel: float, corner_factor: float) -> float:
        d1 = self._vnorm(self._vsub(seg1[1], seg1[0]))
        d2 = self._vnorm(self._vsub(seg2[1], seg2[0]))
        cosine = -self._vdot(d1, d2)
        if abs(cosine - 1.0) < PLANNER_EPSILON:
            return 0.0
        sine = math.sqrt(max(0.0, (1.0 - cosine) / 2.0))
        if abs(sine - 1.0) < PLANNER_EPSILON:
            return max_vel
        vel = math.sqrt(max(0.0, (accel * corner_factor * sine) / max(PLANNER_EPSILON, 1.0 - sine)))
        return min(vel, max_vel)

    def _compute_triangle(self, distance, initial_vel, final_vel, accel, p1, p3):
        s1 = (2.0 * accel * distance + final_vel * final_vel - initial_vel * initial_vel) / (4.0 * accel)
        s2 = distance - s1
        v_max = math.sqrt(max(0.0, initial_vel * initial_vel + 2.0 * accel * s1))
        t1 = (v_max - initial_vel) / accel
        t2 = (final_vel - v_max) / -accel
        direction = self._vnorm(self._vsub(p3, p1))
        p2 = self._vadd(p1, self._vmul(direction, s1))
        return s1, s2, t1, t2, v_max, p1, p2, p3

    def _compute_trapezoid(self, distance, initial_vel, max_vel, final_vel, accel, p1, p4):
        t1 = (max_vel - initial_vel) / accel
        s1 = ((max_vel + initial_vel) / 2.0) * t1
        t3 = (final_vel - max_vel) / -accel
        s3 = ((final_vel + max_vel) / 2.0) * t3
        s2 = distance - s1 - s3
        t2 = s2 / max_vel
        direction = self._vnorm(self._vsub(p4, p1))
        p2 = self._vadd(p1, self._vmul(direction, s1))
        p3 = self._vadd(p1, self._vmul(direction, distance - s3))
        return t1, t2, t3, p1, p2, p3, p4

    def _plan_path_blocks(self, points, accel: float, max_vel: float, corner_factor: float = SAXI_CORNER_FACTOR_MM):
        deduped = []
        for p in points:
            pt = (float(p[0]), float(p[1]))
            if not deduped or self._vlen(self._vsub(pt, deduped[-1])) > PLANNER_EPSILON:
                deduped.append(pt)
        if len(deduped) < 2:
            return []

        segments = [{"p1": deduped[i], "p2": deduped[i + 1], "max_entry": 0.0, "entry": 0.0, "blocks": []}
                    for i in range(len(deduped) - 1)]
        for i in range(1, len(segments)):
            segments[i]["max_entry"] = self._corner_velocity(
                (segments[i - 1]["p1"], segments[i - 1]["p2"]),
                (segments[i]["p1"], segments[i]["p2"]),
                max_vel,
                accel,
                corner_factor,
            )

        last = deduped[-1]
        segments.append({"p1": last, "p2": last, "max_entry": 0.0, "entry": 0.0, "blocks": []})

        i = 0
        while i < len(segments) - 1:
            seg = segments[i]
            next_seg = segments[i + 1]
            distance = self._vlen(self._vsub(seg["p2"], seg["p1"]))
            if distance <= PLANNER_EPSILON:
                i += 1
                continue
            v_initial = seg["entry"]
            v_exit = next_seg["max_entry"]
            s1, s2, t1, t2, tri_vmax, p1, p2, p3 = self._compute_triangle(distance, v_initial, v_exit, accel, seg["p1"], seg["p2"])
            if s1 < -PLANNER_EPSILON:
                seg["max_entry"] = math.sqrt(max(0.0, v_exit * v_exit + 2.0 * accel * distance))
                i = max(0, i - 1)
            elif s2 <= 0:
                v_final = math.sqrt(max(0.0, v_initial * v_initial + 2.0 * accel * distance))
                t = (v_final - v_initial) / accel
                seg["blocks"] = [(accel, t, v_initial, seg["p1"], seg["p2"])]
                next_seg["entry"] = v_final
                i += 1
            elif tri_vmax > max_vel:
                zt1, zt2, zt3, zp1, zp2, zp3, zp4 = self._compute_trapezoid(distance, v_initial, max_vel, v_exit, accel, seg["p1"], seg["p2"])
                seg["blocks"] = [
                    (accel, zt1, v_initial, zp1, zp2),
                    (0.0, zt2, max_vel, zp2, zp3),
                    (-accel, zt3, max_vel, zp3, zp4),
                ]
                next_seg["entry"] = v_exit
                i += 1
            else:
                seg["blocks"] = [
                    (accel, t1, v_initial, p1, p2),
                    (-accel, t2, tri_vmax, p2, p3),
                ]
                next_seg["entry"] = v_exit
                i += 1

        return [b for seg in segments for b in seg["blocks"] if b[1] > PLANNER_EPSILON]

    def _modf_floor(self, value: float) -> tuple[float, int]:
        steps = math.floor(value)
        return value - steps, int(steps)

    def _execute_lm_block(self, block) -> bool:
        accel, duration, v_initial, p1, p2 = block
        err_x, steps_x = self._modf_floor((p2[0] - p1[0]) * MICROSTEPS_PER_MM + self._step_error_x)
        err_y, steps_y = self._modf_floor((p2[1] - p1[1]) * MICROSTEPS_PER_MM + self._step_error_y)
        self._step_error_x = err_x
        self._step_error_y = err_y
        if steps_x == 0 and steps_y == 0:
            return True
        return self._try_lm_move_steps(
            steps_x,
            steps_y,
            v_initial * MICROSTEPS_PER_MM,
            max(0.0, (v_initial + accel * duration) * MICROSTEPS_PER_MM),
        )

    def _try_lm_move_steps(self, x_steps: int, y_steps: int, initial_rate: float, final_rate: float) -> bool:
        if not self._ebb_lm_capable:
            speed_mm_s = max(1.0, max(initial_rate, final_rate) / MICROSTEPS_PER_MM)
            return self._xm_move(x_steps / MICROSTEPS_PER_MM, y_steps / MICROSTEPS_PER_MM, speed_mm_s)
        norm = math.hypot(x_steps, y_steps)
        if norm == 0:
            return True
        norm_x = x_steps / norm
        norm_y = y_steps / norm
        initial_rate_x = initial_rate * norm_x
        initial_rate_y = initial_rate * norm_y
        final_rate_x = final_rate * norm_x
        final_rate_y = final_rate * norm_y
        steps_axis1 = x_steps + y_steps
        steps_axis2 = x_steps - y_steps
        ir1, dr1 = self._lm_axis_rates(steps_axis1, abs(initial_rate_x + initial_rate_y), abs(final_rate_x + final_rate_y))
        ir2, dr2 = self._lm_axis_rates(steps_axis2, abs(initial_rate_x - initial_rate_y), abs(final_rate_x - final_rate_y))
        lm = f"LM,{ir1},{steps_axis1},{dr1},{ir2},{steps_axis2},{dr2}"
        with self._serial_lock:
            resp = self._exchange(lm, read_timeout=2.0)
        rlow = resp.lower()
        if "!" in resp or "err" in rlow:
            logger.warning("LM rejected: %r", resp)
            return False
        return True

    def _xm_move(self, dx_mm: float, dy_mm: float, speed_mm_s: float) -> bool:
        sx, sy = self._microsteps_xy(dx_mm, dy_mm)
        distance_mm = math.hypot(dx_mm, dy_mm)
        if distance_mm < 1e-9 and sx == 0 and sy == 0:
            return True
        duration_ms = max(1, int(distance_mm / max(0.01, speed_mm_s) * 1000))
        if sx == 0 and sy == 0 and distance_mm >= 1e-6:
            if abs(dx_mm) >= abs(dy_mm):
                sx = 1 if dx_mm >= 0 else -1
            else:
                sy = 1 if dy_mm >= 0 else -1
        # XM/SM-class moves are addressed in motor-axis microsteps, not XY coordinates.
        axis1_steps, axis2_steps = self._corexy_axes(sx, sy)
        xm = f"XM,{duration_ms},{axis1_steps},{axis2_steps}"
        with self._serial_lock:
            resp = self._exchange(xm, read_timeout=2.0)
        rlow = resp.lower()
        if "!" in resp or "err" in rlow:
            logger.warning("XM rejected: %r", resp)
            return False
        return True

    def _wait_motion_complete(self) -> bool:
        deadline = time.time() + QM_MAX_WAIT_S
        while time.time() < deadline:
            if self._emergency_stop:
                return False
            with self._serial_lock:
                r = self._exchange("QM", read_timeout=0.5)
            parts = [p.strip() for p in r.replace("\n", "").split(",")]
            if len(parts) >= 5:
                try:
                    if parts[1] == "0" and parts[4] == "0":
                        return True
                except IndexError:
                    pass
            time.sleep(QM_POLL_INTERVAL_S)
        logger.warning("QM wait timed out after %.0fs", QM_MAX_WAIT_S)
        return False

    def _wait_while_paused_or_stopped(self) -> bool:
        while True:
            if self._emergency_stop or self._cancel_requested or self.state.plot_state == PlotState.IDLE:
                return False
            with self._pause_lock:
                paused = self._paused
            if not paused:
                return True
            time.sleep(0.05)

    def _move_with_planner(self, dx_mm: float, dy_mm: float, speed_mm_s: float,
                           wait: bool = True, is_draw: bool = False) -> bool:
        """Plan and execute a move using Saxi's accel profile."""
        if abs(dx_mm) < 1e-9 and abs(dy_mm) < 1e-9:
            return True
        if is_draw:
            accel = self._draw_accel_mm_s2()
            cfactor = SAXI_DRAW_CORNER_FACTOR_MM
        else:
            accel = self._travel_accel_mm_s2()
            cfactor = SAXI_TRAVEL_CORNER_FACTOR_MM
        if self._ebb_lm_capable and wait:
            blocks = self._plan_path_blocks(
                [(0.0, 0.0), (dx_mm, dy_mm)],
                accel=accel, max_vel=speed_mm_s, corner_factor=cfactor,
            )
            if blocks:
                saved_ex, saved_ey = self._step_error_x, self._step_error_y
                self._step_error_x = 0.0
                self._step_error_y = 0.0
                for blk in blocks:
                    if not self._execute_lm_block(blk):
                        self._step_error_x = saved_ex
                        self._step_error_y = saved_ey
                        break
                else:
                    return self._wait_motion_complete()
        if not self._xm_move(dx_mm, dy_mm, speed_mm_s):
            return False
        if not wait:
            return True
        return self._wait_motion_complete()

    # ---- queue execution ----

    def _execute_py_command(self, sc: SerialCommand):
        if self._ser is None or not self._ser.is_open:
            if sc.callback:
                sc.callback("error")
            return
        body = sc.command[4:]
        result = "ok"
        try:
            if body == "penup":
                profile = self._active_profile()
                with self._overrides_lock:
                    o = dict(self.live_overrides)
                pu = float(o.get("pen_pos_up", profile.get("pen_pos_up", 60)))
                pos = self.pen_pct_to_pos(pu)
                d = self._pen_motion_duration_ms(True)
                with self._serial_lock:
                    self._s2_pen_to(pos, d)
            elif body == "pendown":
                profile = self._active_profile()
                with self._overrides_lock:
                    o = dict(self.live_overrides)
                pd = float(o.get("pen_pos_down", profile.get("pen_pos_down", 40)))
                pos = self.pen_pct_to_pos(pd)
                d = self._pen_motion_duration_ms(False)
                with self._serial_lock:
                    self._s2_pen_to(pos, d)
            elif body.startswith("pendown_custom:"):
                raw = body.split(":", 1)[1]
                profile = self._active_profile()
                try:
                    pct = max(0.0, min(100.0, float(raw)))
                except (TypeError, ValueError):
                    pct = float(profile.get("tray_pen_down_depth", 30))
                pos = self.pen_pct_to_pos(pct)
                d = self._pen_motion_duration_ms(False)
                with self._serial_lock:
                    self._s2_pen_to(pos, d)
            elif body.startswith("go,"):
                parts = body.split(",")
                rdx, rdy = float(parts[1]), float(parts[2])
                spd = self._speed_mm_s_for_tag(sc.tag)
                if not self._move_with_planner(rdx, rdy, spd):
                    result = "error"
            elif body.startswith("mov,"):
                parts = body.split(",")
                if len(parts) != 3:
                    logger.warning("Bad mov: %s", sc.command)
                    result = "error"
                    return
                x, y = float(parts[1]), float(parts[2])
                cx = float(self.state.current_x)
                cy = float(self.state.current_y)
                if not self._move_with_planner(x - cx, y - cy, self._speed_mm_s_for_tag("travel")):
                    result = "error"
            elif body.startswith("lin_stream,"):
                parts = body.split(",")
                if len(parts) != 3:
                    logger.warning("Bad lin_stream: %s", sc.command)
                    result = "error"
                    return
                x, y = float(parts[1]), float(parts[2])
                cx = float(self.state.current_x)
                cy = float(self.state.current_y)
                if not self._move_with_planner(x - cx, y - cy, self._speed_mm_s_for_tag("draw"), wait=False):
                    self._stream_motion_error = True
                    result = "error"
            elif body.startswith("lin,"):
                parts = body.split(",")
                if len(parts) == 4:
                    sp, xs, ys = parts[1], parts[2], parts[3]
                    x, y = float(xs), float(ys)
                    spd = self._speed_mm_s_for_tag(sc.tag, swirl_sp=float(sp))
                elif len(parts) == 3:
                    x, y = float(parts[1]), float(parts[2])
                    spd = self._speed_mm_s_for_tag(sc.tag)
                else:
                    logger.warning("Bad lin: %s", sc.command)
                    result = "error"
                    return
                cx = float(self.state.current_x)
                cy = float(self.state.current_y)
                if not self._move_with_planner(x - cx, y - cy, spd):
                    result = "error"
            elif body == "wait_motion":
                had_stream_error = self._stream_motion_error
                self._stream_motion_error = False
                if had_stream_error or not self._wait_motion_complete():
                    result = "error"
            else:
                logger.warning("Unknown command: %s", sc.command)
                result = "error"
        except Exception as e:
            result = "error"
            logger.error("Command error (%s): %s", sc.command, e, exc_info=True)
            if self.on_error:
                self.on_error(str(e))
        finally:
            if sc.callback:
                sc.callback(result)

    def _execute_raw_ebb(self, cmd: str):
        payload = cmd[5:].strip().upper()
        if not (payload.startswith("EM") or payload.startswith("SR")):
            logger.warning("Ignoring raw (not EM/SR): %s", cmd)
            return
        with self._serial_lock:
            self._exchange(cmd[5:].strip(), read_timeout=2.0)

    def prepare_plot(self) -> bool:
        """Match Saxi's prePlot: enable motors at 16x and park the pen up."""
        if self._ser is None or not self._ser.is_open:
            return False

    def _pen_up_direct(self) -> None:
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        pu = float(o.get("pen_pos_up", profile.get("pen_pos_up", 60)))
        pos = self.pen_pct_to_pos(pu)
        duration = self._pen_motion_duration_ms(True)
        with self._serial_lock:
            self._s2_pen_to(pos, duration)
        self.state.update(pen_is_down=False)
        self._emergency_stop = False
        self._cancel_requested = False
        with self._pause_lock:
            self._paused = False
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        pen_up_pct = float(o.get("pen_pos_up", profile.get("pen_pos_up", 60)))
        pen_up_pos = self.pen_pct_to_pos(pen_up_pct)
        try:
            with self._serial_lock:
                self._exchange("EM,1,1", read_timeout=2.0)
                if self._ebb_sr_capable:
                    self._exchange("SR,0,1", read_timeout=2.0)
                self._s2_pen_height(pen_up_pos, rate=1000, delay_ms=1000)
            self.state.update(pen_is_down=False)
            return True
        except Exception as e:
            logger.warning("Plot preparation failed: %s", e)
            if self.on_error:
                self.on_error(str(e))
            return False

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq_counter += 1
            return self._seq_counter

    def enqueue(self, cmd: str, priority: Priority = Priority.STREAM,
                callback: Callable = None, tag: str = ""):
        sc = SerialCommand(
            priority=priority.value,
            seq=self._next_seq(),
            command=cmd,
            callback=callback,
            tag=tag,
        )
        self._cmd_queue.put(sc)

    def enqueue_wait(self, cmd: str, priority: Priority = Priority.STREAM,
                     tag: str = "", timeout_s: float = 120.0) -> bool:
        done = threading.Event()
        result = {"ok": False}

        def _cb(resp: str):
            result["ok"] = resp == "ok"
            done.set()

        self.enqueue(cmd, priority, callback=_cb, tag=tag)
        if not done.wait(timeout_s):
            logger.warning("Timed out waiting for command: %s", cmd)
            return False
        return result["ok"]

    def _flush_queue(self):
        while not self._cmd_queue.empty():
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    def _serial_loop(self):
        logger.info("Serial worker started")
        try:
            while self._running:
                if self._emergency_stop:
                    self._flush_queue()
                    time.sleep(0.05)
                    continue
                try:
                    sc = self._cmd_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self._pause_lock:
                    paused = self._paused
                if paused and sc.priority > Priority.EMERGENCY:
                    self._cmd_queue.put(sc)
                    time.sleep(0.05)
                    continue
                try:
                    cmd = sc.command
                    if cmd == "_noop":
                        if sc.callback:
                            sc.callback("")
                    elif cmd.startswith("_py:"):
                        self._execute_py_command(sc)
                    elif cmd.startswith("_raw:"):
                        self._execute_raw_ebb(cmd)
                        if sc.callback:
                            sc.callback("ok")
                    else:
                        if sc.callback:
                            sc.callback("")
                except (serial.SerialException, OSError) as e:
                    logger.error("Serial error: %s", e)
                    if self.on_error:
                        self.on_error(str(e))
                    self._running = False
                    break
                except Exception as e:
                    logger.error("Worker error: %s", e, exc_info=True)
                    if self.on_error:
                        self.on_error(str(e))
        finally:
            logger.info("Serial worker stopped")
            if self.state.connected:
                self._schedule_disconnect()

    def _button_poll_loop(self):
        logger.info("Button poll started")
        while self._running:
            try:
                if self._ser is None or not self._ser.is_open:
                    time.sleep(0.1)
                    continue
                if self.state.plot_state in (PlotState.PLOTTING, PlotState.PAUSED, PlotState.DIPPING):
                    time.sleep(0.1)
                    continue
                with self._serial_lock:
                    self._write_line("QB")
                    text = self._read_response_until_ok(timeout=0.35)
                first_line = text.splitlines()[0].strip() if text else ""
                if first_line == "1":
                    self._handle_button_press()
            except Exception as e:
                logger.debug("Button poll: %s", e)
            time.sleep(0.05)
        logger.info("Button poll stopped")

    def _handle_button_press(self):
        with self._pause_lock:
            was_paused = self._paused
        if was_paused:
            self.resume()
        else:
            if self.state.plot_state == PlotState.PLOTTING:
                self.pause()
        if self.on_button_press:
            self.on_button_press(not was_paused)

    def pause(self):
        with self._pause_lock:
            self._paused = True
        self.state.update(plot_state=PlotState.PAUSED)
        try:
            self._pen_up_direct()
        except Exception as e:
            logger.warning("Pause pen-up failed: %s", e)
        self._notify_state()

    def resume(self):
        with self._pause_lock:
            self._paused = False
        self._emergency_stop = False
        if self.state.plot_state == PlotState.PAUSED:
            self.state.update(plot_state=PlotState.PLOTTING)
        self._notify_state()

    def emergency_stop(self):
        self._cancel_requested = True
        self._emergency_stop = False
        with self._pause_lock:
            self._paused = False
        self._flush_queue()
        try:
            self._pen_up_direct()
        except Exception as e:
            logger.warning("Controlled stop pen-up failed: %s", e)
        self.state.update(plot_state=PlotState.IDLE, position_known=True)
        self._notify_state()
        logger.warning("CONTROLLED STOP REQUESTED")

    def pen_up(self):
        self.enqueue("_py:penup", Priority.MANUAL, tag="pen_up")
        self.state.update(pen_is_down=False)

    def pen_down(self):
        self.enqueue("_py:pendown", Priority.MANUAL, tag="pen_down")
        self.state.update(pen_is_down=True)

    def pen_up_sync(self) -> bool:
        ok = self.enqueue_wait("_py:penup", Priority.MANUAL, tag="pen_up")
        if ok:
            self.state.update(pen_is_down=False)
        return ok

    def pen_down_sync(self) -> bool:
        ok = self.enqueue_wait("_py:pendown", Priority.MANUAL, tag="pen_down")
        if ok:
            self.state.update(pen_is_down=True)
        return ok

    def pen_toggle(self):
        if self.state.pen_is_down:
            self.pen_up()
        else:
            self.pen_down()

    def enable_motors(self):
        self._emergency_stop = False
        if self.state.plot_state != PlotState.PLOTTING:
            self._cancel_requested = False
        with self._pause_lock:
            self._paused = False
        self.enqueue("_raw:EM,1,1", Priority.MANUAL)
        if self._ebb_sr_capable:
            self.enqueue("_raw:SR,0,1", Priority.MANUAL)

    def disable_motors(self):
        self.enqueue("_raw:EM,0,0", Priority.MANUAL)
        if self._ebb_sr_capable:
            self.enqueue("_raw:SR,60000,0", Priority.MANUAL)

    def set_home(self):
        self.state.update(current_x=0.0, current_y=0.0, position_known=True)
        self._commanded_x = 0.0
        self._commanded_y = 0.0

    def walk_home(self) -> Optional[str]:
        if not self.state.position_known:
            return "Position unknown after E-stop. Jog to the physical home point, then click Set Home."
        self._emergency_stop = False
        if self.state.plot_state != PlotState.PLOTTING:
            self._cancel_requested = False
        with self._pause_lock:
            self._paused = False
        self.enable_motors()
        cx = float(self.state.current_x)
        cy = float(self.state.current_y)
        if abs(cx) < 0.001 and abs(cy) < 0.001:
            self.state.update(current_x=0.0, current_y=0.0)
            self._commanded_x = 0.0
            self._commanded_y = 0.0
            return None
        self._commanded_x = 0.0
        self._commanded_y = 0.0
        self._step_error_x = 0.0
        self._step_error_y = 0.0

        def _home_cb(resp):
            if resp == "ok":
                self.state.update(current_x=0.0, current_y=0.0)

        self.enqueue(
            f"_py:go,{-cx:.6f},{-cy:.6f}",
            Priority.MANUAL,
            callback=_home_cb,
            tag="travel",
        )
        return None

    def jog(self, dx_mm: float, dy_mm: float) -> Optional[str]:
        self._emergency_stop = False
        if self.state.plot_state != PlotState.PLOTTING:
            self._cancel_requested = False
        with self._pause_lock:
            self._paused = False
        if getattr(self, "is_connected", False) or not self.state.connected:
            if not self.state.connected:
                return "Not connected"
        try:
            bed_w = float(self.config.get("bed_width_mm", 300) or 300)
            bed_h = float(self.config.get("bed_height_mm", 218) or 218)
        except (TypeError, ValueError):
            bed_w, bed_h = 300.0, 218.0

        if not self.state.position_known:
            self.enqueue(
                f"_py:go,{float(dx_mm):.6f},{float(dy_mm):.6f}",
                Priority.MANUAL,
                tag="travel",
            )
            return None
            
        if self._cmd_queue.empty() and self.state.plot_state == PlotState.IDLE:
            self._commanded_x = float(self.state.current_x)
            self._commanded_y = float(self.state.current_y)
            
        cx = self._commanded_x
        cy = self._commanded_y
        target_x = max(0.0, min(cx + dx_mm, bed_w))
        target_y = max(0.0, min(cy + dy_mm, bed_h))
        rdx = target_x - cx
        rdy = target_y - cy
        
        if abs(rdx) < 0.001 and abs(rdy) < 0.001:
            return None
            
        self._commanded_x = target_x
        self._commanded_y = target_y

        def _cb(resp, nx=target_x, ny=target_y):
            if resp == "ok":
                self.state.update(current_x=nx, current_y=ny)

        self.enqueue(
            f"_py:go,{rdx:.6f},{rdy:.6f}",
            Priority.MANUAL,
            callback=_cb,
            tag="travel",
        )
        return None

    def _move_to(self, x_mm: float, y_mm: float, travel: bool = True,
                 priority: Priority = Priority.STREAM, pen_down_after: bool = False,
                 wait: bool = False) -> bool:
        err = self._check_soft_limits(x_mm, y_mm)
        if err:
            logger.warning("Soft limit: %s", err)
            if self.on_error:
                self.on_error(err)
            return False
        dx = x_mm - self.state.current_x
        dy = y_mm - self.state.current_y
        dist_mm = math.hypot(dx, dy)
        if dist_mm < 0.001:
            return True
        tag = "travel" if travel else "draw"
        kind = "mov" if travel else "lin"

        def _upd(resp, nx=x_mm, ny=y_mm, d=dist_mm, t=travel):
            if resp == "ok":
                self.state.update(current_x=nx, current_y=ny)
                if not t:
                    self.state.add_distance(d)

        cmd = f"_py:{kind},{x_mm:.6f},{y_mm:.6f}"
        if wait:
            ok = self.enqueue_wait(cmd, priority, tag=tag)
            if ok:
                _upd("ok")
            return ok
        self.enqueue(cmd, priority, callback=_upd, tag=tag)
        return True

    def move_to_and_draw(self, x_mm: float, y_mm: float, priority: Priority = Priority.STREAM):
        self._move_to(x_mm, y_mm, travel=False, priority=priority)

    def rapid_move(self, x_mm: float, y_mm: float, priority: Priority = Priority.STREAM):
        self._move_to(x_mm, y_mm, travel=True, priority=priority)

    def move_to_and_draw_sync(self, x_mm: float, y_mm: float, priority: Priority = Priority.STREAM) -> bool:
        return self._move_to(x_mm, y_mm, travel=False, priority=priority, wait=True)

    def rapid_move_sync(self, x_mm: float, y_mm: float, priority: Priority = Priority.STREAM) -> bool:
        return self._move_to(x_mm, y_mm, travel=True, priority=priority, wait=True)

    def move_to_and_draw_stream(self, x_mm: float, y_mm: float, priority: Priority = Priority.STREAM) -> bool:
        err = self._check_soft_limits(x_mm, y_mm)
        if err:
            logger.warning("Soft limit: %s", err)
            if self.on_error:
                self.on_error(err)
            return False

        cx = float(self.state.current_x)
        cy = float(self.state.current_y)
        dist_mm = math.hypot(x_mm - cx, y_mm - cy)
        if dist_mm < 0.001:
            return True

        def _upd(resp, nx=x_mm, ny=y_mm, d=dist_mm):
            if resp == "ok":
                self.state.update(current_x=nx, current_y=ny)
                self.state.add_distance(d)

        self.enqueue(
            f"_py:lin_stream,{x_mm:.6f},{y_mm:.6f}",
            priority,
            callback=_upd,
            tag="draw",
        )
        return True

    def wait_motion_sync(self, priority: Priority = Priority.STREAM) -> bool:
        return self.enqueue_wait("_py:wait_motion", priority, tag="wait_motion")

    def draw_path_sync(self, points, priority: Priority = Priority.STREAM) -> bool:
        clean_points = [(float(p[0]), float(p[1])) for p in points]
        if len(clean_points) < 2:
            return True
        for x, y in clean_points:
            err = self._check_soft_limits(x, y)
            if err:
                logger.warning("Soft limit: %s", err)
                if self.on_error:
                    self.on_error(err)
                return False

        # Key fix: simplify path before planning (Douglas-Peucker).
        # SVG paths sampled at 0.5 mm create thousands of near-collinear
        # points that cause constant accel/decel = buzz.
        clean_points = self._simplify_path(clean_points, PATH_SIMPLIFY_TOLERANCE_MM)
        if len(clean_points) < 2:
            return True

        speed = self._speed_mm_s_for_tag("draw")
        accel = self._draw_accel_mm_s2()
        blocks = self._plan_path_blocks(
            clean_points, accel=accel, max_vel=speed,
            corner_factor=SAXI_DRAW_CORNER_FACTOR_MM,
        )
        if not blocks:
            return True

        distance = 0.0
        for idx in range(1, len(clean_points)):
            distance += math.hypot(
                clean_points[idx][0] - clean_points[idx - 1][0],
                clean_points[idx][1] - clean_points[idx - 1][1],
            )

        self._step_error_x = 0.0
        self._step_error_y = 0.0
        fifo_check_interval = 8
        last_x, last_y = clean_points[0]
        completed_distance = 0.0
        for idx, block in enumerate(blocks):
            if self._cancel_requested:
                self._wait_motion_complete()
                self.state.update(current_x=last_x, current_y=last_y)
                self._commanded_x = last_x
                self._commanded_y = last_y
                self.state.add_distance(completed_distance)
                return True
            if not self._wait_while_paused_or_stopped():
                return False
            if not self._execute_lm_block(block):
                return False
            last_x, last_y = block[4]
            completed_distance += math.hypot(block[4][0] - block[3][0], block[4][1] - block[3][1])
            if self._emergency_stop:
                return False
            if self._cancel_requested:
                self._wait_motion_complete()
                self.state.update(current_x=last_x, current_y=last_y)
                self._commanded_x = last_x
                self._commanded_y = last_y
                self.state.add_distance(completed_distance)
                return True
            if idx % fifo_check_interval == (fifo_check_interval - 1) and idx < len(blocks) - 1:
                self._wait_for_fifo_space()
        if not self._wait_motion_complete():
            return False

        end_x, end_y = clean_points[-1]
        self.state.update(current_x=end_x, current_y=end_y)
        self._commanded_x = end_x
        self._commanded_y = end_y
        self.state.add_distance(distance)
        return True

    def _wait_for_fifo_space(self, max_fifo: int = 3) -> None:
        """Wait until the EBB FIFO has space."""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._emergency_stop:
                return
            with self._serial_lock:
                r = self._exchange("QM", read_timeout=0.5)
            parts = [p.strip() for p in r.replace("\n", "").split(",")]
            if len(parts) >= 5:
                try:
                    fifo_count = int(parts[4])
                    if fifo_count <= max_fifo:
                        return
                except (ValueError, IndexError):
                    return
            time.sleep(0.01)

    def _check_soft_limits(self, x: float, y: float) -> Optional[str]:
        try:
            bed_w = float(self.config.get("bed_width_mm", 300) or 300)
            bed_h = float(self.config.get("bed_height_mm", 218) or 218)
        except (TypeError, ValueError):
            bed_w, bed_h = 300.0, 218.0
        if x < -1 or y < -1 or x > bed_w + 1 or y > bed_h + 1:
            return f"Move to ({x:.1f}, {y:.1f}) exceeds bed limits ({bed_w}x{bed_h}mm)"
        return None

    def execute_dip(self, return_x: float, return_y: float):
        self.state.update(plot_state=PlotState.DIPPING)
        self._notify_state()
        profile = self._active_profile()
        tray_x = self.config.get("tray_x_mm", 0)
        tray_y = self.config.get("tray_y_mm", 0)
        tray_depth = profile.get("tray_pen_down_depth", 30)
        swirl_d = profile.get("swirl_diameter_mm", 8)
        swirl_n = profile.get("swirl_count", 3)
        swirl_speed = profile.get("swirl_speed", 25)

        self.pen_up()
        self.rapid_move(tray_x, tray_y, Priority.MANUAL)
        self.enqueue(f"_py:pendown_custom:{tray_depth}", Priority.MANUAL, tag="tray_down")
        self._enqueue_swirl(tray_x, tray_y, swirl_d, swirl_n, swirl_speed)
        self.pen_up()

        water_x = self.config.get("water_dish_x_mm")
        water_y = self.config.get("water_dish_y_mm")
        if water_x is not None and water_y is not None:
            self.rapid_move(water_x, water_y, Priority.MANUAL)
            self.enqueue(f"_py:pendown_custom:{tray_depth}", Priority.MANUAL, tag="tray_down")
            self._enqueue_swirl(water_x, water_y, swirl_d, 2, swirl_speed)
            self.pen_up()

        self.rapid_move(return_x, return_y, Priority.MANUAL)
        self.state.update(
            plot_state=PlotState.PLOTTING,
            dip_count=self.state.dip_count + 1,
        )
        self.state.reset_session_distance()
        self._notify_state()

    def _enqueue_swirl(self, cx: float, cy: float, diameter: float,
                       count: int, speed_pct: float):
        r = diameter / 2.0
        segments = 24
        try:
            sp = int(speed_pct)
        except (TypeError, ValueError):
            sp = 25
        last_x = self.state.current_x
        last_y = self.state.current_y
        for _ in range(count):
            for i in range(segments + 1):
                angle = 2.0 * math.pi * i / segments
                tx = cx + r * math.cos(angle)
                ty = cy + r * math.sin(angle)
                if math.hypot(tx - last_x, ty - last_y) < 0.01:
                    continue
                last_x, last_y = tx, ty

                def _cb(_r, nx=tx, ny=ty):
                    self.state.update(current_x=nx, current_y=ny)

                self.enqueue(
                    f"_py:lin,{sp},{tx:.6f},{ty:.6f}",
                    Priority.MANUAL,
                    callback=_cb,
                    tag="swirl",
                )

    def bounding_box_test(self, min_x: float, min_y: float, max_x: float, max_y: float) -> Optional[str]:
        err = self._check_soft_limits(min_x, min_y)
        if not err:
            err = self._check_soft_limits(max_x, max_y)
        if err:
            return err
        self.pen_up()
        self.rapid_move(min_x, min_y, Priority.MANUAL)
        self.rapid_move(max_x, min_y, Priority.MANUAL)
        self.rapid_move(max_x, max_y, Priority.MANUAL)
        self.rapid_move(min_x, max_y, Priority.MANUAL)
        self.rapid_move(min_x, min_y, Priority.MANUAL)
        self.rapid_move(0, 0, Priority.MANUAL)
        return None

    def _schedule_disconnect(self):
        logger.warning("Connection lost")
        self._running = False
        self._close_serial()
        self._flush_queue()
        self.state.update(
            connected=False,
            plot_state=PlotState.IDLE,
            serial_port="",
            firmware_version="",
        )
        self._notify_state()

    def _notify_state(self):
        if self.on_state_change:
            self.on_state_change()

    def set_live_override(self, key: str, value):
        with self._overrides_lock:
            self.live_overrides[key] = value

    def clear_live_overrides(self):
        with self._overrides_lock:
            self.live_overrides.clear()

    @property
    def is_paused(self) -> bool:
        with self._pause_lock:
            return self._paused

    @property
    def is_connected(self) -> bool:
        return self.state.connected
