#!/usr/bin/env node

import { chromium } from "playwright";

function parseArgs(argv) {
  const args = {
    unifiedUrl: process.env.UNIFIED_VIEW_URL || "http://127.0.0.1:8000/unified_sankey_viewer/",
    headless: true
  };
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--url" && argv[i + 1]) {
      args.unifiedUrl = argv[++i];
      continue;
    }
    if (token === "--headed") {
      args.headless = false;
      continue;
    }
  }
  return args;
}

function ensure(condition, message) {
  if (!condition) throw new Error(message);
}

async function dragRangeSelection(page) {
  const rangeRectsBefore = await page.locator(".range-selected").count();
  const bar = await page.locator("#rangeBar").boundingBox();
  ensure(bar, "Range bar not found");

  const y = bar.y + Math.min(bar.height - 8, 52);
  await page.mouse.move(bar.x + 40, y);
  await page.mouse.down();
  await page.mouse.move(bar.x + Math.min(bar.width - 40, 240), y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(120);

  const rangeRectsAfter = await page.locator(".range-selected").count();
  ensure(rangeRectsAfter > rangeRectsBefore, "Range drag did not create a new range rectangle");
}

async function dragViewportWindow(page) {
  const viewport = page.locator(".viewport-window").first();
  await viewport.waitFor({ state: "visible", timeout: 5000 });
  const xBefore = Number(await viewport.getAttribute("x"));
  const box = await viewport.boundingBox();
  ensure(box, "Viewport window has no bounding box");

  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + Math.min(box.width - 2, 10), y);
  await page.mouse.down();
  await page.mouse.move(box.x + Math.min(box.width + 160, 260), y, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(120);

  const xAfter = Number(await viewport.getAttribute("x"));
  ensure(Number.isFinite(xBefore) && Number.isFinite(xAfter), "Viewport x coordinate is invalid");
  ensure(Math.abs(xAfter - xBefore) > 0.5, "Viewport window drag did not move");
}

async function clickRangeSelect(page) {
  await page.locator(".range-selected").last().click();
  await page.waitForTimeout(80);
  const selectedCount = await page.locator(".range-selected.selected").count();
  ensure(selectedCount >= 1, "Range click selection did not apply selected state");
}

async function dragThresholdSlider(page, selector) {
  const slider = page.locator(selector).first();
  await slider.waitFor({ state: "visible", timeout: 5000 });
  const box = await slider.boundingBox();
  ensure(box, `Slider not interactable: ${selector}`);

  const y = box.y + box.height / 2;
  const x0 = box.x + box.width * 0.25;
  const x1 = box.x + box.width * 0.72;
  await page.mouse.move(x0, y);
  await page.mouse.down();
  await page.mouse.move(x1, y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(100);
}

async function runScenario(page, cfg) {
  await page.goto(cfg.url, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#rangeBar", { timeout: 15000 });
  await page.waitForSelector(".range-selected", { timeout: 15000 });

  await dragRangeSelection(page);
  await dragViewportWindow(page);
  await clickRangeSelect(page);
  await dragThresholdSlider(page, cfg.thresholdSelector);
}

async function main() {
  const args = parseArgs(process.argv);
  const browser = await chromium.launch({ headless: args.headless });
  const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });

  try {
    await runScenario(page, {
      url: args.unifiedUrl,
      thresholdSelector: ".panel-controls input[type='range']"
    });
    console.log("OK: range interactions passed for unified viewer");
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
