import re

def extract(pattern, html, flags=re.DOTALL):
    match = re.search(pattern, html, flags=flags)
    if not match:
        print(f"Warning: could not match {pattern}")
        return ""
    return match.group(0)

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# 1. Extract the components
top_bar = extract(r'<div id="top-bar">.*?</div>\s*<!-- \s*-->', html) or extract(r'<div id="top-bar">.*?</div>', html)
telemetry = extract(r'<div class="panel">\s*<h3>Telemetry</h3>.*?</div>\s*</div>\s*</div>\s*</div>', html) 
if not telemetry: telemetry = extract(r'<div class="panel">\s*<h3>Telemetry</h3>.*?</div>\s*</div>', html) # fallback
manual = extract(r'<div class="panel">\s*<h3>Manual Controls</h3>.*?</div>\s*</div>\s*</div>', html)
settings = extract(r'<div class="panel">\s*<h3>Plot Settings</h3>.*?</div>\s*</div>', html)

svg_upload = extract(r'<div class="card">\s*<h3>SVG Upload</h3>.*?</div>\s*</div>\s*</div>', html)
execution = extract(r'<div class="card" style="margin-bottom:0;flex:1">\s*<h3>Execution</h3>.*?</div>\s*</div>', html)
queue = extract(r'<div class="card" style="margin-bottom:0;flex:1" id="queue-card">.*?</div>\s*</div>', html)

vpype = extract(r'<div class="card" id="vpype-card".*?</div>\s*</div>', html)
layers = extract(r'<div class="card" id="layers-card".*?</div>\s*</div>', html)

preview_toolbar = extract(r'<div id="preview-toolbar".*?</div>', html)
paper_settings = extract(r'<div id="paper-settings".*?</div>', html)
svg_preview = extract(r'<div id="svg-preview-container".*?</div>', html)

paint_view = extract(r'<div class="bottom-view tab-content" id="view-paint">.*?</div>\s*</div>\s*</div>\s*</div>', html)
term_view = extract(r'<div class="bottom-view tab-content" id="view-terminal">.*?</div>', html)
hist_view = extract(r'<div class="bottom-view tab-content" id="view-history">.*?</div>\s*</div>\s*</div>', html)

# Fix telemetry match which might have grabbed too much
telemetry = re.search(r'<div class="panel">\s*<h3>Telemetry</h3>.*?<div style="font-size:0\.6rem;color:var\(--text-muted\);margin-top:2px" id="progress-label">0 / 0 mm</div>\s*</div>', html, flags=re.DOTALL)
telemetry = telemetry.group(0) if telemetry else ""

manual = re.search(r'<div class="panel">\s*<h3>Manual Controls</h3>.*?<button onclick="api\(\'/api/dry_run\'\)">Dry Run</button>\s*</div>\s*</div>\s*</div>\s*</div>', html, flags=re.DOTALL)
manual = manual.group(0) if manual else ""

settings = re.search(r'<div class="panel">\s*<h3>Plot Settings</h3>.*?<div class="form-row" style="margin-top:4px"><label><input type="checkbox" id="s-const-speed"> Constant Velocity</label></div>\s*</div>', html, flags=re.DOTALL)
settings = settings.group(0) if settings else ""

svg_upload = re.search(r'<div class="card">\s*<h3>SVG Upload</h3>.*?<div id="file-list".*?</div>\s*</div>\s*</div>', html, flags=re.DOTALL)
svg_upload = svg_upload.group(0) if svg_upload else ""

execution = re.search(r'<div class="card"[^>]*>\s*<h3>Execution</h3>.*?<button class="danger"[^>]*>E-STOP</button>\s*</div>\s*</div>', html, flags=re.DOTALL)
execution = execution.group(0) if execution else ""

queue = re.search(r'<div class="card"[^>]*id="queue-card"[^>]*>.*?<button[^>]*>Add to Queue</button>\s*</div>', html, flags=re.DOTALL)
queue = queue.group(0) if queue else ""

vpype = re.search(r'<div class="card" id="vpype-card".*?<div id="vpype-stats".*?</div>\s*</div>', html, flags=re.DOTALL)
vpype = vpype.group(0) if vpype else ""

layers = re.search(r'<div class="card" id="layers-card".*?<div id="layer-list"></div>\s*</div>', html, flags=re.DOTALL)
layers = layers.group(0) if layers else ""

# Extract top bar specifically 
top_bar = re.search(r'<div id="top-bar">.*?<span id="firmware-info" class="fw-info"></span>\s*</div>', html, flags=re.DOTALL)
top_bar = top_bar.group(0) if top_bar else ""

# Extract tab contents
paint_view = re.search(r'<div class="bottom-view tab-content" id="view-paint">.*?<div id="tray-coords".*?</div>\s*</div>\s*</div>', html, flags=re.DOTALL)
paint_view = paint_view.group(0) if paint_view else ""

term_view = re.search(r'<div class="bottom-view tab-content" id="view-terminal">.*?</div>', html, flags=re.DOTALL)
term_view = term_view.group(0) if term_view else ""

hist_view = re.search(r'<div class="bottom-view tab-content" id="view-history">.*?<div id="history-empty".*?</div>\s*</div>\s*</div>', html, flags=re.DOTALL)
hist_view = hist_view.group(0) if hist_view else ""

preview_toolbar = re.search(r'<div id="preview-toolbar".*?<button onclick="svgUndo\(\)".*?Undo</button>\s*</div>', html, flags=re.DOTALL)
preview_toolbar = preview_toolbar.group(0) if preview_toolbar else ""

paper_settings = re.search(r'<div id="paper-settings".*?<span id="svg-scale-val">100%</span>\s*</div>', html, flags=re.DOTALL)
paper_settings = paper_settings.group(0) if paper_settings else ""

svg_preview = re.search(r'<div id="svg-preview-container".*?</svg>\s*</div>', html, flags=re.DOTALL)
svg_preview = svg_preview.group(0) if svg_preview else ""


# 2. Build the new layout
new_layout = f"""<div id="main" style="display:flex; flex-direction:row; flex:1; overflow:hidden;">

  <!-- LEFT PANEL (65%) -->
  <div id="left-panel" style="width:65%; overflow-y:auto; border-right:1px solid var(--border); display:flex; flex-direction:column; padding-bottom:40px;">
    
    <!-- HOME VIEW -->
    <div class="bottom-view active" id="view-home" style="display:flex; flex-direction:column; padding:10px; gap:10px;">
      {top_bar}
      {telemetry}
      {manual}
      {svg_upload}
      
      <div style="display:flex; gap:10px;">
        {execution}
        {queue}
      </div>

      {settings}

      <div style="display:flex; gap:10px;">
        {vpype}
        {layers}
      </div>
    </div><!-- /view-home -->
    
    {paint_view}
    {term_view}
    {hist_view}

  </div><!-- /left-panel -->

  <!-- RIGHT PANEL (35%) -->
  <div id="right-panel" style="width:35%; background:var(--surface2); display:flex; flex-direction:column;">
    {paper_settings}
    {preview_toolbar}
    {svg_preview}
  </div><!-- /right-panel -->

</div><!-- /main -->"""

# Remove old `#main` from HTML
start_idx = html.find('<div id="main">')
end_idx = html.find('</div><!-- /main -->') + len('</div><!-- /main -->')

if start_idx != -1 and end_idx != -1:
    new_html = html[:start_idx] + new_layout + html[end_idx:]
    with open("static/index.html", "w", encoding="utf-8") as f:
        f.write(new_html)
    print("UI completely rebuilt and applied.")
else:
    print("Error: Could not locate #main block.")

