#!/usr/bin/env node

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

async function loadChromium() {
  try {
    const mod = await import("playwright");
    return mod.chromium;
  } catch (error) {
    const message = [
      "Playwright is not installed in this Node environment.",
      "Install once from this repository with:",
      "  npm install --no-save playwright",
      "Then rerun this exporter with:",
      "  node paper_exports/export_viewer_figure.mjs ...",
      "Original error:",
      `  ${error.code || error.message || String(error)}`
    ].join("\n");
    throw new Error(message);
  }
}

function parseArgs(argv) {
  const args = {
    viewer: "",
    url: "",
    preset: "",
    out: "",
    target: "",
    selector: "",
    width: 2400,
    height: 1400,
    scale: 2,
    waitMs: 350,
    headed: false
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    const next = () => argv[++i];
    if (token === "--viewer") args.viewer = next() || "";
    else if (token === "--url") args.url = next() || "";
    else if (token === "--preset") args.preset = next() || "";
    else if (token === "--out") args.out = next() || "";
    else if (token === "--target") args.target = next() || "";
    else if (token === "--selector") args.selector = next() || "";
    else if (token === "--width") args.width = Number(next()) || args.width;
    else if (token === "--height") args.height = Number(next()) || args.height;
    else if (token === "--scale") args.scale = Number(next()) || args.scale;
    else if (token === "--wait-ms") args.waitMs = Number(next()) || args.waitMs;
    else if (token === "--headed") args.headed = true;
    else if (token === "--help" || token === "-h") args.help = true;
    else throw new Error(`Unknown argument: ${token}`);
  }
  return args;
}

function usage() {
  const self = path.basename(fileURLToPath(import.meta.url));
  return `Usage:
  npm exec --yes --package=playwright -- node paper_exports/${self} \\
    --viewer /path/to/unified_sankey_viewer \\
    --preset /path/to/preset.figure_preset.json \\
    --target active-canvas \\
    --scale 3

Targets:
  full, main, viewer, panels, active-panel, active-canvas, analysis, details, controls, rangebar

Options:
  --url URL          Use an already-running viewer URL instead of --viewer.
  --selector CSS    Capture a custom CSS selector; overrides --target.
  --width N         Browser viewport width, default 2400.
  --height N        Browser viewport height, default 1400.
  --scale N         deviceScaleFactor, default 2.
  --wait-ms N       Extra wait after restoring preset, default 350.
  --headed          Show browser while exporting.

If --viewer is provided and --out is omitted, PNGs are written to:
  /path/to/dataset/sankey/paper_exports/images

Install dependency once with: npm install --no-save playwright
`;
}

function safeName(value) {
  return String(value || "figure")
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "") || "figure";
}

function readPreset(filePath) {
  if (!filePath) return null;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function defaultImageOutputDir(viewerPath) {
  if (!viewerPath) return "";
  return path.resolve(viewerPath, "..", "paper_exports", "images");
}

function ensureOutputPath(outPath, preset, target, viewerPath = "") {
  const requested = outPath || defaultImageOutputDir(viewerPath);
  if (!requested) throw new Error("Missing --out path. Provide --out when using --url without --viewer.");
  const resolved = path.resolve(requested);
  const ext = path.extname(resolved).toLowerCase();
  if (ext === ".png") {
    fs.mkdirSync(path.dirname(resolved), { recursive: true });
    return resolved;
  }
  fs.mkdirSync(resolved, { recursive: true });
  const presetName = safeName(preset?.name || preset?.dataset || "figure");
  const targetName = safeName(target || preset?.recommended_target || "capture");
  return path.join(resolved, `${presetName}_${targetName}.png`);
}

function mimeFor(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".html") return "text/html; charset=utf-8";
  if (ext === ".js") return "text/javascript; charset=utf-8";
  if (ext === ".css") return "text/css; charset=utf-8";
  if (ext === ".json") return "application/json; charset=utf-8";
  if (ext === ".png") return "image/png";
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".svg") return "image/svg+xml";
  return "application/octet-stream";
}

function startStaticServer(rootDir) {
  const root = path.resolve(rootDir);
  const server = http.createServer((req, res) => {
    try {
      const url = new URL(req.url || "/", "http://127.0.0.1");
      const requested = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);
      const candidate = path.resolve(path.join(root, requested));
      const relative = path.relative(root, candidate);
      if (relative.startsWith("..") || path.isAbsolute(relative)) {
        res.writeHead(403);
        res.end("Forbidden");
        return;
      }
      fs.stat(candidate, (statErr, stat) => {
        if (statErr || !stat.isFile()) {
          res.writeHead(404);
          res.end("Not found");
          return;
        }
        res.writeHead(200, { "Content-Type": mimeFor(candidate) });
        fs.createReadStream(candidate).pipe(res);
      });
    } catch (error) {
      res.writeHead(500);
      res.end(String(error));
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({
        url: `http://127.0.0.1:${address.port}/`,
        close: () => new Promise(done => server.close(done))
      });
    });
  });
}

const targetSelectors = {
  full: "body",
  main: "main",
  viewer: "#viewer",
  panels: "#panelList",
  "active-panel": ".panel.active-panel",
  "active-canvas": ".panel.active-panel .panel-canvas",
  analysis: ".panel.active-panel .analysis-box",
  details: "#details",
  controls: "#controls",
  rangebar: "#rangeBarWrap"
};

async function captureTarget(page, selector, outputPath, fullPage) {
  if (fullPage) {
    await page.screenshot({ path: outputPath, fullPage: true });
    return;
  }
  const locator = page.locator(selector).first();
  await locator.waitFor({ state: "visible", timeout: 15000 });
  await locator.screenshot({ path: outputPath });
}

async function main() {
  const args = parseArgs(process.argv);
  if (args.help) {
    console.log(usage());
    return;
  }
  if (!args.viewer && !args.url) throw new Error("Provide either --viewer or --url");

  const preset = readPreset(args.preset);
  const target = args.target || preset?.recommended_target || "active-panel";
  const selector = args.selector || targetSelectors[target];
  if (!selector && target !== "full") throw new Error(`Unknown target: ${target}`);
  const outputPath = ensureOutputPath(args.out, preset, target, args.viewer);

  const chromium = await loadChromium();
  let server = null;
  const url = args.url || (server = await startStaticServer(args.viewer)).url;
  const browser = await chromium.launch({ headless: !args.headed });
  const page = await browser.newPage({
    viewport: { width: args.width, height: args.height },
    deviceScaleFactor: Math.max(1, args.scale)
  });

  try {
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => Boolean(window.ReebFigureExport?.ready?.()), null, { timeout: 20000 });
    await page.waitForSelector("#panelList .panel", { timeout: 20000 });
    if (preset) {
      await page.evaluate(async value => window.ReebFigureExport.applyPreset(value), preset);
    }
    await page.waitForTimeout(Math.max(0, args.waitMs));
    await captureTarget(page, selector || "body", outputPath, target === "full");
    console.log(`Wrote ${outputPath}`);
  } finally {
    await browser.close();
    if (server) await server.close();
  }
}

main().catch(error => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});
