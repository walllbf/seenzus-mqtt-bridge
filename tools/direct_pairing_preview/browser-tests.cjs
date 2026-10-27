/* Run after export_browser_fixture.py. NODE_PATH may point at an existing
 * Playwright installation; no application dependencies are added to the bridge.
 * This tests real browser window/COOP/CSP behavior, with HA navigation and backend
 * authorization stubbed. The installed-HA end-to-end test is a separate gate.
 */
const { chromium } = require('@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const output = process.argv[3];
const root = __dirname;
const results = [];
const haOrigin = 'http://192.168.8.11:8123';
const callback = `${haOrigin}/api/seenzus_bridge/quick_pair/callback`;
const known = ['https://uat.seenzus.ai', 'https://seenzus.ai', 'https://app.seenzus.cn'];

async function run(browser, name, origin, options = {}) {
  const context = await browser.newContext();
  const visited = [];
  let currentState;
  if (options.realMyHa) await context.addInitScript(({ origin }) => {
    if (location.origin === 'https://my.home-assistant.io') localStorage.setItem('hassUrl', origin);
  }, { origin: haOrigin });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    visited.push(url.origin);
    if (process.env.LIVE_ENTRY === '1' && url.origin === origin && url.pathname.startsWith('/ha-connect-direct')) {
      await route.continue();
      return;
    }
    if (options.realMyHa && url.origin === 'https://my.home-assistant.io') {
      await route.continue();
      return;
    }
    if (url.origin === 'https://my.home-assistant.io') {
      await route.fulfill({ contentType: 'text/html', body: `<script>location.replace('${haOrigin}/config/integrations')</script>` });
    } else if (url.origin === haOrigin && (url.pathname === '/config/integrations' || url.pathname.startsWith('/_my_redirect/'))) {
      // Matches HA step-flow-external.ts: window.open(step.url), without features.
      await route.fulfill({ contentType: 'text/html', body: '<button id="authorize">Continue in app</button><script>document.querySelector("button").onclick=()=>window.open("/api/seenzus_bridge/quick_pair/origin")</script>' });
    } else if (url.origin === haOrigin && url.pathname.endsWith('/origin')) {
      const params = { ...fixture.params, redirect_uri: callback, configured_environments: options.duplicate ? 'uat' : '' };
      const state = { flow_id: 'browser-fixture', redirect_uri: callback, pairing_started_at: Date.now() - (options.clockSkew ? 86400000 : 0) };
      // The browser only treats JWT content as a routing hint. Python tests check
      // signature, active flow, replacement, expiry and escaping separately.
      currentState = `fixture.${Buffer.from(JSON.stringify(state)).toString('base64url')}.fixture`;
      params.state = currentState;
      const body = fixture.html.replace(/(<script id=context type=application\/json>).*?(<\/script>)/s,
        (_, start, end) => start + JSON.stringify({ params, chinese: true, origins: {
          cn: known[2], global: known[1], uat: known[0],
        } }).replaceAll('<', '\\u003c') + end);
      await route.fulfill({ status: 200, headers: fixture.headers, body });
    } else if (url.pathname.startsWith('/ha-connect-direct')) {
      const file = url.pathname.endsWith('launch.js') ? 'launch.js'
        : url.pathname.endsWith('style.css') ? 'style.css' : 'index.html';
      await route.fulfill({ headers: {
        'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
        'Cross-Origin-Opener-Policy': options.isolated ? 'same-origin' : 'same-origin-allow-popups',
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
      }, body: fs.readFileSync(path.join(root, file)) });
    } else await route.fulfill({ contentType: 'text/html', body: '<main>Authorization fixture</main>' });
  });
  try {
    const page = await context.newPage();
    await page.goto(origin + '/ha-connect-direct');
    if (process.env.PREVIEW_SCREENSHOT) await page.screenshot({ path: process.env.PREVIEW_SCREENSHOT });
    const haEvent = context.waitForEvent('page');
    await page.locator('#connect').click();
    const ha = await haEvent;
    if (options.realMyHa) await ha.locator('a.open-link').click({ timeout: 30000 });
    await ha.waitForURL(options.realMyHa ? '**/_my_redirect/config_flow_start?**' : haOrigin + '/config/integrations');
    let replacement;
    if (options.stale) {
      // A retained old HA tab must not claim a new launch in the same app page.
      await page.reload();
      const replacementEvent = context.waitForEvent('page');
      await page.locator('#connect').click();
      replacement = await replacementEvent;
      await replacement.waitForURL(haOrigin + '/config/integrations');
    }
    if (options.closed) await page.close();
    const externalEvent = context.waitForEvent('page');
    await ha.locator('#authorize').click();
    const external = await externalEvent;
    await external.waitForLoadState('domcontentloaded');
    if (options.closed || options.isolated || options.stale) {
      await external.waitForFunction(() => document.getElementById('status')?.textContent.includes('无法取得'), { timeout: 15000 });
      assert(new URL(external.url()).pathname.endsWith('/origin'));
      assert(!visited.some(value => known.includes(value) && value !== origin));
      if (replacement) {
        const currentEvent = context.waitForEvent('page');
        await replacement.locator('#authorize').click();
        const current = await currentEvent;
        await current.waitForURL(origin + '/pair/bootstrap?**');
      }
    } else if (options.duplicate) {
      await external.waitForURL('**/callback?**');
      const target = new URL(external.url());
      assert.equal(target.searchParams.get('error'), 'environment_already_configured');
      assert.equal(target.searchParams.get('state'), currentState);
    } else {
      await external.waitForURL(origin + '/pair/bootstrap?**');
      const target = new URL(external.url());
      assert.equal(target.searchParams.get('redirect_uri'), callback);
      assert.equal(target.searchParams.get('state'), currentState);
      assert.equal(await external.evaluate(() => window.opener), null);
      assert(!visited.some(value => known.includes(value) && value !== origin), 'cross-deployment network dependency');
    }
    results.push({ name, passed: true });
    console.log('PASS ' + name);
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    if (process.env.REAL_MY_HA === '1') {
      await run(browser, 'real My Home Assistant + browser origin handoff', known[0], { realMyHa: true });
      fs.writeFileSync(output, JSON.stringify({ browser: browser.version(), passed: results.length, results }, null, 2));
      return;
    }
    for (const origin of known) await run(browser, 'automatic ' + origin, origin);
    await run(browser, 'closed initiating app fails without fallback', known[0], { closed: true });
    await run(browser, 'COOP isolation fails without fallback', known[0], { isolated: true });
    await run(browser, 'old flow cannot claim a new launch', known[0], { stale: true });
    await run(browser, 'HA and PC clock skew does not change the environment', known[0], { clockSkew: true });
    await run(browser, 'same environment returns signed duplicate error', known[0], { duplicate: true });
    fs.writeFileSync(output, JSON.stringify({ browser: browser.version(), passed: results.length, results }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
