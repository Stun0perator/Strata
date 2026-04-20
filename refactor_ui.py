import re

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

# 1. Remove old CSS classes for layout
html = re.sub(r'/\* middle section - 3 columns \*/.*?/\* telemetry - left panel compact \*/', '/* telemetry - left panel compact */', html, flags=re.DOTALL)
html = re.sub(r'#middle-section\s*\{.*?\n.*?\n.*?\}', '', html)
html = re.sub(r'#middle-section\s*>\s*\.panel\s*\{.*?\}', '', html)
html = re.sub(r'#middle-section\s*>\s*\.panel:last-child\s*\{.*?\}', '', html)
html = re.sub(r'#middle-section\s*>\s*\.panel:nth-child\(2\).*?\}', '', html)
html = re.sub(r'#middle-section\s*>\s*\.panel:last-child .*?\}', '', html)
html = re.sub(r'#bottom-section\s*\{.*?\}', '', html)
html = re.sub(r'\.bottom-view\s*\{.*?\}', '', html)
html = re.sub(r'\.bottom-view\.active\s*\{.*?\}', '', html)
html = re.sub(r'#view-home\s*\{.*?\}', '', html)
html = re.sub(r'#view-home\.active\s*\{.*?\}', '', html)
html = re.sub(r'#view-home > \.file-col\s*\{.*?\}', '', html)

# Replace `#svg-preview-container` CSS to adapt to new layout
html = re.sub(r'#svg-preview-container \{.*?\}', '', html, flags=re.DOTALL)

# 2. Extract `#middle-section` contents
match_mid = re.search(r'<!-- ========== MIDDLE SECTION — 3 columns ========== -->\s*<div id="middle-section">(.*?)</div><!-- /middle-section -->', html, flags=re.DOTALL)
if not match_mid:
    match_mid = re.search(r'<!-- ========== MIDDLE SECTION - 3 columns ========== -->\s*<div id="middle-section">(.*?)</div><!-- /middle-section -->', html, flags=re.DOTALL)

mid_content = match_mid.group(1) if match_mid else ""
html = re.sub(r'<!-- ========== MIDDLE SECTION — 3 columns ========== -->\s*<div id="middle-section">.*?</div><!-- /middle-section -->', '', html, flags=re.DOTALL)
html = re.sub(r'<!-- ========== MIDDLE SECTION - 3 columns ========== -->\s*<div id="middle-section">.*?</div><!-- /middle-section -->', '', html, flags=re.DOTALL)

# 3. Restructure `bottom-section` into the new 2-column layout
new_main_layout = f"""
  <div style="display:flex; flex:1; overflow:hidden;">
    <!-- LEFT PANEL (50%) -->
    <div id="left-panel" style="width:50%; overflow-y:auto; border-right:1px solid var(--border); padding-bottom:40px;">
      
      <!-- HOME VIEW -->
      <div class="bottom-view active" id="view-home" style="display:none; flex-direction:column; padding:10px;">
        {mid_content}
"""

html = re.sub(r'<!-- ========== BOTTOM SECTION ========== -->\s*<div id="bottom-section">\s*<!-- HOME VIEW: File & Execution \(left\) \+ Webcam \(right\) -->\s*<div class="bottom-view active" id="view-home">\s*<!-- Left: File & Execution -->\s*<div class="file-col">', new_main_layout, html, flags=re.DOTALL)

