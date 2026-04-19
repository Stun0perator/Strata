import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PlotState(str, Enum):
    IDLE = "idle"
    PLOTTING = "plotting"
    PAUSED = "paused"
    DIPPING = "dipping"
    PEN_SWAP = "pen_swap"


@dataclass
class PlotterState:
    current_x: float = 0.0
    current_y: float = 0.0
    session_distance: float = 0.0
    total_distance: float = 0.0
    plot_state: PlotState = PlotState.IDLE
    current_layer: str = ""
    current_layer_index: int = 0
    total_layers: int = 0
    current_path_index: int = 0
    total_paths: int = 0
    total_plot_distance: float = 0.0
    distance_plotted: float = 0.0
    dip_count: int = 0
    estimated_dips_remaining: int = 0
    pen_is_down: bool = False
    connected: bool = False
    firmware_version: str = ""
    serial_port: str = ""
    current_file: str = ""
    plot_start_time: Optional[float] = None
    errors: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k) and k != "_lock":
                    setattr(self, k, v)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "current_x": round(self.current_x, 2),
                "current_y": round(self.current_y, 2),
                "session_distance": round(self.session_distance, 2),
                "total_distance": round(self.total_distance, 2),
                "plot_state": self.plot_state.value,
                "current_layer": self.current_layer,
                "current_layer_index": self.current_layer_index,
                "total_layers": self.total_layers,
                "current_path_index": self.current_path_index,
                "total_paths": self.total_paths,
                "total_plot_distance": round(self.total_plot_distance, 2),
                "distance_plotted": round(self.distance_plotted, 2),
                "dip_count": self.dip_count,
                "estimated_dips_remaining": self.estimated_dips_remaining,
                "pen_is_down": self.pen_is_down,
                "connected": self.connected,
                "firmware_version": self.firmware_version,
                "serial_port": self.serial_port,
                "current_file": self.current_file or "",
                "plot_start_time": self.plot_start_time,
            }

    def add_distance(self, mm: float):
        with self._lock:
            self.session_distance += mm
            self.total_distance += mm
            self.distance_plotted += mm

    def reset_session_distance(self):
        with self._lock:
            self.session_distance = 0.0

    def reset_for_new_plot(self, total_distance: float, total_paths: int, total_layers: int):
        with self._lock:
            self.total_plot_distance = total_distance
            self.distance_plotted = 0.0
            self.total_paths = total_paths
            self.total_layers = total_layers
            self.current_path_index = 0
            self.current_layer_index = 0
            self.dip_count = 0
            self.session_distance = 0.0
            self.plot_start_time = time.time()
            self.errors = []

    def move_to(self, x: float, y: float):
        """Update position and accumulate travel distance if pen is down."""
        with self._lock:
            dx = x - self.current_x
            dy = y - self.current_y
            dist = (dx * dx + dy * dy) ** 0.5
            if self.pen_is_down:
                self.session_distance += dist
                self.total_distance += dist
                self.distance_plotted += dist
            self.current_x = x
            self.current_y = y
