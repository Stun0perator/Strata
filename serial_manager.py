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
MAX_SPEED_MM_S = 110.0
MAX_ACCEL_MM_S2 = 800.0

# EBB rate scale (saxi ebb.ts)
_LM_RATE_SCALE = 0x80000000 / 25000.0

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

    def _pct_to_speed_mm_s(self, pct: float) -> float:
        return max(1.0, (pct / 100.0) * MAX_SPEED_MM_S)

    def _speed_mm_s_for_tag(self, tag: str, swirl_sp: Optional[float] = None) -> float:
        profile = self._active_profile()
        with self._overrides_lock:
            o = dict(self.live_overrides)
        if swirl_sp is not None:
            return self._pct_to_speed_mm_s(float(swirl_sp))
        if tag in ("draw", "swirl"):
            pct = float(o.get("speed_pendown", profile.get("speed_pendown", 25)))
        else:
            pct = float(o.get("speed_penup", profile.get("speed_penup", 75)))
        return self._pct_to_speed_mm_s(pct)

    def _move_time_trapezoid_s(self, distance_mm: float) -> float:
        if distance_mm <= 1e-9:
            return 0.0
        v_max = min(MAX_SPEED_MM_S, 200.0)
        a = MAX_ACCEL_MM_S2
        t_acc = v_max / a
        d_acc = 0.5 * a * t_acc * t_acc
        if 2.0 * d_acc >= distance_mm:
            return 2.0 * math.sqrt(distance_mm / a)
        return 2.0 * t_acc + (distance_mm - 2.0 * d_acc) / v_max

    def _lm_axis_rates(self, steps: int, initial_steps_per_sec: float, final_steps_per_sec: float) -> Tuple[int, int]:
        """
        Saxi-style LM: initialRate, deltaR per axis.
        """
        if steps == 0:
            return 0, 0
        initial_rate = round(initial_steps_per_sec * _LM_RATE_SCALE)
        final_rate = round(final_steps_per_sec * _LM_RATE_SCALE)
        move_time = (2.0 * abs(steps)) / (initial_steps_per_sec + final_steps_per_sec) if (initial_steps_per_sec + final_steps_per_sec) != 0 else 0
        if move_time == 0:
            return 0, 0
        delta_r = round((final_rate - initial_rate) / (move_time * 25000.0))
        return int(initial_rate), int(delta_r)

    def _try_lm_move(self, dx_mm: float, dy_mm: float, speed_mm_s: float) -> bool:
        if not self._ebb_lm_capable:
            return False
        
        x_steps, y_steps = self._microsteps_xy(dx_mm, dy_mm)
        if x_steps == 0 and y_steps == 0:
            return True

        distance_mm = math.hypot(dx_mm, dy_mm)
        move_time_s = self._move_time_trapezoid_s(distance_mm)
        if move_time_s <= 0:
            move_time_s = max(1e-3, distance_mm / max(1.0, speed_mm_s))

        initial_rate = 0.0
        final_rate = (2.0 * distance_mm / move_time_s) * MICROSTEPS_PER_MM

        norm = math.hypot(x_steps, y_steps)
        if norm == 0:
            return True
        norm_x = x_steps / norm
        norm_y = y_steps / norm
        
        initial_rate_x = initial_rate * norm_x
        initial_rate_y = initial_rate * norm_y
        final_rate_x = final_rate * norm_x
        final_rate_y = final_rate * norm_y
        
        initial_rate_axis1 = abs(initial_rate_x + initial_rate_y)
        initial_rate_axis2 = abs(initial_rate_x - initial_rate_y)
        final_rate_axis1 = abs(final_rate_x + final_rate_y)
        final_rate_axis2 = abs(final_rate_x - final_rate_y)
        
        steps_axis1 = x_steps + y_steps
        steps_axis2 = x_steps - y_steps
        
        ir1, dr1 = self._lm_axis_rates(steps_axis1, initial_rate_axis1, final_rate_axis1)
        ir2, dr2 = self._lm_axis_rates(steps_axis2, initial_rate_axis2, final_rate_axis2)

        lm = f"LM,{ir1},{steps_axis1},{dr1},{ir2},{steps_axis2},{dr2}"
        with self._serial_lock:
            resp = self._exchange(lm, read_timeout=2.0)
        rlow = resp.lower()
        if "!" in resp or "err" in rlow:
            logger.warning("LM rejected: %r — XM fallback", resp)
            return False
        if "ok" in rlow:
            return True
        if not resp.strip():
            return True
        logger.warning("LM unclear response %r — XM fallback", resp)
        return False

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
            with self._serial_lock:
                r = self._exchange("QM", read_timeout=0.5)
            parts = [p.strip() for p in r.replace("\n", "").split(",")]
            if len(parts) >= 5 and parts[0].upper() == "QM":
                try:
                    if parts[1] == "0" and parts[4] == "0":
                        return True
                except IndexError:
                    pass
            time.sleep(QM_POLL_INTERVAL_S)
        logger.warning("QM wait timed out after %.0fs", QM_MAX_WAIT_S)
        return False

    def _move_with_planner(self, dx_mm: float, dy_mm: float, speed_mm_s: float, wait: bool = True) -> bool:
        if abs(dx_mm) < 1e-9 and abs(dy_mm) < 1e-9:
            return True
        used_lm = self._try_lm_move(dx_mm, dy_mm, speed_mm_s) if wait else False
        if not used_lm:
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
                            sc.callback("")
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
        self._notify_state()

    def resume(self):
        with self._pause_lock:
            self._paused = False
        if self.state.plot_state == PlotState.PAUSED:
            self.state.update(plot_state=PlotState.PLOTTING)
        self._notify_state()

    def emergency_stop(self):
        self._emergency_stop = True
        with self._pause_lock:
            self._paused = True
        self._flush_queue()
        self.state.update(plot_state=PlotState.IDLE)
        self._emergency_stop = False
        with self._pause_lock:
            self._paused = False
        self.pen_up()
        self._notify_state()
        logger.warning("EMERGENCY STOP")

    def pen_up(self):
        self.enqueue("_py:penup", Priority.MANUAL, tag="pen_up")
        self.state.update(pen_is_down=False)

    def pen_down(self):
        self.enqueue("_py:pendown", Priority.MANUAL, tag="pen_down")
        self.state.update(pen_is_down=True)

    def pen_toggle(self):
        if self.state.pen_is_down:
            self.pen_up()
        else:
            self.pen_down()

    def enable_motors(self):
        self.enqueue("_raw:EM,1,1", Priority.MANUAL)
        if self._ebb_sr_capable:
            self.enqueue("_raw:SR,0,1", Priority.MANUAL)

    def disable_motors(self):
        self.enqueue("_raw:EM,0,0", Priority.MANUAL)
        if self._ebb_sr_capable:
            self.enqueue("_raw:SR,60000,0", Priority.MANUAL)

    def set_home(self):
        self.state.update(current_x=0.0, current_y=0.0)
        self._commanded_x = 0.0
        self._commanded_y = 0.0

    def walk_home(self):
        self._move_to(0.0, 0.0, travel=True, priority=Priority.MANUAL)

    def jog(self, dx_mm: float, dy_mm: float) -> Optional[str]:
        if getattr(self, "is_connected", False) or not self.state.connected:
            if not self.state.connected:
                return "Not connected"
        try:
            bed_w = float(self.config.get("bed_width_mm", 300) or 300)
            bed_h = float(self.config.get("bed_height_mm", 218) or 218)
        except (TypeError, ValueError):
            bed_w, bed_h = 300.0, 218.0
            
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
