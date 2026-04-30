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
from plot_history import PlotHistory
from terminal_manager import TerminalManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("strata")

# ---- globals ----
config = ConfigManager()
state = PlotterState()
serial_mgr = SerialManager(config, state)
svg_proc = SVGProcessor(config)
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
    for ws in set(ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


async def broadcast_message(msg_type: str, data: dict):
    global ws_clients
    payload = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in set(ws_clients):
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
async def dry_run(request: Request):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}

    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    asyncio.create_task(execute_plot(dry=True, req_args=body))
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
    try:
        model_id = int(p.get("model")) if p.get("model") is not None else None
    except (TypeError, ValueError):
        model_id = None
    dims = AXIDRAW_MODELS.get(model_id) if model_id is not None else None
    if dims:
        config.update_config({
            "bed_width_mm": dims["width"],
            "bed_height_mm": dims["height"],
        })
        config.set("last_model", model_id)
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
        logger.info(f"Loaded SVG: {svg_data.total_paths()} paths, bounds {svg_data.min_x}, {svg_data.min_y} to {svg_data.max_x}, {svg_data.max_y}")
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
    msg_data = {
        "preview": preview,
        "estimated_dips": est_dips,
        "estimated_time_s": est_time,
        "filename": safe,
    }
    logger.info(f"Broadcasting svg_loaded message. Preview keys: {list(preview.keys())}")
    await broadcast_message("svg_loaded", msg_data)
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
    msg_data = {
        "preview": preview,
        "estimated_dips": est_dips,
        "estimated_time_s": est_time,
        "filename": filename,
    }
    logger.info(f"Broadcasting svg_loaded message. Preview keys: {list(preview.keys())}")
    await broadcast_message("svg_loaded", msg_data)
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


def _svg_tool_response(result: dict) -> dict:
    return {
        "ok": True,
        "result": result,
        "preview": svg_proc.current.to_preview_json() if svg_proc.current else None,
    }


@app.get("/api/svg/sanity")
async def svg_sanity():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    return {"ok": True, "result": svg_proc.sanity_check()}


@app.post("/api/svg/layer_optimizer")
async def svg_layer_optimizer():
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    try:
        return _svg_tool_response(svg_proc.optimize_layers_by_pen())
    except Exception as e:
        logger.exception("Layer optimizer error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/registration_marks")
