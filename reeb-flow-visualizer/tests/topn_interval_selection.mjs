#!/usr/bin/env node

import { chromium } from "playwright";

function ensure(condition, message) {
  if (!condition) throw new Error(message);
}

async function verifyTopSelection(page, count) {
  const countInput = page.locator(".analysis-content .analysis-actions input[type='number']").first();
  await countInput.fill(String(count));
  await countInput.dispatchEvent("change");
  await page.waitForTimeout(200);

  const activeDots = page.locator(".analysis-graph-dot.active");
  ensure(await activeDots.count() === count, `Changing top N did not select exactly ${count} intervals`);
  for (const dot of await activeDots.all()) {
    ensure(await dot.evaluate(node => getComputedStyle(node).opacity) === "1", "Selected interval is not opaque");
    ensure(
      await dot.evaluate(node => getComputedStyle(node).fill === "rgb(239, 68, 68)"),
      "Selected interval does not use the configured red color"
    );
  }
  const activeEdges = page.locator(".analysis-graph-line.active");
  for (const edge of await activeEdges.all()) {
    ensure(
      await edge.evaluate(node => getComputedStyle(node).stroke === "rgb(107, 114, 128)"),
      "Edge between selected intervals does not retain the plot color"
    );
    ensure(await edge.evaluate(node => getComputedStyle(node).opacity) === "1", "Selected interval edge is not opaque");
  }

  ensure(
    await page.locator(".node.analysis-highlight, .link.analysis-highlight").count() > 0,
    "Selected intervals did not highlight the Sankey"
  );
  const focusOffset = await page.evaluate(() => {
    const selected = [...document.querySelectorAll(".analysis-graph-dot.active")]
      .map(node => node.__data__)
      .sort((a, b) => (
        Number(b?.event_score) - Number(a?.event_score)
        || Number(a?.source_timestep_index) - Number(b?.source_timestep_index)
      ));
    const first = selected[0];
    if (!first) return null;
    const start = Number(first.source_timestep_index);
    const end = Number(first.target_timestep_index);
    const rects = [...document.querySelectorAll(".node > rect")]
      .filter(node => {
        const timestep = Number(node.__data__?.timestep_index);
        return timestep >= start && timestep <= end;
      })
      .map(node => node.getBoundingClientRect());
    const canvas = document.querySelector(".panel-canvas")?.getBoundingClientRect();
    if (!rects.length || !canvas) return null;
    const intervalCenter = (
      Math.min(...rects.map(rect => rect.left))
      + Math.max(...rects.map(rect => rect.right))
    ) / 2;
    return intervalCenter - (canvas.left + canvas.width / 2);
  });
  ensure(Number.isFinite(focusOffset), "Could not measure the first ranked interval focus");
  ensure(Math.abs(focusOffset) < 3, `First ranked interval is ${focusOffset}px away from the Sankey center`);
}

async function main() {
  const url = process.env.UNIFIED_VIEW_URL || "http://127.0.0.1:8000/";
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1680, height: 980 } });

  try {
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".analysis-graph-dot", { timeout: 15000 });

    ensure(
      await page.locator(".analysis-toolbar input[type='color']").first().inputValue() === "#6b7280",
      "Analysis plot color does not default to grey"
    );
    ensure(
      await page.locator(".analysis-content .analysis-actions input[type='number']").first().inputValue() === "0",
      "Top interesting intervals does not default to 0"
    );
    ensure(
      await page.locator(".analysis-toolbar input[type='range']").first().inputValue() === "0",
      "Analysis plot transparency does not default to 0%"
    );
    ensure(
      await page.getByRole("button", { name: "Best supported range intervals", exact: true }).count() === 0,
      "Best supported range intervals tab is still visible"
    );
    ensure(
      await page.getByRole("button", { name: "Best supported domain intervals", exact: true }).count() === 0,
      "Best supported domain intervals tab is still visible"
    );
    ensure(
      await page.getByRole("button", { name: "Domain/range complementarity", exact: true }).count() === 0,
      "Domain/range complementarity tab is still visible"
    );
    ensure(
      await page.locator("select option", { hasText: "Sheet geometry IoU" }).count() === 0,
      "Sheet geometry IoU metric option is still visible"
    );

    await verifyTopSelection(page, 3);

    console.log("OK: top-N interval selection passed");
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
