import re

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# Add Pan button and Reset button to preview toolbar
toolbar_pattern = r'<button id="sel-rect"'
pan_btn = '<button id="sel-pan" onclick="setSelTool(\'pan\')" style="font-size:0.72rem;padding:2px 6px">🖐 Pan</button>\n        <button id="sel-rect"'
html = re.sub(toolbar_pattern, pan_btn, html)

toolbar_end_pattern = r'<button onclick="svgUndo\(\)"[^>]*>Undo</button>'
reset_btn = '<button onclick="svgUndo()" style="font-size:0.72rem;padding:2px 6px">Undo</button>\n        <button onclick="resetSvgView()" style="font-size:0.72rem;padding:2px 6px">Reset</button>'
html = re.sub(toolbar_end_pattern, reset_btn, html)

# Add pan state variables and wheel listener to SVG preview container
# First, update the SVG transform logic to correctly apply scale and offset
update_transform_pattern = r'function updateSvgTransform\(\) \{.*?(?=\n\})'
update_transform_new = r"""function updateSvgTransform() {
  const scale = parseFloat(document.getElementById('svg-scale').value) || 100;
  document.getElementById('svg-scale-val').textContent = scale + '%';
  const g = document.getElementById('svg-paths-group');
  if (g) {
    const s = scale / 100.0;
    const ox = parseFloat(document.getElementById('svg-offset-x').value) || 0;
    const oy = parseFloat(document.getElementById('svg-offset-y').value) || 0;
    g.setAttribute('transform', `translate(${ox}, ${oy}) scale(${s})`);
  }
"""
html = re.sub(update_transform_pattern, update_transform_new, html, flags=re.DOTALL)

# Now, add the Pan and Zoom logic to the JS
js_injection = """
// ===== Pan and Zoom =====
let isPanning = false;
let panStart = { x: 0, y: 0 };
let offsetStart = { x: 0, y: 0 };

document.getElementById('svg-preview-container').addEventListener('mousedown', e => {
  if (selTool === 'pan' || e.button === 1) { // Left click if Pan tool, or Middle click anytime
    e.preventDefault();
    isPanning = true;
    panStart = { x: e.clientX, y: e.clientY };
    offsetStart = {
      x: parseFloat(document.getElementById('svg-offset-x').value) || 0,
      y: parseFloat(document.getElementById('svg-offset-y').value) || 0
    };
    document.getElementById('svg-preview-container').style.cursor = 'grabbing';
  }
});

window.addEventListener('mousemove', e => {
  if (!isPanning) return;
  e.preventDefault();
  
  // Calculate movement in SVG viewBox units
  const container = document.getElementById('svg-preview-container');
  const svg = document.getElementById('svg-preview');
  if (!svg || !svg.viewBox.baseVal) return;
  
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  
  // Ratio of SVG units to Screen pixels
  const ratioX = vb.width / rect.width;
  const ratioY = vb.height / rect.height;
  
  const dx = (e.clientX - panStart.x) * ratioX;
  const dy = (e.clientY - panStart.y) * ratioY;
  
  document.getElementById('svg-offset-x').value = (offsetStart.x + dx).toFixed(2);
  document.getElementById('svg-offset-y').value = (offsetStart.y + dy).toFixed(2);
  updateSvgTransform();
});

window.addEventListener('mouseup', e => {
  if (isPanning) {
    isPanning = false;
    document.getElementById('svg-preview-container').style.cursor = 'default';
  }
});

document.getElementById('svg-preview-container').addEventListener('wheel', e => {
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
  const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9; // 10% zoom per tick
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

function resetSvgView() {
  document.getElementById('svg-scale').value = 100;
  document.getElementById('svg-offset-x').value = 0;
  document.getElementById('svg-offset-y').value = 0;
  updateSvgTransform();
}
"""

# Inject before Selection tools
selection_tool_idx = html.find('// ===== Selection tools =====')
if selection_tool_idx != -1:
    html = html[:selection_tool_idx] + js_injection + "\n" + html[selection_tool_idx:]
else:
    html += js_injection

with open("static/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Applied pan/zoom fixes.")
