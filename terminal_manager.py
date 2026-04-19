import asyncio
import logging
import os
import struct
import threading
from typing import Optional

logger = logging.getLogger("strata.terminal")

try:
    import pty
    import fcntl
    import termios
    HAS_PTY = True
except ImportError:
    HAS_PTY = False
    logger.info("pty module not available (Windows) — terminal feature disabled")


class TerminalManager:
    """
    Manages a PTY-based shell for the xterm.js terminal.
    Only functional on Linux/macOS where pty module is available.
    """

    def __init__(self):
        self._fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._running = False
        self._read_thread: Optional[threading.Thread] = None
        self._on_output = None  # callback(data: bytes)

    @property
    def is_available(self) -> bool:
        return HAS_PTY

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, on_output=None, cols: int = 120, rows: int = 40) -> bool:
        if not HAS_PTY:
            return False
        if self._running:
            return True

        self._on_output = on_output
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("/bin/bash", ["/bin/bash"])
        else:
            self._pid = pid
            self._fd = fd
            self._set_winsize(cols, rows)
            self._running = True
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="terminal-reader"
            )
            self._read_thread.start()
            logger.info("Terminal started (pid=%d)", pid)
            return True

    def stop(self):
        self._running = False
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._pid:
            try:
                os.kill(self._pid, 9)
                os.waitpid(self._pid, os.WNOHANG)
            except (OSError, ChildProcessError):
                pass
            self._pid = None
        logger.info("Terminal stopped")

    def write(self, data: str):
        if self._fd is not None:
            try:
                os.write(self._fd, data.encode("utf-8"))
            except OSError as e:
                logger.error("Terminal write error: %s", e)

    def resize(self, cols: int, rows: int):
        if self._fd is not None:
            self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int):
        if not HAS_PTY or self._fd is None:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            logger.debug("Failed to set terminal size: %s", e)

    def _read_loop(self):
        while self._running and self._fd is not None:
            try:
                data = os.read(self._fd, 4096)
                if data and self._on_output:
                    self._on_output(data)
                elif not data:
                    break
            except OSError:
                break
        self._running = False
        logger.info("Terminal read loop ended")
