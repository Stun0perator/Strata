import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger("strata.history")

DB_PATH = os.path.join(os.path.dirname(__file__), "plot_history.db")


class PlotHistory:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    profile_name TEXT,
                    profile_data TEXT,
                    layers_plotted TEXT,
                    duration_seconds REAL,
                    total_distance_mm REAL,
                    dip_count INTEGER DEFAULT 0,
                    errors TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

    def log_plot(self, filename: str, profile_name: str, profile_data: dict,
                 layers: list[str], duration_seconds: float, distance_mm: float,
                 dip_count: int, errors: list[str]) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            cur = conn.execute(
                """INSERT INTO plots
                   (filename, profile_name, profile_data, layers_plotted,
                    duration_seconds, total_distance_mm, dip_count, errors, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filename,
                    profile_name,
                    json.dumps(profile_data),
                    json.dumps(layers),
                    round(duration_seconds, 1),
                    round(distance_mm, 1),
                    dip_count,
                    json.dumps(errors),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            row_id = cur.lastrowid
            conn.close()
            logger.info("Logged plot #%d: %s", row_id, filename)
            return row_id

    def get_all(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM plots ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            conn.close()
        return [self._row_to_dict(r) for r in rows]

    def get_by_id(self, plot_id: int) -> Optional[dict]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM plots WHERE id = ?", (plot_id,)).fetchone()
            conn.close()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        for key in ("profile_data", "layers_plotted", "errors"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
