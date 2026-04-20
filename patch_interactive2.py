import re

with open('static/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix the CSS rule for toolbar buttons
html = html.replace('.selection-toolbar button.active', '#preview-toolbar button.active')

# Re-inject the correct pan, zoom, and selection logic
idx1 = html.find('// ===== Pan and Zoom =====')
idx2 = html.find('async function applySelection')

if idx1 != -1 and idx2 != -1:
    new_js = """// ===== Interactive Canvas Controls =====
let selTool = 'pan'; // Default tool
let selRegion = null;
let isPanning = false;
let isDrawing = false;
let panStart = { x: 0, y: 0 };
let offsetStart = { x: 0, y: 0 };
let selStart = null;
let lassoPoints = [];

// Initialize default tool highlighting
document.addEventListener('DOMContentLoaded', () => {
  setSelTool('pan');
});

function setSelTool(tool) {
  selTool = tool;
  document.querySelectorAll('#preview-toolbar button').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('sel-'+tool);
  if (btn) btn.classList.add('active');
}

const svgPreviewContainer = document.getElementById('svg-preview-container');

svgPreviewContainer.addEventListener('mousedown', e => {
  // If pan tool selected OR middle mouse clicked, start panning
  if (selTool === 'pan' || e.button === 1) { 
    e.preventDefault();
    isPanning = true;
    panStart = { x: e.clientX, y: e.clientY };
    offsetStart = {
      x: parseFloat(document.getElementById('svg-offset-x').value) || 0,
      y: parseFloat(document.getElementById('svg-offset-y').value) || 0
    };
    svgPreviewContainer.style.cursor = 'grabbing';
    return;
  }
  
  // If drawing tool selected and left mouse clicked, start drawing
  if (e.button === 0 && (selTool === 'rect' || selTool === 'circle' || selTool === 'lasso')) {
    e.preventDefault();
    isDrawing = true;
    const rect = svgPreviewContainer.getBoundingClientRect();
    selStart = {x: e.clientX - rect.left, y: e.clientY - rect.top, rx: rect.width, ry: rect.height};
    
    // Clear previous visual shape
    const existing = document.getElementById('sel-preview-shape');
    if (existing) existing.remove();
    
    const svg = document.getElementById('svg-preview');
    const shapeType = selTool === 'rect' ? 'rect' : selTool === 'circle' ? 'circle' : 'polyline';
    const shape = document.createElementNS("http://www.w3.org/2000/svg", shapeType);
    shape.id = 'sel-preview-shape';
    shape.setAttribute('fill', 'rgba(88,166,255,0.3)');
    shape.setAttribute('stroke', '#58a6ff');
    shape.setAttribute('stroke-width', '1');
    shape.setAttribute('stroke-dasharray', '4');
    
    if (selTool === 'lasso') {
      lassoPoints = [{x: selStart.x, y: selStart.y}];
      shape.setAttribute('fill', 'rgba(88,166,255,0.1)'); // lighter for lasso
    }
    
    svg.appendChild(shape);
  }
});

window.addEventListener('mousemove', e => {
  const svg = document.getElementById('svg-preview');
  if (!svg || !svg.viewBox.baseVal) return;
  const vb = svg.viewBox.baseVal;
  const rect = svg.getBoundingClientRect();
  const ratioX = vb.width / rect.width;
  const ratioY = vb.height / rect.height;

  if (isPanning) {
    e.preventDefault();
    const dx = (e.clientX - panStart.x) * ratioX;
    const dy = (e.clientY - panStart.y) * ratioY;
    document.getElementById('svg-offset-x').value = (offsetStart.x + dx).toFixed(2);
    document.getElementById('svg-offset-y').value = (offsetStart.y + dy).toFixed(2);
    updateSvgTransform();
    return;
  }
  
  if (isDrawing && selStart) {
    e.preventDefault();
    const ex = e.clientX - rect.left;
    const ey = e.clientY - rect.top;
    
    const sx = (x) => vb.x + (x / rect.width) * vb.width;
    const sy = (y) => vb.y + (y / rect.height) * vb.height;
    
    const shape = document.getElementById('sel-preview-shape');
    if (!shape) return;
    
    if (selTool === 'rect') {
      const x1 = sx(Math.min(selStart.x, ex));
      const y1 = sy(Math.min(selStart.y, ey));
      const w = Math.abs(sx(ex) - sx(selStart.x));
      const h = Math.abs(sy(ey) - sy(selStart.y));
      shape.setAttribute('x', x1);
      shape.setAttribute('y', y1);
      shape.setAttribute('width', w);
      shape.setAttribute('height', h);
    } else if (selTool === 'circle') {
      const cx = sx((selStart.x + ex) / 2);
      const cy = sy((selStart.y + ey) / 2);
      const r = Math.max(Math.abs(sx(ex) - sx(selStart.x)), Math.abs(sy(ey) - sy(selStart.y))) / 2;
      shape.setAttribute('cx', cx);
      shape.setAttribute('cy', cy);
      shape.setAttribute('r', r);
    } else if (selTool === 'lasso') {
      lassoPoints.push({x: ex, y: ey});
      const pointsStr = lassoPoints.map(p => `${sx(p.x)},${sy(p.y)}`).join(' ');
      shape.setAttribute('points', pointsStr);
    }
  }
});

window.addEventListener('mouseup', e => {
  if (isPanning) {
    isPanning = false;
    svgPreviewContainer.style.cursor = 'default';
  }
  
  if (isDrawing && selStart) {
    isDrawing = false;
    const svg = document.getElementById('svg-preview');
    const rect = svg.getBoundingClientRect();
    const ex = e.clientX - rect.left;
    const ey = e.clientY - rect.top;
    const vb = svg.viewBox.baseVal;
    
    const sx = (x) => vb.x + (x / rect.width) * vb.width;
    const sy = (y) => vb.y + (y / rect.height) * vb.height;
    
    if (selTool === 'rect') {
      selRegion = {
        type: 'rect', 
        params: {
          x: sx(Math.min(selStart.x, ex)), 
          y: sy(Math.min(selStart.y, ey)),
          width: Math.abs(sx(ex) - sx(selStart.x)), 
          height: Math.abs(sy(ey) - sy(selStart.y))
        }
      };
    } else if (selTool === 'circle') {
      const cx = sx((selStart.x + ex) / 2);
      const cy = sy((selStart.y + ey) / 2);
      const r = Math.max(Math.abs(sx(ex) - sx(selStart.x)), Math.abs(sy(ey) - sy(selStart.y))) / 2;
      selRegion = {type: 'circle', params: {cx, cy, radius: r}};
    } else if (selTool === 'lasso') {
      const coords = lassoPoints.map(p => [sx(p.x), sy(p.y)]);
      selRegion = {type: 'lasso', params: {points: coords}};
    }
    selStart = null;
  }
});

svgPreviewContainer.addEventListener('wheel', e => {
  e.preventDefault();
  
  const svg = document.getElementById('svg-preview');
  if (!svg || !svg.viewBox.baseVal) return;
  
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  
  // Mouse position in screen pixels relative to SVG element
  const mouseX = e.clientX - rect.left;
  const mouseY = e.clientY - rect.top;
  
  // Mouse position in SVG paper units
  const paperX = vb.x + (mouseX / rect.width) * vb.width;
  const paperY = vb.y + (mouseY / rect.height) * vb.height;
  
  // Current scale and offset
  const scaleInput = document.getElementById('svg-scale');
  let currentScale = parseFloat(scaleInput.value) || 100;
  const s = currentScale / 100.0;
  
  const offsetXInput = document.getElementById('svg-offset-x');
  const offsetYInput = document.getElementById('svg-offset-y');
  let ox = parseFloat(offsetXInput.value) || 0;
  let oy = parseFloat(offsetYInput.value) || 0;
  
  // Mouse position relative to the unscaled/untranslated paths
  // paperX = pathX * s + ox  =>  pathX = (paperX - ox) / s
  const pathX = (paperX - ox) / s;
  const pathY = (paperY - oy) / s;
  
  // Determine new scale
  const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9; // Scroll up zooms IN
  let newScale = currentScale * zoomFactor;
  newScale = Math.max(10, Math.min(3000, newScale)); // clamp between 10% and 3000%
  const newS = newScale / 100.0;
  
  // Calculate new offset so that pathX/pathY remains under mouse
  // paperX = pathX * newS + newOx  =>  newOx = paperX - pathX * newS
  const newOx = paperX - pathX * newS;
  const newOy = paperY - pathY * newS;
  
  scaleInput.value = newScale.toFixed(1);
  offsetXInput.value = newOx.toFixed(2);
  offsetYInput.value = newOy.toFixed(2);
  
  updateSvgTransform();
});

"""
    html = html[:idx1] + new_js + html[idx2:]

    with open('static/index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Patched index.html")
else:
    print("Could not find boundaries")
