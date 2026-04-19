# Strata — Plotter Control System

Web-based control interface for an AxiDraw pen plotter running headlessly on a Le Potato SBC. Access the full UI via browser from any device on the local network.

## Quick Start (Le Potato)

```bash
# Install system dependencies
sudo apt update && sudo apt install python3-pip python3-venv

# Clone/copy project to /home/pi/strata
cd /home/pi/strata

# Create virtual environment and install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install vpype

# Create upload/recording directories
mkdir -p /home/pi/uploads /home/pi/recordings /home/pi/plot_finished

# Run
python3 main.py
```

Open `http://<le-potato-ip>:8080` in your browser.

## Systemd Service (Auto-Start)

```bash
sudo cp strata.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable strata
sudo systemctl start strata
```

## Architecture

- **Backend:** FastAPI with native WebSockets (no Flask)
- **Serial I/O:** Dedicated thread with 3-level priority queue (Emergency > Manual > Stream)
- **Button Polling:** Dedicated thread, polls EBB `QB` command every 50ms
- **Webcam:** Dedicated capture thread with OpenCV, MJPEG stream
- **Frontend:** Single-page HTML/CSS/JS, no build step
- **Terminal:** xterm.js + PTY for full shell access
- **Persistence:** `config.json`, `profiles.json`, SQLite `plot_history.db`

## File Structure

```
main.py               FastAPI application and plot execution engine
serial_manager.py     EBB serial protocol, priority queue, threads
plotter_state.py      Shared plotter state (position, status, telemetry)
config_manager.py     config.json and profiles.json management
svg_processor.py      SVG parsing, layers, vpype, shapely path splitting
webcam_manager.py     OpenCV capture, MJPEG streaming, recording
plot_history.py       SQLite plot history
terminal_manager.py   PTY terminal for xterm.js
static/index.html     Complete single-page frontend
config.json           Machine and app configuration
profiles.json         Pen/brush preset profiles
strata.service        systemd unit file
```

## UI Tabs

1. **Dashboard** — Connection, live telemetry, progress
2. **Manual Controls** — Pen, jog pad, bounding box test, dry run, motors
3. **Plot Settings** — Speed, height, acceleration sliders with live override; profile management
4. **File & Execution** — SVG upload, preview, vpype optimization, layer editor, plot queue, start/pause/stop
5. **Paint Mode** — Dip threshold, swirl settings, tray calibration, brush pressure taper
6. **Webcam** — MJPEG live feed, recording, downloads
7. **Terminal** — Full interactive shell via xterm.js
8. **History** — SQLite plot log with settings reload and re-queue
