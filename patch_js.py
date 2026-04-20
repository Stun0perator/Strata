import re

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# 1. Update `api` function to support DELETE
api_new = """async function api(url, body=null, method='POST') {
  try {
    const opts = body ? {method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {method:method};
    const r = await fetch(url, opts);
    const d = await r.json();
    if (d.error) { toast(d.error,'error'); return null; }
    return d;
  } catch(e) { toast('Request failed: '+e.message,'error'); return null; }
}"""
html = re.sub(r'async function api\(url, body=null\) \{.*?(?=async function apiGet)', api_new, html, flags=re.DOTALL)

# 2. Add Delete File button
delete_file_js = """
async function deleteFile(filename, e) {
  e.stopPropagation();
  if (confirm(`Delete file ${filename}?`)) {
    const d = await api(`/api/files/${filename}`, null, 'DELETE');
    if (d && d.ok) {
        toast(`Deleted ${filename}`, 'success');
        loadFiles();
    }
  }
}
"""
html = html.replace('function handleFileUpload', delete_file_js + '\nfunction handleFileUpload')

# Add the X button to the file list renderer
old_file_renderer = """
        <div class="file-item" style="display: flex; justify-content: space-between; padding: 4px 4px; cursor: pointer; border-bottom: 1px solid var(--border);" onclick="loadFile('${f.name}')" onmouseover="this.style.background='var(--accent)';" onmouseout="this.style.background='transparent';">
          <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 70%; pointer-events: none;">${f.name}</span>
          <span style="color: var(--text-muted); pointer-events: none;">${(f.size/1024).toFixed(1)} KB</span>
        </div>
"""
new_file_renderer = """
        <div class="file-item" style="display: flex; justify-content: space-between; padding: 4px 4px; cursor: pointer; border-bottom: 1px solid var(--border);" onclick="loadFile('${f.name}')" onmouseover="this.style.background='var(--surface)'; " onmouseout="this.style.background='transparent';">
          <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; pointer-events: none;">${f.name}</span>
          <div>
            <span style="color: var(--text-muted); pointer-events: none; margin-right:6px;">${(f.size/1024).toFixed(1)} KB</span>
            <button onclick="deleteFile('${f.name}', event)" style="padding:0 6px; color:var(--red); font-weight:bold; font-size:0.7rem; background:transparent;">X</button>
          </div>
        </div>
"""
html = html.replace(old_file_renderer, new_file_renderer)

# 3. Add Plot Layer button to buildLayerList
layer_row_new = """
      <span class="layer-info">${l.path_count} paths, ${l.distance_mm} mm</span>
      <button onclick="plotLayer('${l.name}')" style="font-size:0.65rem; padding:2px 6px; margin-left:8px;" class="success">Plot</button>`;
"""
html = re.sub(r'<span class="layer-info">\$\{l\.path_count\} paths, \$\{l\.distance_mm\} mm</span>.*?Set Mask</button>`;', layer_row_new, html, flags=re.DOTALL)

# 4. Implement plotLayer and update startPlot
plot_layer_js = """
async function plotLayer(layerName) {
  const scale = document.getElementById('svg-scale').value / 100.0;
  const dx = document.getElementById('svg-offset-x').value || 0;
  const dy = document.getElementById('svg-offset-y').value || 0;
  const d = await api('/api/plot/start', {layer_name: layerName, scale: scale, offset_x: dx, offset_y: dy});
  if (d && d.ok) toast(`Started plotting ${layerName}`, 'success');
}
"""
html = html.replace('async function startPlot() {', plot_layer_js + '\nasync function startPlot() {')

# update startPlot to send args
start_plot_new = """async function startPlot() {
  const scale = document.getElementById('svg-scale').value / 100.0;
  const dx = document.getElementById('svg-offset-x').value || 0;
  const dy = document.getElementById('svg-offset-y').value || 0;
  const d = await api('/api/plot/start', {scale: scale, offset_x: dx, offset_y: dy});
  if (d && d.ok) toast('Plot started', 'success');
}"""
html = re.sub(r'async function startPlot\(\) \{.*?(?=async function pausePlot\(\))', start_plot_new + "\n", html, flags=re.DOTALL)

# 5. Implement setAsMask
mask_js = """
async function setAsMask() {
  const layer = document.getElementById('target-layer-select').value;
  if (layer === '__new__' || !selRegion) return toast("Select a layer and draw a shape first", "error");
  
  let pts = [];
  if (selTool === 'rect') {
    pts = [ [selRegion.x, selRegion.y], [selRegion.x+selRegion.w, selRegion.y], [selRegion.x+selRegion.w, selRegion.y+selRegion.h], [selRegion.x, selRegion.y+selRegion.h] ];
  } else if (selTool === 'circle') {
    const cx = selRegion.x + selRegion.r;
    const cy = selRegion.y + selRegion.r;
    for(let i=0; i<16; i++) {
      pts.push([cx + Math.cos(i*Math.PI/8)*selRegion.r, cy + Math.sin(i*Math.PI/8)*selRegion.r]);
    }
  } else if (selTool === 'lasso') {
    pts = selRegion.points;
  }
  
  if (pts.length < 3) return toast("Mask shape is too small", "error");
  
  const d = await api('/api/layers/mask', {layer: layer, polygon: pts});
  if (d && d.ok) {
     toast("Mask applied!");
     clearSelection();
  }
}
"""

html = html.replace('function setAsMask() {\n  // implemented in masking code block later\n}', mask_js)

with open("static/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("index.html Javascript patched successfully.")
