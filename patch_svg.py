import re

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# Replace the showSvgPreview function
new_showSvgPreview = """function showSvgPreview(data) {
  console.log("showSvgPreview executing with data:", data);
  try {
    const svg = document.getElementById('svg-preview');
    // Clear existing children
    while (svg.firstChild) {
      svg.removeChild(svg.firstChild);
    }
    
    // Determine paper size
    let [pw, ph] = document.getElementById('paper-size').value.split(',').map(Number);
    if (!pw) { pw = 215.9; ph = 279.4; } // fallback
    
    svg.setAttribute('viewBox', `0 0 ${pw} ${ph}`);
    
    const ns = "http://www.w3.org/2000/svg";

    // Build the paths group
    const g = document.createElementNS(ns, 'g');
    g.id = "svg-paths-group";
    
    const s = document.getElementById('svg-scale').value / 100.0;
    // We will just scale for now. Offset is harder without inputs.
    g.setAttribute('transform', `scale(${s})`);
    
    let pathsAdded = 0;
    for (const layer of data.layers) {
      if (!layer.enabled) continue;
      for (const path of layer.paths) {
        if (path.points && path.points.length >= 2) {
          const pathEl = document.createElementNS(ns, 'path');
          const d = 'M' + path.points.map(p => p[0]+','+p[1]).join(' L');
          pathEl.setAttribute('d', d);
          pathEl.setAttribute('fill', 'none');
          pathEl.setAttribute('stroke', path.color || layer.color || '#000');
          pathEl.setAttribute('stroke-width', (0.5 / Math.max(0.1, s)).toFixed(2));
          pathEl.setAttribute('opacity', '0.9');
          g.appendChild(pathEl);
          pathsAdded++;
        }
      }
    }
    
    svg.appendChild(g);
    
    console.log(`showSvgPreview added ${pathsAdded} paths to the DOM.`);
    updateTargetLayerSelect(data.layers);
  } catch (err) {
    console.error("Error in showSvgPreview:", err);
  }
}

function updatePaperSize() {
    if (previewData) showSvgPreview(previewData);
}

function updateSvgTransform() {
    const s = document.getElementById('svg-scale').value;
    document.getElementById('svg-scale-val').textContent = s + '%';
    if (previewData) showSvgPreview(previewData);
}
"""

html = re.sub(r'function showSvgPreview\(data\) \{.*?(?=function updateTargetLayerSelect)', new_showSvgPreview, html, flags=re.DOTALL)

with open("static/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("showSvgPreview updated.")
