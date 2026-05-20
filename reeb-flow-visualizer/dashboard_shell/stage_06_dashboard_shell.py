#!/usr/bin/env python3

"""Build a root dashboard shell that loads the unified Sankey app."""

from __future__ import annotations

from pathlib import Path

from common import OUTPUT_DIR

DASHBOARD_DIR = OUTPUT_DIR
SHELL_HTML = DASHBOARD_DIR / "index.html"
SHELL_CSS = DASHBOARD_DIR / "dashboard.css"
SHELL_JS = DASHBOARD_DIR / "dashboard.js"

VIEWER_CHOICES = [
    {
        "id": "unified",
        "label": "Unified",
        "path": "unified_sankey_viewer/index.html",
        "description": "Unified domain/range Sankey dashboard.",
    },
]


def write_index_html() -> Path:
    options = "\n".join(
        f'<option value="{item["path"]}" {"selected" if idx == 0 else ""}>{item["label"]}</option>'
        for idx, item in enumerate(VIEWER_CHOICES)
    )

    path = SHELL_HTML
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reeb Sankey Dashboard</title>
  <link rel="stylesheet" href="dashboard.css">
</head>
<body>
  <header class="shell-header">
    <div class="shell-title">
      <h1>Reeb Sankey Dashboard</h1>
      <p>Unified view for domain overlap and range-based matching metrics.</p>
    </div>
    <div class="shell-actions">
      <label>
        View
        <select id="viewerSelect">
          {options}
        </select>
      </label>
    </div>
  </header>

  <main class="shell-main">
    <iframe id="viewerFrame" title="Reeb Sankey Viewer" src="{VIEWER_CHOICES[0]['path']}"></iframe>
  </main>

  <script src="dashboard.js"></script>
</body>
</html>
"""
    )
    return path


def write_style_css() -> Path:
    path = SHELL_CSS
    path.write_text(
        """* { box-sizing: border-box; }
html, body {
  margin: 0;
  width: 100%;
  height: 100%;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #1d252d;
  background: #f6f7f9;
}
body {
  display: grid;
  grid-template-rows: 72px minmax(0, 1fr);
}
.shell-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  background: #fff;
  border-bottom: 1px solid #d9dee5;
}
.shell-title h1 {
  margin: 0;
  font-size: 20px;
}
.shell-title p {
  margin: 4px 0 0;
  font-size: 13px;
  color: #5a6572;
}
.shell-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.shell-actions label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
select, button {
  font: inherit;
}
select, button {
  border: 1px solid #b8c1cc;
  background: #fff;
  border-radius: 5px;
  padding: 7px 10px;
}
button {
  cursor: pointer;
}
.shell-main {
  min-height: 0;
}
iframe {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
  background: #fff;
}
"""
    )
    return path


def write_viewer_js() -> Path:
    path = SHELL_JS
    path.write_text(
        """const select = document.getElementById("viewerSelect");
const frame = document.getElementById("viewerFrame");

function setViewer(path) {
  frame.src = path;
  const base = path.endsWith("/index.html") ? path.slice(0, -"index.html".length) : path;
  history.replaceState({}, "", `?view=${encodeURIComponent(base)}`);
}

function loadFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  if (!view) return;
  const match = Array.from(select.options).find(option => option.value.startsWith(view));
  if (match) {
    select.value = match.value;
    frame.src = match.value;
  }
}

select.addEventListener("change", () => setViewer(select.value));

loadFromQuery();
"""
    )
    return path


def build_dashboard_shell_stage() -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    index_path = write_index_html()
    css_path = write_style_css()
    js_path = write_viewer_js()

    print(f"Wrote dashboard shell: {DASHBOARD_DIR}")
    for artifact in (index_path, css_path, js_path):
        print(f"  {artifact.name}")
    print("\nOpen with:")
    print(f"  cd {DASHBOARD_DIR}")
    print("  python3 -m http.server 8000")
    print("  http://localhost:8000")


def main() -> int:
    build_dashboard_shell_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
