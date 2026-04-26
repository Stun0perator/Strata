import copy
import json
import logging
import math
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, TYPE_CHECKING

from shapely.geometry import LineString, Polygon, MultiLineString, Point

if TYPE_CHECKING:
    from config_manager import ConfigManager
from shapely.ops import split

logger = logging.getLogger("strata.svg")

SAMPLE_INTERVAL_MM = 0.5


@dataclass
class PathSegment:
    points: list[tuple[float, float]]
    layer: str = "default"
    color: str = "#000000"
    stroke_width: float = 1.0
    path_id: str = ""

    def length_mm(self) -> float:
        total = 0.0
        for i in range(1, len(self.points)):
            dx = self.points[i][0] - self.points[i - 1][0]
            dy = self.points[i][1] - self.points[i - 1][1]
            total += math.hypot(dx, dy)
        return total


@dataclass
class SVGLayer:
    name: str
    color: str = "#000000"
    paths: list[PathSegment] = field(default_factory=list)
    enabled: bool = True
    order: int = 0
    overrides: Optional[dict] = None
    profile_name: Optional[str] = None

    def total_distance(self) -> float:
        return sum(p.length_mm() for p in self.paths)

    def path_count(self) -> int:
        return len(self.paths)


@dataclass
class SVGData:
    layers: list[SVGLayer] = field(default_factory=list)
    width_mm: float = 0.0
    height_mm: float = 0.0
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0
    filename: str = ""
    source_path: str = ""

    def total_distance(self) -> float:
        return sum(l.total_distance() for l in self.layers if l.enabled)

    def total_paths(self) -> int:
        return sum(l.path_count() for l in self.layers if l.enabled)

    def enabled_layers(self) -> list[SVGLayer]:
        return [l for l in self.layers if l.enabled]

    def to_preview_json(self) -> dict:
        layers_data = []
        for layer in self.layers:
            paths_data = []
            for p in layer.paths:
                paths_data.append({
                    "points": p.points,
                    "color": p.color,
                    "id": p.path_id,
                })
            layers_data.append({
                "name": layer.name,
                "color": layer.color,
                "enabled": layer.enabled,
                "order": layer.order,
                "path_count": layer.path_count(),
                "distance_mm": round(layer.total_distance(), 1),
                "paths": paths_data,
                "overrides": layer.overrides,
                "profile_name": layer.profile_name,
            })
        return {
            "width_mm": round(self.width_mm, 2),
            "height_mm": round(self.height_mm, 2),
            "min_x": round(self.min_x, 2),
            "min_y": round(self.min_y, 2),
            "max_x": round(self.max_x, 2),
            "max_y": round(self.max_y, 2),
            "total_distance_mm": round(self.total_distance(), 1),
            "total_paths": self.total_paths(),
            "filename": self.filename,
            "layers": layers_data,
        }


