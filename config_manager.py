import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
PROFILES_PATH = BASE_DIR / "profiles.json"


class ConfigManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._config = self._load_file(CONFIG_PATH, self._default_config())
        self._profiles = self._load_file(PROFILES_PATH, self._default_profiles())
        self._ensure_dirs()

    # --- defaults ---

    @staticmethod
    def _default_config() -> dict:
        return {
            "bed_width_mm": 300,
            "bed_height_mm": 218,
            "steps_per_mm": 80,
            "servo_min": 7500,
            "servo_max": 28000,
            "tray_x_mm": 0,
            "tray_y_mm": 0,
            "water_dish_x_mm": None,
            "water_dish_y_mm": None,
            "auto_home_on_connect": True,
            "motor_hold_during_swap": True,
            "swap_timeout_warning_minutes": 5,
            "last_serial_port": None,
            "last_model": 1,
            "last_profile": "Default",
            "plot_queue": [],
            "upload_dir": "uploads",
            "recording_dir": "/home/pi/recordings",
            "finished_dir": "/home/pi/plot_finished",
        }

    @staticmethod
    def _default_profiles() -> dict:
        return {
            "Default": {
                "speed_pendown": 25,
                "speed_penup": 75,
                "pen_pos_down": 40,
                "pen_pos_up": 60,
                "accel": 75,
                "pen_rate_lower": 50,
                "pen_rate_raise": 75,
                "model": 1,
                "const_speed": False,
                "paint_enabled": False,
                "dip_threshold_mm": 80,
                "swirl_diameter_mm": 8,
                "swirl_count": 3,
                "swirl_speed": 25,
                "tray_pen_down_depth": 30,
                "pressure_taper_enabled": False,
                "pressure_taper_mm": 2,
            }
        }

    @staticmethod
    def _load_file(path: Path, defaults: dict) -> dict:
        if path.exists():
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                merged = {**defaults, **data}
                return merged
            except (json.JSONDecodeError, IOError):
                pass
        return dict(defaults)

    def _save_config(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self._config, f, indent=2)

    def _save_profiles(self):
        with open(PROFILES_PATH, "w") as f:
            json.dump(self._profiles, f, indent=2)

    def _ensure_dirs(self):
        for key in ("upload_dir", "recording_dir", "finished_dir"):
            d = self._config.get(key)
            if d:
                os.makedirs(d, exist_ok=True)

    # --- config accessors ---

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._config.get(key, default)

    def set(self, key: str, value: Any):
        with self._lock:
            self._config[key] = value
            self._save_config()

    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def update_config(self, data: dict):
        with self._lock:
            self._config.update(data)
            self._save_config()

    # --- profile accessors ---

    def get_profiles(self) -> dict:
        with self._lock:
            return dict(self._profiles)

    def get_profile(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._profiles.get(name)

    def save_profile(self, name: str, data: dict):
        with self._lock:
            self._profiles[name] = data
            self._save_profiles()

    def delete_profile(self, name: str) -> bool:
        with self._lock:
            if name in self._profiles:
                del self._profiles[name]
                self._save_profiles()
                return True
            return False

    def duplicate_profile(self, src: str, dest: str) -> bool:
        with self._lock:
            if src in self._profiles:
                self._profiles[dest] = dict(self._profiles[src])
                self._save_profiles()
                return True
            return False
