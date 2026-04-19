import asyncio
import json
import logging
import os
import shutil
import socket

import aiofiles
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import webbrowser

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config_manager import ConfigManager
from plotter_state import PlotterState, PlotState
from serial_manager import SerialManager, Priority
from svg_processor import SVGProcessor
from webcam_manager import WebcamManager
from plot_history import PlotHistory
from terminal_manager import TerminalManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("strata")

# ---- globals ----
config = ConfigManager()
state = PlotterState()
serial_mgr = SerialManager(config, state)
svg_proc = SVGProcessor(config)
webcam = WebcamManager(config.get("recording_dir", "/home/pi/recordings"))
history = PlotHistory()
terminal = TerminalManager()

async def auto_connect():
    """Attempt to reconnect to the last used serial port on startup."""
    await asyncio.sleep(0.5)
    port = config.get("last_serial_port")
    if not port:
        return
    available = SerialManager.list_ports()
    if port not in available:
        logger.info("Auto-connect: port %s not available (found %s)", port, available)
        return
    logger.info("Auto-connect: attempting %s", port)
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, serial_mgr.connect, port)
    if ok:
        logger.info("Auto-connect: connected to %s", port)
        await broadcast_state()
    else:
        logger.warning("Auto-connect: failed to connect to %s", port)


@asynccontextmanager
async def lifespan(application: FastAPI):
    apply_bed_from_saved_model()
    asyncio.create_task(telemetry_loop())
    asyncio.create_task(auto_connect())
    yield

app = FastAPI(title="Strata Plotter Control", lifespan=lifespan)

BASE_DIR = Path(__file__).parent
_cfg_upload = config.get("upload_dir") or "uploads"
_p_upload = Path(_cfg_upload)
UPLOAD_DIR = _p_upload if _p_upload.is_absolute() else (BASE_DIR / _cfg_upload)
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logger.error("Cannot create upload_dir %s: %s — using %s", UPLOAD_DIR, e, BASE_DIR / "uploads")
    UPLOAD_DIR = BASE_DIR / "uploads"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger.info("Upload directory: %s", UPLOAD_DIR.resolve())

ws_clients: set[WebSocket] = set()
terminal_ws_clients: set[WebSocket] = set()

# ---- plot execution state ----
plot_task: Optional[asyncio.Task] = None


# ---- WebSocket broadcast ----

async def broadcast_state():
    global ws_clients
    data = json.dumps({"type": "state", "data": state.to_dict()})
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


async def broadcast_message(msg_type: str, data: dict):
    global ws_clients
    payload = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