class SVGProcessor:
    """
    Parses SVGs, manages layers, provides vpype integration, undo stack,
    and path intersection/splitting via shapely.
    """

    def __init__(self, config: Optional["ConfigManager"] = None):
        self._config = config
        self._current: Optional[SVGData] = None
        self._original_svg_path: Optional[str] = None
        self._optimized_svg_path: Optional[str] = None
        self._use_optimized = False
        self._undo_stack: list[SVGData] = []
        self._max_undo = 10

    @property
    def current(self) -> Optional[SVGData]:
        return self._current

    @property
    def has_svg(self) -> bool:
        return self._current is not None

    def load(self, filepath: str) -> SVGData:
        """Load and parse an SVG file into structured layer/path data."""
        self._original_svg_path = filepath

        try:
            import svgpathtools
            paths, attributes, svg_attributes = svgpathtools.svg2paths2(filepath)
        except Exception as e:
            logger.error("Failed to parse SVG: %s", e)
            raise ValueError(f"Failed to parse SVG: {e}")

        vb = self._parse_viewbox(svg_attributes)
        width_mm, height_mm = self._document_size_mm(svg_attributes, vb)

        layer_map: dict[str, SVGLayer] = {}
        all_min_x, all_min_y = float("inf"), float("inf")
        all_max_x, all_max_y = float("-inf"), float("-inf")

        tree = ET.parse(filepath)
        root = tree.getroot()
        ns = {"svg": "http://www.w3.org/2000/svg", "inkscape": "http://www.inkscape.org/namespaces/inkscape"}
        layer_names = self._extract_layer_names(root, ns)

        for i, (path, attr) in enumerate(zip(paths, attributes)):
            layer_name = self._get_path_layer(attr, layer_names, root, ns)
            color = self._get_path_color(attr)
            points = self._sample_path(path)

            if len(points) < 2:
                continue

            for px, py in points:
                all_min_x = min(all_min_x, px)
                all_min_y = min(all_min_y, py)
                all_max_x = max(all_max_x, px)
                all_max_y = max(all_max_y, py)

            seg = PathSegment(
                points=points,
                layer=layer_name,
                color=color,
                path_id=attr.get("id", f"path_{i}"),
            )

            if layer_name not in layer_map:
                layer_map[layer_name] = SVGLayer(
                    name=layer_name,
                    color=color,
                    order=len(layer_map),
                )
            layer_map[layer_name].paths.append(seg)

        if all_min_x == float("inf"):
            all_min_x = all_min_y = 0
            all_max_x = width_mm
            all_max_y = height_mm

        layers = list(layer_map.values())
        vx0, vy0, vw, vh = self._mapping_viewport(vb, width_mm, height_mm)
        if vw > 0 and vh > 0:
            layers = self._map_layers_to_document(layers, vx0, vy0, vw, vh, width_mm, height_mm)
            all_min_x = all_min_y = float("inf")
            all_max_x = all_max_y = float("-inf")
            for layer in layers:
                for seg in layer.paths:
                    for px, py in seg.points:
                        all_min_x = min(all_min_x, px)
                        all_min_y = min(all_min_y, py)
                        all_max_x = max(all_max_x, px)
                        all_max_y = max(all_max_y, py)
            if all_min_x == float("inf"):
                all_min_x = all_min_y = 0.0
                all_max_x, all_max_y = width_mm, height_mm

        # Normalize layer naming to numeric "1", "2", ... (stable order)
        layers = self._renumber_layers_numeric(layers)

        svg_data = SVGData(
            layers=layers,
            width_mm=width_mm,
            height_mm=height_mm,
            min_x=all_min_x,
            min_y=all_min_y,
            max_x=all_max_x,
            max_y=all_max_y,
            filename=os.path.basename(filepath),
            source_path=filepath,
        )

        self._current = svg_data
        self._undo_stack.clear()
        return svg_data

    @staticmethod
    def _renumber_layers_numeric(layers: list[SVGLayer]) -> list[SVGLayer]:
        """Rename layers to '1','2',... and update PathSegment.layer accordingly."""
        renamed: list[SVGLayer] = []
        for idx, layer in enumerate(layers, start=1):
            new_name = str(idx)
            new_paths = []
            for seg in layer.paths:
                new_paths.append(PathSegment(
                    points=seg.points,
                    layer=new_name,
                    color=seg.color,
                    stroke_width=seg.stroke_width,
                    path_id=seg.path_id,
                ))
            renamed.append(SVGLayer(
                name=new_name,
                color=layer.color,
                paths=new_paths,
                enabled=layer.enabled,
                order=layer.order,
                overrides=layer.overrides,
                profile_name=layer.profile_name,
            ))
        # Ensure orders are 0..n-1 in the renamed list order
        for i, l in enumerate(renamed):
            l.order = i
        return renamed

    def _next_numeric_layer_name(self) -> str:
        if not self._current or not self._current.layers:
            return "1"
        nums = []
        for l in self._current.layers:
            try:
                nums.append(int(str(l.name).strip()))
            except Exception:
                continue
        return str((max(nums) if nums else len(self._current.layers)) + 1)

    def _bed_dims_from_config(self) -> Tuple[Optional[float], Optional[float]]:
        if self._config is None:
            return None, None
        try:
            w = float(self._config.get("bed_width_mm", 300) or 300)
            h = float(self._config.get("bed_height_mm", 218) or 218)
            if w <= 0 or h <= 0:
                return None, None
            return w, h
        except (TypeError, ValueError):
            return None, None

    @staticmethod
    def _mapping_viewport(
        vb: Optional[tuple],
        doc_w: float,
        doc_h: float,
    ) -> tuple[float, float, float, float]:
        """Return (min_x, min_y, width, height) of SVG user space to map to document mm."""
        if vb:
            return vb[0], vb[1], max(vb[2], 1e-9), max(vb[3], 1e-9)
        return 0.0, 0.0, max(doc_w, 1e-9), max(doc_h, 1e-9)

    @staticmethod
    def _map_layers_to_document(
        layers: list[SVGLayer],
        vx0: float,
        vy0: float,
        vw: float,
        vh: float,
        doc_w: float,
        doc_h: float,
    ) -> list[SVGLayer]:
        """Map SVG user coordinates into physical document millimeters."""
        out: list[SVGLayer] = []
        for layer in layers:
            new_paths = []
            for seg in layer.paths:
                new_pts = [
                    ((px - vx0) / vw * doc_w, (py - vy0) / vh * doc_h)
                    for px, py in seg.points
                ]
                new_paths.append(PathSegment(
                    points=new_pts,
                    layer=seg.layer,
                    color=seg.color,
                    stroke_width=seg.stroke_width,
                    path_id=seg.path_id,
                ))
            out.append(SVGLayer(
                name=layer.name,
                color=layer.color,
                paths=new_paths,
                enabled=layer.enabled,
                order=layer.order,
                overrides=layer.overrides,
                profile_name=layer.profile_name,
            ))
        return out

    def _parse_viewbox(self, attrs: dict) -> Optional[tuple]:
        vb = attrs.get("viewBox", attrs.get("viewbox", ""))
        if vb:
            parts = vb.replace(",", " ").split()
            if len(parts) == 4:
                try:
                    return tuple(float(p) for p in parts)
                except ValueError:
                    pass

        return None

    @staticmethod
    def _parse_dimension(val: str) -> Optional[float]:
        if not val:
            return None
        val = val.strip()
        for suffix in ("mm", "px", "pt", "in", "cm"):
            if val.endswith(suffix):
                num = val[: -len(suffix)].strip()
                try:
                    v = float(num)
                    if suffix == "in":
                        return v * 25.4
                    if suffix == "cm":
                        return v * 10.0
                    if suffix == "pt":
                        return v * 0.3528
                    if suffix == "px":
                        return v * 25.4 / 96.0
                    return v
                except ValueError:
                    return None
        try:
            return float(val)
        except ValueError:
            return None

    def _document_size_mm(self, attrs: dict, vb: Optional[tuple]) -> tuple[float, float]:
        """Return physical SVG document size in millimeters."""
        w = self._parse_dimension(attrs.get("width", ""))
        h = self._parse_dimension(attrs.get("height", ""))
        if w and h:
            return w, h
        if vb:
            # Many plotter SVGs are authored with unitless viewBox dimensions.
            # Treat those user units as millimeters when no physical size exists.
            return max(vb[2], 1e-9), max(vb[3], 1e-9)
        bed_w, bed_h = self._bed_dims_from_config()
        return bed_w or 300.0, bed_h or 218.0

    def _extract_layer_names(self, root, ns) -> dict:
        """Map group IDs to Inkscape layer labels."""
        names = {}
        for g in root.iter("{http://www.w3.org/2000/svg}g"):
            label = g.get("{http://www.inkscape.org/namespaces/inkscape}label")
            gid = g.get("id", "")
            if label:
                names[gid] = label
                for child in g.iter():
                    cid = child.get("id", "")
                    if cid:
                        names[cid] = label
        return names

    def _get_path_layer(self, attr: dict, layer_names: dict, root, ns) -> str:
        pid = attr.get("id", "")
        if pid in layer_names:
            return layer_names[pid]

        ink_label = attr.get("{http://www.inkscape.org/namespaces/inkscape}label", "")
        if ink_label:
            return ink_label

        style = attr.get("style", "")
        color = self._color_from_style(style)
        if color and color != "#000000":
            return f"color_{color}"

        return "default"

    @staticmethod
    def _get_path_color(attr: dict) -> str:
        style = attr.get("style", "")
        stroke = ""
        for part in style.split(";"):
            kv = part.strip().split(":")
            if len(kv) == 2 and kv[0].strip() == "stroke":
                stroke = kv[1].strip()
                break
        if not stroke:
            stroke = attr.get("stroke", "#000000")
        if stroke == "none":
            stroke = "#000000"
        return stroke

    @staticmethod
    def _color_from_style(style: str) -> str:
        for part in style.split(";"):
            kv = part.strip().split(":")
            if len(kv) == 2 and kv[0].strip() == "stroke":
                return kv[1].strip()
        return ""

    def _sample_path(self, svg_path) -> list[tuple[float, float]]:
        """Sample an svgpathtools Path object into polyline points."""
        try:
            length = svg_path.length()
        except Exception:
            return []

        if length < 0.01:
            return []

        num_samples = max(2, int(length / SAMPLE_INTERVAL_MM) + 1)
        points = []
        for i in range(num_samples):
            t = i / (num_samples - 1)
            try:
                pt = svg_path.point(t)
                points.append((pt.real, pt.imag))
            except Exception:
                continue
        return points

    # ---- vpype integration ----

    def run_vpype(self, operations: list[dict]) -> dict:
        """Run vpype as subprocess. Returns before/after stats."""
        if not self._original_svg_path:
            raise ValueError("No SVG loaded")

        before_stats = {
            "paths": self._current.total_paths() if self._current else 0,
            "distance_mm": round(self._current.total_distance(), 1) if self._current else 0,
        }

        cmd = ["vpype", "read", self._original_svg_path]

        for op in operations:
            name = op.get("name", "")
            if name == "linemerge":
                tol = op.get("tolerance", 0.5)
                cmd += ["linemerge", "--tolerance", str(tol)]
            elif name == "linesort":
                cmd += ["linesort"]
            elif name == "filter":
                min_len = op.get("min_length", 1.0)
                cmd += ["filter", "--min-length", f"{min_len}mm"]
            elif name == "splitall":
                cmd += ["splitall"]
            elif name == "scaleto":
                w = op.get("width")
                h = op.get("height")
                if w is None or h is None:
                    if self._config is not None:
                        try:
                            w = float(self._config.get("bed_width_mm", 300) or 300)
                            h = float(self._config.get("bed_height_mm", 218) or 218)
                        except (TypeError, ValueError):
                            w, h = 300, 218
                    else:
                        w, h = 300, 218
                cmd += ["scaleto", f"{w}mm", f"{h}mm"]

        tmp = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
        tmp.close()
        cmd += ["write", tmp.name]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"vpype error: {result.stderr}")
        except FileNotFoundError:
            raise RuntimeError("vpype not installed or not in PATH")

        original_path = self._original_svg_path
        original_filename = self._current.filename if self._current else "optimized.svg"

        self._optimized_svg_path = tmp.name
        optimized = self.load(tmp.name)
        optimized.filename = original_filename
        self._original_svg_path = original_path

        after_stats = {
            "paths": optimized.total_paths(),
            "distance_mm": round(optimized.total_distance(), 1),
        }

        self._current = optimized
        return {"before": before_stats, "after": after_stats}

    def use_optimized(self, use: bool):
        self._use_optimized = use
        if use and self._optimized_svg_path:
            original_path = self._original_svg_path
            original_filename = self._current.filename if self._current else "optimized.svg"
            self.load(self._optimized_svg_path)
            if self._current:
                self._current.filename = original_filename
            self._original_svg_path = original_path
        elif not use and self._original_svg_path:
            self.load(self._original_svg_path)

    # ---- undo stack ----

    def _push_undo(self):
        if self._current:
            snapshot = copy.deepcopy(self._current)
            self._undo_stack.append(snapshot)
            if len(self._undo_stack) > self._max_undo:
                self._undo_stack.pop(0)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._current = self._undo_stack.pop()
        return True

    # ---- layer management ----

    def set_layer_enabled(self, layer_name: str, enabled: bool):
        if self._current:
            for layer in self._current.layers:
                if layer.name == layer_name:
                    layer.enabled = enabled
                    break

    def set_layer_order(self, order: list[str]):
        if self._current:
            name_map = {l.name: l for l in self._current.layers}
            reordered = []
            for i, name in enumerate(order):
                if name in name_map:
                    name_map[name].order = i
                    reordered.append(name_map[name])
            for l in self._current.layers:
                if l not in reordered:
                    l.order = len(reordered)
                    reordered.append(l)
            self._current.layers = reordered

    def set_layer_overrides(self, layer_name: str, overrides: Optional[dict]):
        if self._current:
            for layer in self._current.layers:
                if layer.name == layer_name:
                    layer.overrides = overrides
                    break

    def set_layer_profile(self, layer_name: str, profile_name: Optional[str]):
        if self._current:
            for layer in self._current.layers:
                if layer.name == layer_name:
                    layer.profile_name = profile_name
                    break

    # ---- path selection & splitting with shapely ----

    def reassign_paths(self, region_type: str, region_params: dict,
                       target_layer: str, mode: str = "select") -> dict:
        """
        Reassign paths inside a selection region to a target layer.
        Paths crossing the boundary are split at intersection points.
        All math done on sampled polylines, not Bezier curves.
        """
        if not self._current:
            return {"error": "No SVG loaded"}

        self._push_undo()

        region = self._make_region(region_type, region_params)
        if region is None:
            return {"error": "Invalid region"}

        target = None
        for layer in self._current.layers:
            if layer.name == target_layer:
                target = layer
                break
        if target is None:
            target = SVGLayer(name=target_layer, order=len(self._current.layers))
            self._current.layers.append(target)

        moved = 0
        split_count = 0

        for layer in self._current.layers:
            if mode == "mask":
                if layer.name != target_layer:
                    continue
            else:
                if layer.name == target_layer:
                    continue
            
            new_paths = []
            for seg in layer.paths:
                line = LineString(seg.points)
                if region.contains(line):
                    if mode == "select":
                        seg.layer = target_layer
                        target.paths.append(seg)
                        moved += 1
                    elif mode == "mask":
                        new_paths.append(seg)
                    else:
                        new_paths.append(seg)
                elif region.intersects(line):
                    inside, outside = self._split_at_region(line, region)
                    if mode == "mask":
                        for part in inside:
                            pts = list(part.coords)
                            if len(pts) >= 2:
                                new_paths.append(PathSegment(
                                    points=pts, layer=layer.name,
                                    color=seg.color, stroke_width=seg.stroke_width, path_id=f"{seg.path_id}_in",
                                ))
                        split_count += 1
                    else:
                        for part in inside:
                            pts = list(part.coords)
                            if len(pts) >= 2:
                                if mode == "select":
                                    new_seg = PathSegment(
                                        points=pts, layer=target_layer,
                                        color=seg.color, stroke_width=seg.stroke_width, path_id=f"{seg.path_id}_in",
                                    )
                                    target.paths.append(new_seg)
                                    moved += 1
                                else:
                                    new_paths.append(PathSegment(
                                        points=pts, layer=layer.name,
                                        color=seg.color, stroke_width=seg.stroke_width, path_id=f"{seg.path_id}_in",
                                    ))
                        for part in outside:
                            pts = list(part.coords)
                            if len(pts) >= 2:
                                if mode == "select":
                                    new_paths.append(PathSegment(
                                        points=pts, layer=layer.name,
                                        color=seg.color, stroke_width=seg.stroke_width, path_id=f"{seg.path_id}_out",
                                    ))
                                else:
                                    new_seg = PathSegment(
                                        points=pts, layer=target_layer,
                                        color=seg.color, stroke_width=seg.stroke_width, path_id=f"{seg.path_id}_out",
                                    )
                                    target.paths.append(new_seg)
                                    moved += 1
                        split_count += 1
                else:
                    if mode != "mask":
                        new_paths.append(seg)
            layer.paths = new_paths

        return {"moved": moved, "split": split_count}

    def extract_region_to_new_layer(self, source_layer: str, region_type: str, region_params: dict) -> dict:
        """
        Extract geometry inside region from a source layer into a new auto-named layer.
        The extracted geometry is removed from the source layer (so it won't plot twice).
        """
        if not self._current:
            return {"error": "No SVG loaded"}

        region = self._make_region(region_type, region_params)
        if region is None:
            return {"error": "Invalid region"}

        src = None
        for layer in self._current.layers:
            if layer.name == source_layer:
                src = layer
                break
        if src is None:
            return {"error": f"Layer not found: {source_layer}"}

        self._push_undo()

        new_layer_name = self._next_numeric_layer_name()
        new_layer = SVGLayer(
            name=new_layer_name,
            color=src.color,
            enabled=True,
            order=max((l.order for l in self._current.layers), default=-1) + 1,
        )

        moved = 0
        split_count = 0
        kept: list[PathSegment] = []
        extracted: list[PathSegment] = []

        for seg in src.paths:
            if len(seg.points) < 2:
                continue
            line = LineString(seg.points)
            if region.contains(line):
                extracted.append(PathSegment(
                    points=seg.points,
                    layer=new_layer_name,
                    color=seg.color,
                    stroke_width=seg.stroke_width,
                    path_id=seg.path_id,
                ))
                moved += 1
                continue

            if region.intersects(line):
                inside, outside = self._split_at_region(line, region)
                for part in outside:
                    pts = list(part.coords)
                    if len(pts) >= 2:
                        kept.append(PathSegment(
                            points=pts,
                            layer=src.name,
                            color=seg.color,
                            stroke_width=seg.stroke_width,
                            path_id=f"{seg.path_id}_out",
                        ))
                for part in inside:
                    pts = list(part.coords)
                    if len(pts) >= 2:
                        extracted.append(PathSegment(
                            points=pts,
                            layer=new_layer_name,
                            color=seg.color,
                            stroke_width=seg.stroke_width,
                            path_id=f"{seg.path_id}_in",
                        ))
                        moved += 1
                split_count += 1
                continue

            kept.append(seg)

        src.paths = kept
        new_layer.paths = extracted
        self._current.layers.append(new_layer)
        self._recompute_bounds()
        return {"new_layer": new_layer_name, "moved": moved, "split": split_count}

    def _make_region(self, region_type: str, params: dict) -> Optional[Polygon]:
        if region_type == "rect":
            x, y = params["x"], params["y"]
            w, h = params["width"], params["height"]
            return Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
        elif region_type == "circle":
            cx, cy, r = params["cx"], params["cy"], params["radius"]
            pts = [(cx + r * math.cos(a), cy + r * math.sin(a))
                   for a in [i * 2 * math.pi / 64 for i in range(64)]]
            return Polygon(pts)
        elif region_type == "lasso":
            pts = params.get("points", [])
            if len(pts) >= 3:
                return Polygon(pts)
        return None

    @staticmethod
    def _split_at_region(line: LineString, region: Polygon):
        inside_parts = []
        outside_parts = []
        try:
            intersection = line.intersection(region)
            difference = line.difference(region)

            for geom in (intersection,):
                if geom.is_empty:
                    continue
                if isinstance(geom, LineString):
                    inside_parts.append(geom)
                elif isinstance(geom, MultiLineString):
                    inside_parts.extend(geom.geoms)

            for geom in (difference,):
                if geom.is_empty:
                    continue
                if isinstance(geom, LineString):
                    outside_parts.append(geom)
                elif isinstance(geom, MultiLineString):
                    outside_parts.extend(geom.geoms)
        except Exception as e:
            logger.warning("Shapely split error: %s", e)

        return inside_parts, outside_parts

    # ---- path generation for plotter ----

    
    def _recompute_bounds(self) -> None:
        if not self._current:
            return
        all_min_x, all_min_y = float("inf"), float("inf")
        all_max_x, all_max_y = float("-inf"), float("-inf")
        for layer in self._current.layers:
            for seg in layer.paths:
                for px, py in seg.points:
                    all_min_x = min(all_min_x, px)
                    all_min_y = min(all_min_y, py)
                    all_max_x = max(all_max_x, px)
                    all_max_y = max(all_max_y, py)
        if all_min_x == float("inf"):
            all_min_x = all_min_y = 0.0
            all_max_x = self._current.width_mm or 0.0
            all_max_y = self._current.height_mm or 0.0
        self._current.min_x = all_min_x
        self._current.min_y = all_min_y
        self._current.max_x = all_max_x
        self._current.max_y = all_max_y

    def align_to_canvas(self, mode: str, canvas_w: float, canvas_h: float) -> None:
        """Translate geometry so bounds align to canvas edges/centers."""
        if not self._current:
            return
        self._push_undo()
        self._recompute_bounds()
        b = self._current
        if mode in ("left", "center_x", "right"):
            if mode == "left":
                dx = -b.min_x
            elif mode == "center_x":
                dx = (canvas_w / 2.0) - ((b.min_x + b.max_x) / 2.0)
            else:
                dx = canvas_w - b.max_x
        else:
            dx = 0.0

        if mode in ("top", "center_y", "bottom"):
            if mode == "top":
                dy = -b.min_y
            elif mode == "center_y":
                dy = (canvas_h / 2.0) - ((b.min_y + b.max_y) / 2.0)
            else:
                dy = canvas_h - b.max_y
        else:
            dy = 0.0

        if mode == "center":
            dx = (canvas_w / 2.0) - ((b.min_x + b.max_x) / 2.0)
            dy = (canvas_h / 2.0) - ((b.min_y + b.max_y) / 2.0)

        if dx == 0.0 and dy == 0.0:
            return
        for layer in self._current.layers:
            for seg in layer.paths:
                seg.points = [(px + dx, py + dy) for px, py in seg.points]
        self._recompute_bounds()

    def scale_about_center(self, factor: float) -> None:
        """Scale all points about the current bounds center."""
        if not self._current:
            return
        self._push_undo()
        self._recompute_bounds()
        cx = (self._current.min_x + self._current.max_x) / 2.0
        cy = (self._current.min_y + self._current.max_y) / 2.0
        for layer in self._current.layers:
            for seg in layer.paths:
                seg.points = [((px - cx) * factor + cx, (py - cy) * factor + cy) for px, py in seg.points]
        self._recompute_bounds()

    def translate(self, dx: float, dy: float) -> None:
        """Translate all geometry by (dx, dy) in current coordinate units (mm)."""
        if not self._current:
            return
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            return
        self._push_undo()
        for layer in self._current.layers:
            for seg in layer.paths:
                seg.points = [(px + dx, py + dy) for px, py in seg.points]
        self._recompute_bounds()

    def get_plot_paths(self, dip_threshold_mm: float = 0, scale: float = 1.0, offset_x: float = 0.0, offset_y: float = 0.0, layer_name: str = None) -> list[dict]:
        """
        Return ordered list of move instructions for the plotter.
        Each entry: {"type": "travel"/"draw", "x": float, "y": float, "layer": str}
        If dip_threshold_mm > 0, inserts dip markers when distance exceeds threshold.
        """
        if not self._current:
            return []

        instructions = []
        running_distance = 0.0

        for layer in sorted(self._current.enabled_layers(), key=lambda l: l.order):
            if layer_name and layer.name != layer_name:
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

                        running_distance += step_dist
                        if running_distance >= dip_threshold_mm:
                            instructions.append({
                                "type": "dip",
                                "return_x": px,
                                "return_y": py,
                            })
                            running_distance = 0.0
                    instructions.append({"type": "draw", "x": px, "y": py})

                instructions.append({"type": "pen_up"})

            instructions.append({"type": "layer_end", "layer": layer.name})

        return instructions

    def estimate_dips(self, dip_threshold_mm: float) -> int:
        if not self._current or dip_threshold_mm <= 0:
            return 0
        total = self._current.total_distance()
        return max(0, int(total / dip_threshold_mm))

    def estimate_time_seconds(self, speed_pct: float = 25, travel_speed_pct: float = 75) -> float:
        if not self._current:
            return 0
        draw_speed = max(1, (speed_pct / 100.0) * 110.0)
        travel_speed = max(1, (travel_speed_pct / 100.0) * 110.0)
        draw_dist = self._current.total_distance()
        travel_dist = draw_dist * 0.3  # rough estimate of travel overhead
        return (draw_dist / draw_speed) + (travel_dist / travel_speed)