# Now we need to extract the `preview-card` from `file-col` and put it in the RIGHT PANEL.
match_preview = re.search(r'<div class="card" id="preview-card" style="display:none">(.*?)</div>\s*<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">', html, flags=re.DOTALL)
if match_preview:
    preview_inner = match_preview.group(1)
    # Remove it from the left panel
    html = html.replace(match_preview.group(0), '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">')

    # Close left panel and open right panel
    # We find `</div><!-- /view-home -->` and replace it
    
    right_panel = f"""
      </div><!-- /view-home -->
      
      <!-- Paint, Terminal, History tabs will just follow here inside left-panel -->
"""
    html = html.replace("</div><!-- /view-home -->", right_panel, 1)

    # Finally, close left-panel and add right-panel before `</div><!-- /main -->`
    end_layout = f"""
    </div><!-- /left-panel -->

    <!-- RIGHT PANEL (50%) -->
    <div id="right-panel" style="width:50%; background:var(--surface2); display:flex; flex-direction:column;">
      <div id="preview-toolbar" style="padding:10px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
        <div style="font-weight:600; font-size:0.8rem; color:var(--text); margin-right:10px;">SVG Preview</div>
        <button id="sel-rect" class="active" onclick="setSelTool('rect')" style="font-size:0.72rem;padding:2px 6px">&#9634; Rect</button>
        <button id="sel-circle" onclick="setSelTool('circle')" style="font-size:0.72rem;padding:2px 6px">&#9675; Circle</button>
        <button id="sel-lasso" onclick="setSelTool('lasso')" style="font-size:0.72rem;padding:2px 6px">&#10047; Lasso</button>
        <span style="flex:1"></span>
        <select id="target-layer-select" style="font-size:0.72rem;padding:2px 4px"><option value="__new__">New Layer...</option></select>
        <button onclick="applySelection()" style="font-size:0.72rem;padding:2px 6px">Apply Mask</button>
        <button onclick="setAsMask()" style="font-size:0.72rem;padding:2px 6px">Set as Mask</button>
        <button onclick="svgUndo()" style="font-size:0.72rem;padding:2px 6px">Undo</button>
      </div>
      
      <div id="paper-settings" style="padding:6px 10px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; gap:10px; align-items:center; font-size:0.75rem;">
        <label>Paper Size:</label>
        <select id="paper-size" onchange="updatePaperSize()" style="font-size:0.7rem;padding:2px 4px">
            <option value="215.9,279.4">Letter (8.5x11")</option>
            <option value="210,297">A4</option>
            <option value="297,420">A3</option>
            <option value="custom">Custom Bed Size</option>
        </select>
        <label><input type="checkbox" id="paper-units" onchange="updatePaperSize()"> mm</label>
        <span style="flex:1"></span>
        <label>Scale:</label>
        <input type="range" id="svg-scale" min="10" max="300" value="100" oninput="updateSvgTransform()" style="width:80px">
        <span id="svg-scale-val">100%</span>
      </div>

      <div id="svg-preview-container" style="flex:1; position:relative; overflow:auto; display:flex; align-items:center; justify-content:center; padding:20px; background:#e0e0e0;">
        <svg id="svg-preview" style="box-shadow: 0 4px 12px rgba(0,0,0,0.2); background:#fff; transition:transform 0.1s;" viewBox="0 0 300 218"></svg>
      </div>
    </div><!-- /right-panel -->
  </div><!-- /main-flex -->
</div><!-- /main -->
"""
    html = re.sub(r'</div><!-- /bottom-section -->\s*</div><!-- /main -->', end_layout, html, flags=re.DOTALL)

# Add display logic for tabs
html = re.sub(r'\.bottom-view\.tab-content \{ padding:14px; overflow-y:auto; display:none; \}', '.bottom-view.tab-content { padding:14px; overflow-y:auto; display:none; flex-direction:column; }', html)
html = html.replace('.bottom-view.tab-content.active { display:block; }', '.bottom-view.tab-content.active { display:flex; }')
html = html.replace('.bottom-view { display:none;', '.bottom-view { display:none;')
html = html.replace('.bottom-view.active { display:flex; }', '.bottom-view.active { display:flex; }')

# Make sure view-home shows correctly
html = html.replace('document.querySelectorAll(\'.bottom-view\').forEach(v => v.classList.remove(\'active\'));\n  const panel = document.getElementById(\'view-\' + view);\n  if (panel) panel.classList.add(\'active\');', 
'''document.querySelectorAll('.bottom-view').forEach(v => { v.classList.remove('active'); v.style.display = 'none'; });
  const panel = document.getElementById('view-' + view);
  if (panel) { panel.classList.add('active'); panel.style.display = (view === 'home' ? 'flex' : 'flex'); }''')

with open("static/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("UI Refactor Script Executed.")
