// Isolated browser regression: exercise the shipped module through real HTTP,
// nested HA/Lit shadow roots and a same-origin HACS iframe. No running HA needed.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import http from "node:http";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = new URL("../custom_components/seenzus_bridge/", import.meta.url);
const script = await readFile(new URL("frontend/brand-compat.js", root));
const icon = await readFile(new URL("brand/icon.png", root));
const requests = [];
let failLocal = false;
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  requests.push(url.pathname);
  if (url.pathname === "/seenzus_bridge/frontend/brand-compat.js") {
    response.writeHead(200, { "Content-Type": "text/javascript" }).end(
      process.argv.includes("--baseline") ? "// no compatibility module" : script,
    );
  } else if (url.pathname.startsWith("/seenzus_bridge/brand/")) {
    if (failLocal) return response.writeHead(404).end();
    const name = url.pathname.split("/").pop();
    if (!["icon.png", "icon@2x.png", "logo.png", "logo@2x.png"].includes(name)) {
      return response.writeHead(404).end();
    }
    response.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-store" }).end(
      await readFile(new URL(`brand/${name}`, root)),
    );
  } else {
    response.writeHead(200, { "Content-Type": "text/html" }).end("<!doctype html><body></body>");
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
let checks = 0;
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  await page.route("https://brands.home-assistant.io/**", route => route.fulfill({
    status: 200, contentType: "image/png", body: icon,
  }));
  await page.goto(`${origin}/hacs`);
  await page.evaluate(() => {
    const ha = document.createElement("home-assistant");
    const main = document.createElement("home-assistant-main");
    const panel = document.createElement("ha-panel-custom");
    document.body.append(ha);
    ha.attachShadow({ mode: "open" }).append(main);
    main.attachShadow({ mode: "open" }).append(panel);
    const iframe = document.createElement("iframe");
    iframe.src = "/fixture-frame";
    panel.append(iframe);
    window.hacsFrame = iframe;
    const unrelated = document.createElement("img");
    unrelated.id = "unrelated";
    unrelated.src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png";
    document.body.append(unrelated);
  });
  await page.waitForFunction(() => window.hacsFrame.contentDocument?.readyState === "complete");
  await page.addScriptTag({ type: "module", url: `${origin}/seenzus_bridge/frontend/brand-compat.js?v=test` });
  // HACS is created after the compatibility script has already started.
  await page.evaluate(() => {
    const doc = window.hacsFrame.contentDocument;
    const hacs = doc.createElement("hacs-frontend");
    doc.body.append(hacs);
    const dashboard = doc.createElement("hacs-dashboard");
    hacs.attachShadow({ mode: "open" }).append(dashboard);
    window.rows = dashboard.attachShadow({ mode: "open" });
    const row = doc.createElement("img");
    row.id = "seenzus";
    row.src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png";
    window.rows.append(row);
  });
  const src = () => page.evaluate(() => window.rows.querySelector("#seenzus").getAttribute("src"));
  const waitLocal = () => page.waitForFunction(() =>
    window.rows.querySelector("#seenzus").src.includes("/seenzus_bridge/brand/") &&
    window.rows.querySelector("#seenzus").naturalWidth > 0,
    undefined, { timeout: process.argv.includes("--baseline") ? 2500 : 10000 });
  await waitLocal();
  assert.equal(await src(), `${origin}/seenzus_bridge/brand/icon.png?v=test`);
  checks++;
  assert.equal(await page.locator("#unrelated").getAttribute("src"), "https://brands.home-assistant.io/_/seenzus_bridge/icon.png");
  checks++;
  for (const prefix of ["", "_/", "marketplace/"]) {
    for (const name of ["icon.png", "dark_icon.png", "icon@2x.png", "dark_icon@2x.png", "logo.png", "dark_logo@2x.png"]) {
      await page.evaluate(source => { window.rows.querySelector("#seenzus").src = source; },
        `https://brands.home-assistant.io/${prefix}seenzus_bridge/${name}`);
      await waitLocal();
      assert.equal(await src(), `${origin}/seenzus_bridge/brand/${name.replace("dark_", "")}?v=test`);
      checks++;
    }
  }
  // Other domains, company-brand requests, lookalike hosts and local HA token
  // URLs must not be rewritten, even when rendered inside the HACS scope.
  for (const source of [
    "https://brands.home-assistant.io/_/hacs/icon.png",
    "https://brands.home-assistant.io/brands/seenzus_bridge/icon.png",
    "https://brands.home-assistant.io.evil.invalid/_/seenzus_bridge/icon.png",
    `${origin}/api/brands/integration/seenzus_bridge/icon.png?token=fixture-only`,
  ]) {
    await page.evaluate(source => {
      window.rows.querySelector("#seenzus").src = source;
      dispatchEvent(new Event("location-changed"));
    }, source);
    assert.equal(await src(), source);
    checks++;
  }
  // Route changes stop all rewrites outside the HACS page.
  await page.evaluate(() => {
    history.pushState({}, "", "/lovelace/default_view");
    window.rows.querySelector("#seenzus").src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png";
    dispatchEvent(new Event("location-changed"));
  });
  await page.waitForTimeout(1100);
  assert.equal(await src(), "https://brands.home-assistant.io/_/seenzus_bridge/icon.png");
  checks++;
  await page.evaluate(() => { history.pushState({}, "", "/hacs"); dispatchEvent(new Event("popstate")); });
  await waitLocal();
  checks++;
  // On a missing local asset use the previous HACS image once, without a loop.
  failLocal = true;
  await page.addScriptTag({ type: "module", url: `${origin}/seenzus_bridge/frontend/brand-compat.js?v=missing` });
  const beforeFailures = requests.filter(p => p.startsWith("/seenzus_bridge/brand/")).length;
  await page.evaluate(() => {
    window.rows.querySelector("#seenzus").src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png?new-request=1";
    dispatchEvent(new Event("location-changed"));
  });
  await page.waitForFunction(() => window.rows.querySelector("#seenzus").src.includes("new-request=1"));
  await page.waitForTimeout(1500);
  assert.equal(await src(), "https://brands.home-assistant.io/_/seenzus_bridge/icon.png?new-request=1");
  assert.equal(requests.filter(p => p.startsWith("/seenzus_bridge/brand/")).length - beforeFailures, 1);
  checks++;
  failLocal = false;
  // Page lifecycle pauses the timer, then resumes, including bfcache return.
  await page.evaluate(() => {
    dispatchEvent(new Event("pagehide"));
    window.rows.querySelector("#seenzus").src = "https://brands.home-assistant.io/_/seenzus_bridge/dark_icon.png";
  });
  await page.waitForTimeout(1100);
  assert.equal(await src(), "https://brands.home-assistant.io/_/seenzus_bridge/dark_icon.png");
  await page.evaluate(() => dispatchEvent(new Event("pageshow")));
  await waitLocal();
  checks++;
  // A replacement resource stops the previous instance, avoiding duplicate scans.
  await page.addScriptTag({ type: "module", url: `${origin}/seenzus_bridge/frontend/brand-compat.js?v=second` });
  await page.evaluate(() => {
    window.rows.querySelector("#seenzus").src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png";
    dispatchEvent(new Event("location-changed"));
  });
  await waitLocal();
  assert.equal(await src(), `${origin}/seenzus_bridge/brand/icon.png?v=second`);
  checks++;
  await page.evaluate(() => {
    globalThis[Symbol.for("seenzus.hacsIconCompatibility")].stop();
    window.rows.querySelector("#seenzus").src = "https://brands.home-assistant.io/_/seenzus_bridge/icon.png";
    dispatchEvent(new Event("location-changed"));
  });
  await page.waitForTimeout(1100);
  assert.equal(await src(), "https://brands.home-assistant.io/_/seenzus_bridge/icon.png");
  checks++;
  assert.deepEqual(errors, []);
  assert(requests.every(p => p === "/hacs" || p === "/fixture-frame" || p === "/lovelace/default_view" ||
    p.startsWith("/seenzus_bridge/") || p === "/api/brands/integration/seenzus_bridge/icon.png" || p === "/favicon.ico"));
  console.log(`PASS: ${checks} automatic compatibility browser checks; no HACS source edits or API calls.`);
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