async def svg_registration_marks(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.add_registration_marks(
            size_mm=float(body.get("size_mm", 5.0)),
            inset_mm=float(body.get("inset_mm", 8.0)),
        )
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Registration mark error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/pen_weight")
async def svg_pen_weight(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.add_pen_weight(
            spacing_mm=float(body.get("spacing_mm", 0.35)),
            passes=int(body.get("passes", 3)),
        )
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Pen weight error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/hatch_fill")
async def svg_hatch_fill(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.add_hatch_fill(
            style=body.get("style", "straight"),
            spacing_mm=float(body.get("spacing_mm", 3.0)),
            angle_deg=float(body.get("angle_deg", 45.0)),
            target_layer=body.get("target_layer") or None,
        )
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Hatch fill error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/masked_fill")
async def svg_masked_fill(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.add_masked_fill(
            region_type=body.get("region_type"),
            region_params=body.get("region_params"),
            fill_style=body.get("fill_style", "waves"),
            spacing_mm=float(body.get("spacing_mm", 3.0)),
        )
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Masked fill error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/maze_fill")
async def svg_maze_fill(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.add_maze_fill(spacing_mm=float(body.get("spacing_mm", 4.0)))
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Maze fill error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/style_preset")
async def svg_style_preset(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        result = svg_proc.apply_style_preset(body.get("preset", "engraving"))
        return _svg_tool_response(result)
    except Exception as e:
        logger.exception("Style preset error")
        return JSONResponse({"error": str(e)}, 500)


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


def _validate_plot_bounds(args: dict) -> Optional[str]:
    if not svg_proc.has_svg or not svg_proc.current:
        return None
    try:
        scale = float(args.get("scale", 1.0))
        offset_x = float(args.get("offset_x", 0.0))
        offset_y = float(args.get("offset_y", 0.0))
        bed_w = float(config.get("bed_width_mm", 300) or 300)
        bed_h = float(config.get("bed_height_mm", 218) or 218)
    except (TypeError, ValueError):
        return "Invalid plot transform"
    d = svg_proc.current
    min_x = d.min_x * scale + offset_x
    max_x = d.max_x * scale + offset_x
    min_y = d.min_y * scale + offset_y
    max_y = d.max_y * scale + offset_y
    if min(min_x, max_x) < -1 or min(min_y, max_y) < -1 or max(min_x, max_x) > bed_w + 1 or max(min_y, max_y) > bed_h + 1:
        return (
            f"Transformed artwork bounds ({min_x:.1f}, {min_y:.1f}) to "
            f"({max_x:.1f}, {max_y:.1f}) exceed bed limits ({bed_w:.1f}x{bed_h:.1f}mm)"
        )
    return None


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    path = UPLOAD_DIR / filename
    if path.exists() and path.is_file():
        path.unlink()
        return {"ok": True}
    return JSONResponse({"error": "File not found"}, 404)

@app.post("/api/layers/extract")
async def layer_extract(request: Request):
    """Extract selection region from a source layer into a new auto-named layer."""
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    source_layer = body.get("source_layer")
    region_type = body.get("region_type")
    region_params = body.get("region_params")
    if not source_layer or not region_type or region_params is None:
        return JSONResponse({"error": "Missing source_layer or region data"}, 400)
    try:
        result = svg_proc.extract_region_to_new_layer(
            source_layer=source_layer,
            region_type=region_type,
            region_params=region_params,
        )
        preview = svg_proc.current.to_preview_json() if svg_proc.current else None
        if preview:
            await broadcast_message("svg_loaded", {"preview": preview, "filename": preview.get("filename", "")})
        return {"ok": True, "result": result, "preview": preview}
    except Exception as e:
        logger.exception("Extract error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/align")
async def svg_align(request: Request):
    """Translate all geometry to align bounds within a given canvas size."""
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    mode = body.get("mode")
    try:
        canvas_w = float(body.get("canvas_w"))
        canvas_h = float(body.get("canvas_h"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid canvas_w/canvas_h"}, 400)
    if canvas_w <= 0 or canvas_h <= 0:
        return JSONResponse({"error": "Invalid canvas size"}, 400)
    try:
        svg_proc.align_to_canvas(mode=mode, canvas_w=canvas_w, canvas_h=canvas_h)
        preview = svg_proc.current.to_preview_json() if svg_proc.current else None
        if preview:
            await broadcast_message("svg_loaded", {"preview": preview, "filename": preview.get("filename", "")})
        return {"ok": True, "preview": preview}
    except Exception as e:
        logger.exception("Align error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/scale")
async def svg_scale(request: Request):
    """Scale all geometry about current bounds center."""
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        factor = float(body.get("factor", 1.0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid factor"}, 400)
    if factor <= 0 or factor > 100:
        return JSONResponse({"error": "Factor out of range"}, 400)
    try:
        svg_proc.scale_about_center(factor=factor)
        preview = svg_proc.current.to_preview_json() if svg_proc.current else None
        if preview:
            await broadcast_message("svg_loaded", {"preview": preview, "filename": preview.get("filename", "")})
        return {"ok": True, "preview": preview}
    except Exception as e:
        logger.exception("Scale error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/apply_transform")
async def svg_apply_transform(request: Request):
    """Bake the current browser preview scale/offset into SVG geometry."""
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        scale = float(body.get("scale", 1.0))
        offset_x = float(body.get("offset_x", 0.0))
        offset_y = float(body.get("offset_y", 0.0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid transform"}, 400)
    if scale <= 0 or scale > 100:
        return JSONResponse({"error": "Scale out of range"}, 400)
    try:
        svg_proc.apply_transform(scale=scale, offset_x=offset_x, offset_y=offset_y)
        preview = svg_proc.current.to_preview_json() if svg_proc.current else None
        if preview:
            await broadcast_message("svg_loaded", {"preview": preview, "filename": preview.get("filename", "")})
        return {"ok": True, "preview": preview}
    except Exception as e:
        logger.exception("Apply transform error")
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/svg/translate")
async def svg_translate(request: Request):
    """Translate all geometry by dx/dy (mm)."""
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    try:
        dx = float(body.get("dx", 0.0))
        dy = float(body.get("dy", 0.0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid dx/dy"}, 400)
    try:
        svg_proc.translate(dx=dx, dy=dy)
        preview = svg_proc.current.to_preview_json() if svg_proc.current else None
        if preview:
            await broadcast_message("svg_loaded", {"preview": preview, "filename": preview.get("filename", "")})
        return {"ok": True, "preview": preview}
    except Exception as e:
        logger.exception("Translate error")
        return JSONResponse({"error": str(e)}, 500)

@app.post("/api/plot/start")
async def start_plot(request: Request):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}

    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    if not state.connected:
        return JSONResponse({"error": "Not connected"}, 400)
    if state.plot_state == PlotState.PLOTTING:
        return JSONResponse({"error": "Already plotting"}, 400)
    err = _validate_plot_bounds(body)
    if err:
        return JSONResponse({"error": err}, 400)
    global plot_task
    plot_task = asyncio.create_task(execute_plot(dry=False, req_args=body))
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

async def execute_plot(dry: bool = False, req_args: dict = None):
    """Main plot execution coroutine. Streams paths to serial manager."""
    if not svg_proc.has_svg:
        return

    profile_name = config.get("last_profile", "Default")
    profile = config.get_profile(profile_name) or {}
    paint_enabled = profile.get("paint_enabled", False) and not dry
    dip_threshold = profile.get("dip_threshold_mm", 80) if paint_enabled else 0
    taper_enabled = profile.get("pressure_taper_enabled", False) and paint_enabled
    taper_mm = profile.get("pressure_taper_mm", 2) if taper_enabled else 0

    req_args = req_args or {}
    # layer-only plotting support
    layer_name = req_args.get("layer_name")
    # (legacy) transform support, in case any clients still send it
    try:
        scale = float(req_args.get("scale", 1.0))
        offset_x = float(req_args.get("offset_x", 0.0))
        offset_y = float(req_args.get("offset_y", 0.0))
    except (TypeError, ValueError):
        scale, offset_x, offset_y = 1.0, 0.0, 0.0
    distance_scale = abs(scale) if scale else 1.0

    instructions = svg_proc.get_plot_paths(
        dip_threshold_mm=dip_threshold,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        layer_name=layer_name,
    )
    plotted_layers = [
        l for l in sorted(svg_proc.current.enabled_layers(), key=lambda l: l.order)
        if not layer_name or l.name == layer_name
    ]
    # telemetry counts reflect enabled layers; if plotting a single layer, scope it
    if layer_name:
        layer_obj = next((l for l in svg_proc.current.enabled_layers() if l.name == layer_name), None) if svg_proc.current else None
        total_dist = (layer_obj.total_distance() * distance_scale) if layer_obj else 0.0
        total_paths = layer_obj.path_count() if layer_obj else 0
        total_layers = 1 if layer_obj else 0
    else:
        total_dist = svg_proc.current.total_distance() * distance_scale
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
    motion_failed = False

    async def run_motion(fn, x: float, y: float) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, x, y)

    async def run_serial(fn, *args) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    async def wait_for_streamed_motion() -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, serial_mgr.wait_motion_sync)

    streamed_draw_count = 0
    stream_flush_every = 32

    if not dry:
        motion_failed = not await run_serial(serial_mgr.prepare_plot)
        if motion_failed:
            state.errors.append("Could not prepare plotter for plotting")
            logger.error("Plot stopped: plotter preparation failed")

    for instr in instructions if not motion_failed else []:
        if state.plot_state == PlotState.IDLE:
            break

        while serial_mgr.is_paused and state.plot_state != PlotState.IDLE:
            await asyncio.sleep(0.1)

        itype = instr["type"]

        if itype != "draw" and streamed_draw_count:
            motion_failed = not await wait_for_streamed_motion()
            streamed_draw_count = 0
            if motion_failed:
                state.errors.append("Motion command failed or timed out")
                logger.error("Plot stopped: streamed motion failed")
                break

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
            remaining = plotted_layers[layer_idx:]
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
                motion_failed = not await run_motion(serial_mgr.rapid_move_sync, instr["x"], instr["y"])
            else:
                serial_mgr.pen_up()
                motion_failed = not await run_motion(serial_mgr.rapid_move_sync, instr["x"], instr["y"])

        elif itype == "pen_down":
            if not dry:
                if taper_enabled and dip_threshold > 0:
                    offset = taper_mm * (state.session_distance / dip_threshold)
                    base = profile.get("pen_pos_down", 40)
                    serial_mgr.set_live_override("pen_pos_down", base - offset)
                motion_failed = not await run_serial(serial_mgr.pen_down_sync)

        elif itype == "pen_up":
            if not dry:
                motion_failed = not await run_serial(serial_mgr.pen_up_sync)
            path_idx += 1
            state.update(current_path_index=path_idx)

        elif itype == "draw_path":
            if dry:
                for px, py in instr["points"][1:]:
                    motion_failed = not await run_motion(serial_mgr.rapid_move_sync, px, py)
                    if motion_failed:
                        break
            else:
                motion_failed = not await run_serial(serial_mgr.draw_path_sync, instr["points"])

        elif itype == "draw":
            if dry:
                motion_failed = not await run_motion(serial_mgr.rapid_move_sync, instr["x"], instr["y"])
            else:
                motion_failed = not serial_mgr.move_to_and_draw_stream(instr["x"], instr["y"])
                if not motion_failed:
                    streamed_draw_count += 1
                    if streamed_draw_count >= stream_flush_every:
                        motion_failed = not await wait_for_streamed_motion()
                        streamed_draw_count = 0

        elif itype == "dip":
            if not dry:
                serial_mgr.execute_dip(instr["return_x"], instr["return_y"])
                if taper_enabled:
                    serial_mgr.set_live_override("pen_pos_down", profile.get("pen_pos_down", 40))

        if motion_failed:
            state.errors.append("Motion command failed or timed out")
            logger.error("Plot stopped: motion command failed")
            break

        await asyncio.sleep(0)  # yield to event loop

    if not motion_failed and streamed_draw_count:
        motion_failed = not await wait_for_streamed_motion()

    # plot complete / stopped
    duration = time.time() - plot_start
    state.update(plot_state=PlotState.IDLE)
    if not dry and not motion_failed:
        serial_mgr.pen_up()
        serial_mgr.walk_home()

    if not dry and not motion_failed:
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
        "error": "Motion command failed or timed out" if motion_failed else "",
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

    # webbrowser.open(f"http://localhost:{port}")

    # Keep banner ASCII-only for Windows consoles.
    banner = "\n" + "-" * 37 + "\n  STRATA is running at:\n  " + url + "\n" + "-" * 37 + "\n"
    try:
        print(banner)
    except Exception:
        print(f"\nSTRATA is running at: {url}\n")

    uvicorn.run(app, host="0.0.0.0", port=port)