def sync_notify():
    """Called from serial thread — schedules async broadcast."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_state())
    except RuntimeError:
        pass


serial_mgr.on_state_change = sync_notify


def on_button(paused: bool):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_message("button", {"paused": paused}))
    except RuntimeError:
        pass


serial_mgr.on_button_press = on_button


# ---- telemetry loop ----

async def telemetry_loop():
    global ws_clients
    while True:
        if ws_clients and state.connected:
            await broadcast_state()
        await asyncio.sleep(0.5)


# ---- static files & index ----

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ======== REST API ========

# ---- model dimensions ----

AXIDRAW_MODELS = {
    1: {"name": "AxiDraw V2/V3",   "width": 300, "height": 218},
    2: {"name": "AxiDraw V3/A3",   "width": 430, "height": 297},
    3: {"name": "AxiDraw SE/A1",   "width": 864, "height": 610},
    4: {"name": "AxiDraw SE/A3",   "width": 430, "height": 297},
    5: {"name": "AxiDraw V3 XLX",  "width": 585, "height": 218},
    7: {"name": "AxiDraw MiniKit", "width": 160, "height": 101},
}


def apply_bed_from_saved_model() -> None:
    """Set bed_width_mm / bed_height_mm from config last_model (runs at startup)."""
    mid = config.get("last_model")
    if mid is None:
        return
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return
    dims = AXIDRAW_MODELS.get(mid)
    if not dims:
        logger.warning("Unknown last_model %s in config — skipping bed apply", mid)
        return
    config.update_config({
        "bed_width_mm": dims["width"],
        "bed_height_mm": dims["height"],
    })
    logger.info(
        "Startup: applied bed from last_model=%s → %s×%s mm",
        mid, dims["width"], dims["height"],
    )


@app.get("/api/models")
async def get_models():
    return AXIDRAW_MODELS


@app.post("/api/model/select")
async def select_model(request: Request):
    body = await request.json()
    model_id = int(body.get("model", 1))
    dims = AXIDRAW_MODELS.get(model_id)
    if not dims:
        return JSONResponse({"error": "Unknown model"}, 400)
    config.update_config({
        "bed_width_mm": dims["width"],
        "bed_height_mm": dims["height"],
    })
    config.set("last_model", model_id)
    return {"ok": True, "width": dims["width"], "height": dims["height"], "name": dims["name"]}


# ---- connection ----

@app.get("/api/ports")
async def list_ports():
    return {"ports": SerialManager.list_ports()}


@app.post("/api/connect")
async def connect(request: Request):
    body = await request.json()
    port = body.get("port", "")
    if not port:
        return JSONResponse({"error": "No port specified"}, 400)
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, serial_mgr.connect, port)
    if ok:
        return {"status": "connected", "firmware": state.firmware_version}
    return JSONResponse({"error": "Connection failed"}, 500)


@app.post("/api/disconnect")
async def disconnect():
    serial_mgr.disconnect()
    await broadcast_state()
    return {"status": "disconnected"}


# ---- manual controls ----

def _require_connected():
    # Use PlotterState.connected (logical session), not raw serial fd state.
    if not state.connected:
        return JSONResponse({"error": "Not connected"}, 400)
    return None


@app.post("/api/pen/up")
async def pen_up():
    err = _require_connected()
    if err:
        return err
    logger.info("POST /api/pen/up — sending pen up command")
    serial_mgr.pen_up()
    return {"ok": True}


@app.post("/api/pen/down")
async def pen_down():
    err = _require_connected()
    if err:
        return err
    logger.info("POST /api/pen/down — sending pen down command")
    serial_mgr.pen_down()
    return {"ok": True}


@app.post("/api/pen/toggle")
async def pen_toggle():
    err = _require_connected()
    if err:
        return err
    serial_mgr.pen_toggle()
    return {"ok": True}


@app.post("/api/home")
async def walk_home():
    err = _require_connected()
    if err:
        return err
    serial_mgr.walk_home()
    return {"ok": True}


@app.post("/api/set_home")
async def set_home():
    serial_mgr.set_home()
    return {"ok": True}


@app.post("/api/jog")
async def jog(request: Request):
    err = _require_connected()
    if err:
        return err
    body = await request.json()
    def _xy_float(d: dict, a: str, b: str) -> float:
        for k in (a, b):
            if k in d and d[k] is not None:
                return float(d[k])
        return 0.0

    try:
        dx = _xy_float(body, "dx", "delta_x")
        dy = _xy_float(body, "dy", "delta_y")
    except (TypeError, ValueError) as e:
        logger.warning("jog: invalid dx/dy in body %s: %s", body, e)
        return JSONResponse({"error": "Invalid dx/dy"}, 400)
    err = serial_mgr.jog(dx, dy)
    if err:
        return JSONResponse({"error": err}, 400)
    return {"ok": True}


@app.post("/api/motors/enable")
async def motors_enable():
    err = _require_connected()
    if err:
        return err
    serial_mgr.enable_motors()
    return {"ok": True}


@app.post("/api/motors/disable")
async def motors_disable():
    err = _require_connected()
    if err:
        return err
    serial_mgr.disable_motors()
    return {"ok": True}


@app.post("/api/bounding_box")
async def bounding_box():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    d = svg_proc.current
    err = serial_mgr.bounding_box_test(d.min_x, d.min_y, d.max_x, d.max_y)
    if err:
        return JSONResponse({"error": err}, 400)
    return {"ok": True}


@app.post("/api/dry_run")
async def dry_run():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    asyncio.create_task(execute_plot(dry=True))
    return {"ok": True}


# ---- config & profiles ----

@app.get("/api/config")
async def get_config():
    return config.get_config()


@app.post("/api/config")
async def update_config(request: Request):
    body = await request.json()
    config.update_config(body)
    return {"ok": True}


@app.get("/api/profiles")
async def get_profiles():
    return config.get_profiles()


@app.get("/api/profiles/{name}")
async def get_profile(name: str):
    p = config.get_profile(name)
    if p is None:
        return JSONResponse({"error": "Profile not found"}, 404)
    return p


@app.post("/api/profiles/{name}")
async def save_profile(name: str, request: Request):
    body = await request.json()
    config.save_profile(name, body)
    return {"ok": True}


@app.delete("/api/profiles/{name}")
async def delete_profile(name: str):
    if config.delete_profile(name):
        return {"ok": True}
    return JSONResponse({"error": "Profile not found"}, 404)


@app.post("/api/profiles/{name}/duplicate")
async def duplicate_profile(name: str, request: Request):
    body = await request.json()
    dest = body.get("new_name", f"{name}_copy")
    if config.duplicate_profile(name, dest):
        return {"ok": True, "new_name": dest}
    return JSONResponse({"error": "Source not found"}, 404)


@app.post("/api/profiles/{name}/load")
async def load_profile(name: str):
    p = config.get_profile(name)
    if p is None:
        return JSONResponse({"error": "Profile not found"}, 404)
    config.set("last_profile", name)
    serial_mgr.clear_live_overrides()
    return {"ok": True, "profile": p, "name": name}


# ---- live overrides ----

@app.post("/api/override")
async def set_override(request: Request):
    body = await request.json()
    for k, v in body.items():
        serial_mgr.set_live_override(k, v)
    return {"ok": True}


# ---- SVG upload & processing ----

@app.post("/api/svg/upload")
async def upload_svg(file: UploadFile = File(...)):
    raw = file.filename or "upload.svg"
    safe = os.path.basename(str(raw).replace("\\", "/")) or "upload.svg"
    if not safe.lower().endswith(".svg"):
        safe = safe + ".svg"
    filepath = UPLOAD_DIR / safe
    try:
        content = await file.read()
        logger.info(
            "SVG upload: filename=%r saving to %s (%d bytes)",
            raw,
            filepath.resolve(),
            len(content),
        )
        async with aiofiles.open(filepath, "wb") as out:
            await out.write(content)
    except OSError as e:
        logger.exception("SVG upload: failed to write %s", filepath)
        return JSONResponse({"error": f"Could not save file: {e}"}, 500)
    try:
        svg_data = svg_proc.load(str(filepath))
    except ValueError as e:
        logger.warning("SVG upload: load_svg failed for %s: %s", filepath, e)
        return JSONResponse({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("SVG upload: unexpected error loading %s", filepath)
        return JSONResponse({"error": str(e)}, 500)

    profile = config.get_profile(config.get("last_profile", "Default")) or {}
    dip_threshold = profile.get("dip_threshold_mm", 80)
    speed = profile.get("speed_pendown", 25)
    travel = profile.get("speed_penup", 75)

    preview = svg_data.to_preview_json()
    est_dips = svg_proc.estimate_dips(dip_threshold)
    est_time = round(svg_proc.estimate_time_seconds(speed, travel), 1)

    state.update(current_file=safe)
    await broadcast_message(
        "svg_loaded",
        {
            "preview": preview,
            "estimated_dips": est_dips,
            "estimated_time_s": est_time,
            "filename": safe,
        },
    )
    await broadcast_state()

    return {
        "preview": preview,
        "estimated_dips": est_dips,
        "estimated_time_s": est_time,
    }

@app.get("/api/files")
async def list_files():
    files = []
    if UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.glob("*.svg"):
            try:
                st = p.stat()
                files.append({
                    "name": p.name,
                    "size": st.st_size,
                    "date": st.st_mtime
                })
            except Exception:
                pass
    files.sort(key=lambda x: x["date"], reverse=True)
    return {"files": files}

@app.post("/api/files/load")
async def load_file(request: Request):
    body = await request.json()
    filename = body.get("filename")
    if not filename:
        return JSONResponse({"error": "Missing filename"}, 400)
    filepath = UPLOAD_DIR / Path(filename).name
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "File not found"}, 404)
        
    try:
        svg_data = svg_proc.load(str(filepath))
    except ValueError as e:
        logger.warning("SVG load failed for %s: %s", filepath, e)
        return JSONResponse({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("SVG unexpected error loading %s", filepath)
        return JSONResponse({"error": str(e)}, 500)

    profile = config.get_profile(config.get("last_profile", "Default")) or {}
    dip_threshold = profile.get("dip_threshold_mm", 80)
    speed = profile.get("speed_pendown", 25)
    travel = profile.get("speed_penup", 75)

    preview = svg_data.to_preview_json()
    est_dips = svg_proc.estimate_dips(dip_threshold)
    est_time = round(svg_proc.estimate_time_seconds(speed, travel), 1)

    state.update(current_file=filename)
    await broadcast_message(
        "svg_loaded",
        {
            "preview": preview,
            "estimated_dips": est_dips,
            "estimated_time_s": est_time,
            "filename": filename,
        },
    )
    await broadcast_state()

    return {
        "preview": preview,
        "estimated_dips": est_dips,
        "estimated_time_s": est_time,
    }


@app.get("/api/svg/preview")
async def svg_preview():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 404)
    return svg_proc.current.to_preview_json()


@app.post("/api/svg/vpype")
async def run_vpype(request: Request):
    body = await request.json()
    operations = body.get("operations", [])
    try:
        stats = svg_proc.run_vpype(operations)
    except (RuntimeError, ValueError) as e:
        return JSONResponse({"error": str(e)}, 500)
    return {
        "stats": stats,
        "preview": svg_proc.current.to_preview_json() if svg_proc.current else None,
    }


@app.post("/api/svg/use_optimized")
async def use_optimized(request: Request):
    body = await request.json()
    svg_proc.use_optimized(body.get("use", False))
    return {"ok": True, "preview": svg_proc.current.to_preview_json() if svg_proc.current else None}


@app.post("/api/svg/undo")
async def svg_undo():
    if svg_proc.undo():
        return {"ok": True, "preview": svg_proc.current.to_preview_json() if svg_proc.current else None}
    return JSONResponse({"error": "Nothing to undo"}, 400)


# ---- layer management ----

@app.post("/api/layers/enable")
async def layer_enable(request: Request):
    body = await request.json()
    svg_proc.set_layer_enabled(body["layer"], body["enabled"])
    return {"ok": True}


@app.post("/api/layers/order")
async def layer_order(request: Request):
    body = await request.json()
    svg_proc.set_layer_order(body["order"])
    return {"ok": True}


@app.post("/api/layers/overrides")
async def layer_overrides(request: Request):
    body = await request.json()
    svg_proc.set_layer_overrides(body["layer"], body.get("overrides"))
    return {"ok": True}


@app.post("/api/layers/profile")
async def layer_profile(request: Request):
    body = await request.json()
    svg_proc.set_layer_profile(body["layer"], body.get("profile"))
    return {"ok": True}


@app.post("/api/layers/mask")
async def layer_mask(request: Request):
    body = await request.json()
    layer_name = body["layer"]
    region_type = body["region_type"]
    region_params = body["region_params"]
    
    # Store the mask in overrides
    overrides = {"mask": {"type": region_type, "params": region_params}}
    svg_proc.set_layer_overrides(layer_name, overrides)
    
    # Apply the mask via path reassignment (keep paths inside the mask, remove outside)
    result = svg_proc.reassign_paths(region_type, region_params, layer_name, mode="mask")
    preview = svg_proc.current.to_preview_json() if svg_proc.current else None
    return {"result": result, "preview": preview}


# ---- path reassignment ----

@app.post("/api/paths/reassign")
async def reassign_paths(request: Request):
    body = await request.json()
    result = svg_proc.reassign_paths(
        body["region_type"], body["region_params"],
        body["target_layer"], body.get("mode", "select"),
    )
    preview = svg_proc.current.to_preview_json() if svg_proc.current else None
    return {"result": result, "preview": preview}


# ---- plot queue ----

@app.get("/api/queue")
async def get_queue():
    return {"queue": config.get("plot_queue", [])}


@app.post("/api/queue/add")
async def queue_add(request: Request):
    body = await request.json()
    q = config.get("plot_queue", [])
    q.append({
        "filename": body["filename"],
        "profile": body.get("profile", config.get("last_profile", "Default")),
    })
    config.set("plot_queue", q)
    return {"queue": q}


@app.post("/api/queue/remove")
async def queue_remove(request: Request):
    body = await request.json()
    idx = body.get("index", -1)
    q = config.get("plot_queue", [])
    if 0 <= idx < len(q):
        q.pop(idx)
        config.set("plot_queue", q)
    return {"queue": q}


@app.post("/api/queue/reorder")
async def queue_reorder(request: Request):
    body = await request.json()
    config.set("plot_queue", body.get("queue", []))
    return {"ok": True}


# ---- plot execution ----

@app.post("/api/plot/start")
async def start_plot():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    if not state.connected:
        return JSONResponse({"error": "Not connected"}, 400)
    if state.plot_state == PlotState.PLOTTING:
        return JSONResponse({"error": "Already plotting"}, 400)
    global plot_task
    plot_task = asyncio.create_task(execute_plot(dry=False))
    return {"ok": True}


@app.post("/api/plot/pause")
async def pause_plot():
    serial_mgr.pause()
    return {"ok": True}


@app.post("/api/plot/resume")
async def resume_plot():
    serial_mgr.resume()
    return {"ok": True}


@app.post("/api/plot/stop")
async def stop_plot():
    serial_mgr.emergency_stop()
    return {"ok": True}


@app.post("/api/plot/pen_swap_done")
async def pen_swap_done(request: Request):
    body = await request.json()
    action = body.get("action", "start")  # "start" or "skip"
    state.update(plot_state=PlotState.PLOTTING)
    await broadcast_message("pen_swap_ack", {"action": action})
    return {"ok": True}


# ---- paint mode ----

@app.post("/api/paint/save_tray")
async def save_tray():
    config.update_config({
        "tray_x_mm": state.current_x,
        "tray_y_mm": state.current_y,
    })
    return {"ok": True, "x": state.current_x, "y": state.current_y}


@app.post("/api/paint/save_water_dish")
async def save_water_dish():
    config.update_config({
        "water_dish_x_mm": state.current_x,
        "water_dish_y_mm": state.current_y,
    })
    return {"ok": True, "x": state.current_x, "y": state.current_y}


@app.post("/api/paint/test_dip")
async def test_dip():
    serial_mgr.execute_dip(state.current_x, state.current_y)
    return {"ok": True}


# ---- webcam ----

@app.post("/api/webcam/start")
async def webcam_start(request: Request):
    body = await request.json()
    res = body.get("resolution", "720p")
    ok = webcam.start(resolution=res)
    return {"ok": ok}


@app.post("/api/webcam/stop")
async def webcam_stop():
    webcam.stop()
    return {"ok": True}


@app.get("/video_feed")
async def video_feed():
    if not webcam.is_streaming:
        return JSONResponse({"error": "Webcam not active"}, 503)
    return StreamingResponse(
        webcam.generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/webcam/record/start")
async def start_recording():
    fn = webcam.start_recording()
    if fn:
        return {"ok": True, "filename": fn}
    return JSONResponse({"error": "Failed to start recording"}, 500)


@app.post("/api/webcam/record/stop")
async def stop_recording():
    info = webcam.stop_recording()
    return {"ok": True, **info}


@app.get("/api/webcam/recordings")
async def list_recordings():
    return {"recordings": webcam.list_recordings()}


@app.get("/api/webcam/recordings/{filename}")
async def download_recording(filename: str):
    path = Path(config.get("recording_dir", "/home/pi/recordings")) / filename
    if path.exists():
        return FileResponse(str(path), filename=filename)
    return JSONResponse({"error": "Not found"}, 404)


@app.post("/api/webcam/resolution")
async def set_resolution(request: Request):
    body = await request.json()
    webcam.set_resolution(body.get("resolution", "720p"))
    return {"ok": True}


# ---- history ----

@app.get("/api/history")
async def get_history():
    return {"plots": history.get_all()}


@app.get("/api/history/{plot_id}")
async def get_history_item(plot_id: int):
    item = history.get_by_id(plot_id)
    if item:
        return item
    return JSONResponse({"error": "Not found"}, 404)


@app.post("/api/history/{plot_id}/requeue")
async def requeue_from_history(plot_id: int):
    item = history.get_by_id(plot_id)
    if not item:
        return JSONResponse({"error": "Not found"}, 404)
    filepath = UPLOAD_DIR / item["filename"]
    if not filepath.exists():
        return JSONResponse({"error": "File no longer exists"}, 404)
    q = config.get("plot_queue", [])
    q.append({"filename": item["filename"], "profile": item.get("profile_name", "Default")})
    config.set("plot_queue", q)
    return {"ok": True}


# ---- state ----

@app.get("/api/state")
async def get_state():
    return state.to_dict()


# ======== WebSockets ========

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global ws_clients
    await ws.accept()
    ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "state", "data": state.to_dict()}))
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            await handle_ws_message(data, ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket error: %s", e)
    finally:
        ws_clients.discard(ws)


async def handle_ws_message(data: dict, ws: WebSocket):
    action = data.get("action")
    if action == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))


@app.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket):
    await ws.accept()
    terminal_ws_clients.add(ws)

    if not terminal.is_available:
        await ws.send_text("\r\n[Terminal not available on this platform]\r\n")
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            terminal_ws_clients.discard(ws)
        return

    loop = asyncio.get_event_loop()

    def on_output(data: bytes):
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_bytes(data), loop
            )
        except Exception:
            pass

    if not terminal.is_running:
        terminal.start(on_output=on_output)

    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            if data.get("type") == "input":
                terminal.write(data.get("data", ""))
            elif data.get("type") == "resize":
                terminal.resize(data.get("cols", 120), data.get("rows", 40))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("Terminal WS error: %s", e)
    finally:
        terminal_ws_clients.discard(ws)


# ======== Plot Execution Engine ========

async def execute_plot(dry: bool = False):
    """Main plot execution coroutine. Streams paths to serial manager."""
    if not svg_proc.has_svg:
        return

    profile_name = config.get("last_profile", "Default")
    profile = config.get_profile(profile_name) or {}
    paint_enabled = profile.get("paint_enabled", False) and not dry
    dip_threshold = profile.get("dip_threshold_mm", 80) if paint_enabled else 0
    taper_enabled = profile.get("pressure_taper_enabled", False) and paint_enabled
    taper_mm = profile.get("pressure_taper_mm", 2) if taper_enabled else 0

    instructions = svg_proc.get_plot_paths(dip_threshold)
    total_dist = svg_proc.current.total_distance()
    total_paths = svg_proc.current.total_paths()
    total_layers = len(svg_proc.current.enabled_layers())

    state.reset_for_new_plot(total_dist, total_paths, total_layers)
    state.update(
        plot_state=PlotState.PLOTTING,
        current_file=svg_proc.current.filename,
    )
    await broadcast_state()

    plot_start = time.time()
    current_layer_name = ""
    layer_idx = 0
    path_idx = 0
    pending_swap = False
    swap_event = asyncio.Event()

    for instr in instructions:
        if state.plot_state == PlotState.IDLE:
            break

        while serial_mgr.is_paused and state.plot_state != PlotState.IDLE:
            await asyncio.sleep(0.1)

        itype = instr["type"]

        if itype == "layer_start":
            current_layer_name = instr["layer"]
            layer_idx += 1
            state.update(
                current_layer=current_layer_name,
                current_layer_index=layer_idx,
            )
            layer_overrides = instr.get("overrides")
            layer_profile = instr.get("profile")
            if layer_profile:
                p = config.get_profile(layer_profile)
                if p:
                    for k, v in p.items():
                        serial_mgr.set_live_override(k, v)
            elif layer_overrides:
                for k, v in layer_overrides.items():
                    serial_mgr.set_live_override(k, v)
            await broadcast_state()

        elif itype == "layer_end":
            remaining = [l for l in svg_proc.current.enabled_layers()
                         if l.order > layer_idx - 1]
            if remaining and not dry:
                serial_mgr.pen_up()
                serial_mgr.walk_home()
                state.update(plot_state=PlotState.PEN_SWAP)

                next_layer = remaining[0]
                await broadcast_message("pen_swap", {
                    "completed_layer": current_layer_name,
                    "next_layer": next_layer.name,
                    "next_color": next_layer.color,
                    "remaining_layers": [l.name for l in remaining],
                })

                while state.plot_state == PlotState.PEN_SWAP:
                    await asyncio.sleep(0.2)

            serial_mgr.clear_live_overrides()

        elif itype == "travel":
            if dry:
                serial_mgr.rapid_move(instr["x"], instr["y"])
            else:
                serial_mgr.pen_up()
                serial_mgr.rapid_move(instr["x"], instr["y"])

        elif itype == "pen_down":
            if not dry:
                if taper_enabled and dip_threshold > 0:
                    offset = taper_mm * (state.session_distance / dip_threshold)
                    base = profile.get("pen_pos_down", 40)
                    serial_mgr.set_live_override("pen_pos_down", base - offset)
                serial_mgr.pen_down()

        elif itype == "pen_up":
            if not dry:
                serial_mgr.pen_up()
            path_idx += 1
            state.update(current_path_index=path_idx)

        elif itype == "draw":
            if dry:
                serial_mgr.rapid_move(instr["x"], instr["y"])
            else:
                serial_mgr.move_to_and_draw(instr["x"], instr["y"])

        elif itype == "dip":
            if not dry:
                serial_mgr.execute_dip(instr["return_x"], instr["return_y"])
                if taper_enabled:
                    serial_mgr.set_live_override("pen_pos_down", profile.get("pen_pos_down", 40))

        await asyncio.sleep(0)  # yield to event loop

    # plot complete
    duration = time.time() - plot_start
    state.update(plot_state=PlotState.IDLE)
    serial_mgr.pen_up()
    serial_mgr.walk_home()

    if not dry:
        history.log_plot(
            filename=svg_proc.current.filename if svg_proc.current else "unknown",
            profile_name=profile_name,
            profile_data=profile,
            layers=[l.name for l in svg_proc.current.enabled_layers()] if svg_proc.current else [],
            duration_seconds=duration,
            distance_mm=state.distance_plotted,
            dip_count=state.dip_count,
            errors=state.errors,
        )

        finished_dir = Path(config.get("finished_dir", "/home/pi/plot_finished"))
        finished_dir.mkdir(parents=True, exist_ok=True)
        if svg_proc.current and svg_proc.current.source_path:
            src = Path(svg_proc.current.source_path)
            if src.exists():
                shutil.copy2(str(src), str(finished_dir / src.name))

    await broadcast_message("plot_complete", {
        "duration_s": round(duration, 1),
        "distance_mm": round(state.distance_plotted, 1),
        "dip_count": state.dip_count,
        "dry_run": dry,
    })
    await broadcast_state()


# ======== entry point ========


def _get_local_ip() -> str:
    """Get the LAN IP by briefly connecting to an external address (no data sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    host_ip = _get_local_ip()
    port = 8080
    url = f"http://{host_ip}:{port}"

    webbrowser.open(f"http://localhost:{port}")

    banner = (
        "\n"
        "─────────────────────────────────────\n"
        "  STRATA is running at:\n"
        f"  {url}\n"
        "─────────────────────────────────────\n"
    )
    print(banner)

    uvicorn.run(app, host="0.0.0.0", port=port)
