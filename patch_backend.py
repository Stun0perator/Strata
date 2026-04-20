import re

# Update main.py
with open("main.py", "r", encoding="utf-8") as f:
    main_py = f.read()

# 1. Add DELETE file endpoint
delete_file_endpoint = """
@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    path = UPLOAD_DIR / filename
    if path.exists() and path.is_file():
        path.unlink()
        return {"ok": True}
    return JSONResponse({"error": "File not found"}, 404)

@app.post("/api/layers/mask")
async def layer_mask(request: Request):
    if not svg_proc.has_svg:
        return JSONResponse({"error": "No SVG loaded"}, 400)
    body = await request.json()
    layer_name = body.get("layer")
    polygon = body.get("polygon")
    if not layer_name or not polygon:
        return JSONResponse({"error": "Missing layer or polygon data"}, 400)
    
    try:
        svg_proc.apply_mask(layer_name, polygon)
        await broadcast_message("svg_loaded", svg_proc.to_preview_json())
        return {"ok": True}
    except Exception as e:
        logger.error(f"Mask error: {e}")
        return JSONResponse({"error": str(e)}, 500)

@app.post("/api/plot/start")
"""

main_py = main_py.replace('@app.post("/api/plot/start")', delete_file_endpoint)

# 2. Update plot/start and execute_plot signatures
main_py = re.sub(
    r'@app.post\("/api/plot/start"\)\s*async def start_plot\(\):',
    '@app.post("/api/plot/start")\nasync def start_plot(request: Request):\n    body = await request.json() if request.headers.get("content-type") == "application/json" else {}\n',
    main_py
)
main_py = main_py.replace(
    'plot_task = asyncio.create_task(execute_plot(dry=False))',
    'plot_task = asyncio.create_task(execute_plot(dry=False, req_args=body))'
)

main_py = re.sub(
    r'@app.post\("/api/dry_run"\)\s*async def dry_run\(\):',
    '@app.post("/api/dry_run")\nasync def dry_run(request: Request):\n    body = await request.json() if request.headers.get("content-type") == "application/json" else {}\n',
    main_py
)
main_py = main_py.replace(
    'asyncio.create_task(execute_plot(dry=True))',
    'asyncio.create_task(execute_plot(dry=True, req_args=body))'
)

main_py = main_py.replace('async def execute_plot(dry: bool = False):', 'async def execute_plot(dry: bool = False, req_args: dict = None):')

execute_plot_patch = """    if req_args is None: req_args = {}
    scale = float(req_args.get("scale", 1.0))
    offset_x = float(req_args.get("offset_x", 0.0))
    offset_y = float(req_args.get("offset_y", 0.0))
    layer_name = req_args.get("layer_name")

    # Pass parameters to get_plot_paths
    paths = svg_proc.get_plot_paths(dip_threshold, scale=scale, offset_x=offset_x, offset_y=offset_y, layer_name=layer_name)
"""
# We need to find where `paths = svg_proc.get_plot_paths(dip_threshold)` is and replace it.
main_py = re.sub(r'\s*paths = svg_proc\.get_plot_paths\(dip_threshold\)', execute_plot_patch, main_py)


with open("main.py", "w", encoding="utf-8") as f:
    f.write(main_py)

print("main.py patched.")

# Update svg_processor.py
with open("svg_processor.py", "r", encoding="utf-8") as f:
    svg_py = f.read()

# Modify get_plot_paths signature
svg_py = svg_py.replace(
    'def get_plot_paths(self, dip_threshold_mm: float = 0) -> list[dict]:',
    'def get_plot_paths(self, dip_threshold_mm: float = 0, scale: float = 1.0, offset_x: float = 0.0, offset_y: float = 0.0, layer_name: str = None) -> list[dict]:'
)

path_processing = """            if layer_name and layer.name != layer_name:
                continue
            instructions.append({"type": "layer_start", "layer": layer.name,
                                 "overrides": layer.overrides, "profile": layer.profile_name})

            for seg in layer.paths:
                if len(seg.points) < 2:
                    continue
                
                # Apply transform
                transformed_pts = [((px * scale) + offset_x, (py * scale) + offset_y) for px, py in seg.points]

                instructions.append({"type": "travel", "x": transformed_pts[0][0], "y": transformed_pts[0][1]})
                instructions.append({"type": "pen_down"})

                for j in range(1, len(transformed_pts)):
                    px, py = transformed_pts[j]
                    if dip_threshold_mm > 0:
                        ppx, ppy = transformed_pts[j - 1]
                        step_dist = math.hypot(px - ppx, py - ppy)
"""
# Replace the old path loop with the new one
svg_py = re.sub(
    r'instructions\.append\(\{"type": "layer_start", "layer": layer\.name,.*?"profile": layer\.profile_name\}\)\n\n            for seg in layer\.paths:\n                if len\(seg\.points\) < 2:\n                    continue\n\n                instructions\.append\(\{"type": "travel", "x": seg\.points\[0\]\[0\], "y": seg\.points\[0\]\[1\]\}\)\n                instructions\.append\(\{"type": "pen_down"\}\)\n\n                for j in range\(1, len\(seg\.points\)\):\n                    px, py = seg\.points\[j\]\n                    if dip_threshold_mm > 0:\n                        ppx, ppy = seg\.points\[j - 1\]\n                        step_dist = math\.hypot\(px - ppx, py - ppy\)',
    path_processing,
    svg_py,
    flags=re.DOTALL
)

# Add apply_mask method
apply_mask_code = """
    def apply_mask(self, layer_name: str, polygon_points: list[list[float]]):
        if not self._current:
            return
        
        target = None
        for layer in self._current.layers:
            if layer.name == layer_name:
                target = layer
                break
                
        if not target:
            raise ValueError(f"Layer {layer_name} not found")
            
        poly = Polygon(polygon_points)
        if not poly.is_valid:
            poly = poly.buffer(0)
            
        new_paths = []
        for path in target.paths:
            if len(path.points) < 2:
                continue
            ls = LineString(path.points)
            intersection = ls.intersection(poly)
            
            def add_geom(geom):
                if geom.geom_type == 'LineString':
                    if len(geom.coords) >= 2:
                        new_paths.append(SVGPath(list(geom.coords), path.color))
                elif geom.geom_type == 'MultiLineString':
                    for line in geom.geoms:
                        if len(line.coords) >= 2:
                            new_paths.append(SVGPath(list(line.coords), path.color))
                            
            add_geom(intersection)
            
        self._push_undo()
        target.paths = new_paths
"""

svg_py = svg_py.replace('def get_plot_paths', apply_mask_code + '\n    def get_plot_paths')

with open("svg_processor.py", "w", encoding="utf-8") as f:
    f.write(svg_py)

print("svg_processor.py patched.")
