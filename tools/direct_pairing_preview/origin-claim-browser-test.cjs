/* Cross-repository browser check of the opener-free origin handoff.
 * Arguments: HA view fixture (export_browser_fixture.py), application public directory.
 *
 * Models the browsers that keep no opener: the Android/iOS in-app WebView
 * (one window, same-tab navigation) and a phone system browser (HA opens the
 * external step in a new tab). The HA origin page is the bridge's actual HTML
 * and script; the claim pages are the application's actual static files. HA,
 * My Home Assistant and /pair/bootstrap are fixtures, so this proves routing
 * only, not a real authorization.
 */
// BROWSER=webkit runs the WebKit engine used by iOS WKWebView.
const engine = require('@playwright/test')[process.env.BROWSER || 'chromium'];
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const publicDir = process.argv[3];
// The HA address a scenario runs under; the pairing callback lives on it.
let ha = 'http://192.168.8.11:8123';
const callbackUrl = () => `${ha}/api/seenzus_bridge/quick_pair/callback`;
const origins = { cn: 'https://app.seenzus.cn', global: 'https://seenzus.ai', uat: 'https://uat.seenzus.ai' };
const custom = 'http://dev.localhost:3000';
const LAUNCH_KEY = 'seenzus:ha-origin-launch:v1';

const labels = { cn: '国内 PRD', global: '境外 PRD', uat: 'UAT（开发）', custom: 'Dev Server（开发预设）' };

function originPage({ configured = '', withCustom = false, choices = 'cn,global' }) {
  const params = { ...fixture.params, redirect_uri: callbackUrl(), configured_environments: configured,
    manual_environments: choices };
  params.state = `fixture.${Buffer.from(JSON.stringify({ flow_id: 'fixture', redirect_uri: callbackUrl() })).toString('base64url')}.fixture`;
  const all = withCustom ? { ...origins, custom } : origins;
  const body = fixture.html.replace(/(<script id=context type=application\/json>).*?(<\/script>)/s,
    (_, before, after) => before + JSON.stringify({ params, chinese: true, origins: all, labels }).replaceAll('<', '\\u003c') + after);
  const headers = { ...fixture.headers };
  // The view derives connect-src from the same origins it embeds.
  headers['Content-Security-Policy'] = headers['Content-Security-Policy']
    .replace(/connect-src [^;]*;/, `connect-src ${Object.values(all).sort().join(' ')};`);
  return { status: 200, headers, body, state: params.state };
}

/** One browser profile: every deployment, My HA and HA itself are routed fixtures. */
async function exercise(browser, name, { launches, unreachable = [], configured = '', withCustom = false,
  choices, systemBrowser = false, expect }) {
  const context = await browser.newContext();
  const visited = [];
  const page = originPage({ configured, withCustom, choices });
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    visited.push(url.origin + url.pathname);
    const deployment = [...Object.values(origins), custom].includes(url.origin);
    if (deployment && unreachable.includes(url.origin)) return route.abort('connectionrefused');
    if (deployment && url.pathname === '/launch') {
      // What /native-signin does in the in-app WebView: record, then go to HA in the same tab.
      return route.fulfill({ contentType: 'text/html', body: `<script>localStorage.setItem('${LAUNCH_KEY}',
        JSON.stringify({createdAt: Date.now(), expiresAt: Date.now() + 900000}));
        location.replace('https://my.home-assistant.io/redirect/config_flow_start/?domain=seenzus_bridge')</script>` });
    }
    if (deployment && url.pathname.startsWith('/ha-origin-claim/')) {
      const file = path.basename(url.pathname.endsWith('/') ? 'index.html' : url.pathname);
      const type = { 'claim.js': 'text/javascript', 'style.css': 'text/css' }[file] || 'text/html';
      // Production keeps everything same-origin except the claim script, which HA's page
      // fetches cross-origin as its reachability probe. Routed responses bypass CORP, so the
      // app's own header test covers that contract.
      return route.fulfill({ headers: { 'Content-Type': type, 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
        'Cross-Origin-Resource-Policy': file === 'claim.js' ? 'cross-origin' : 'same-origin',
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" },
        body: fs.readFileSync(path.join(publicDir, 'ha-origin-claim', file)) });
    }
    if (deployment && url.pathname === '/pair/bootstrap') return route.fulfill({ contentType: 'text/html', body: 'Authorization fixture' });
    if (url.origin === 'https://my.home-assistant.io') return route.fulfill({ contentType: 'text/html', body: `<script>location.replace('${ha}/config/integrations')</script>` });
    if (url.origin === ha && url.pathname === '/config/integrations') {
      // A phone browser opens HA's external step in a new tab; an in-app WebView without
      // multi-window support loads it in place.
      const open = systemBrowser ? "window.open('/api/seenzus_bridge/quick_pair/origin?state=' + encodeURIComponent(STATE))"
        : "location.assign('/api/seenzus_bridge/quick_pair/origin?state=' + encodeURIComponent(STATE))";
      return route.fulfill({ contentType: 'text/html', body: `<script>const STATE=${JSON.stringify(page.state)}</script><button onclick="${open}">Authorize</button>` });
    }
    if (url.origin === ha && url.pathname === '/api/seenzus_bridge/quick_pair/origin') return route.fulfill(page);
    if (url.origin === ha && url.pathname === '/api/seenzus_bridge/quick_pair/callback') return route.fulfill({ contentType: 'text/html', body: 'HA callback fixture' });
    return route.fulfill({ status: 404, body: '' });
  });
  try {
    const tab = await context.newPage();
    for (const [index, origin] of launches.entries()) {
      // Older launches first; the last one is the app the user just started from.
      await tab.goto(`${origin}/launch`);
      await tab.waitForURL(`${ha}/config/integrations`);
      if (index < launches.length - 1) await tab.waitForTimeout(20);
    }
    if (!launches.length) await tab.goto(`${ha}/config/integrations`);
    const next = systemBrowser ? context.waitForEvent('page') : Promise.resolve(tab);
    await tab.locator('button').click();
    const external = await next;
    if (expect.offers) {
      await external.locator('#environments').waitFor({ state: 'visible' });
      assert.deepEqual(await external.locator('#environments button').allTextContents(), expect.offers.map((key) => labels[key]));
      assert(await external.locator('#manual').isHidden());
    } else if (expect.choose) {
      await external.getByRole('button', { name: labels[expect.choose] }).click();
      await external.waitForURL(`${origins[expect.choose]}/pair/bootstrap?**`);
      assert.equal(new URL(external.url()).searchParams.get('state'), page.state);
    } else if (expect.manual) {
      await external.locator('#manual').waitFor({ state: 'visible' });
      await external.locator('#manual').click();
      await external.waitForURL(`${callbackUrl()}?**`);
      assert.equal(new URL(external.url()).searchParams.get('error'), 'environment_required');
    } else if (expect.error) {
      await external.waitForURL(`${callbackUrl()}?**`);
      assert.equal(new URL(external.url()).searchParams.get('error'), expect.error);
    } else {
      await external.waitForURL(`${expect.origin}/pair/bootstrap?**`);
      const params = new URL(external.url()).searchParams;
      assert.equal(params.get('redirect_uri'), callbackUrl());
      assert.equal(params.get('state'), page.state);
    }
    for (const origin of unreachable) assert(!visited.some((value) => value.startsWith(`${origin}/ha-origin-claim/?`)), `${origin} must be skipped`);
    console.log(`PASS ${name}`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await engine.launch({ headless: true });
  try {
    for (const [key, origin] of Object.entries(origins)) {
      await exercise(browser, `in-app WebView started from ${key}`, { launches: [origin], expect: { origin } });
    }
    await exercise(browser, 'system browser tab started from UAT', { launches: [origins.uat], systemBrowser: true, expect: { origin: origins.uat } });
    await exercise(browser, 'newest launch wins over an earlier one', { launches: [origins.uat, origins.global], expect: { origin: origins.global } });
    await exercise(browser, 'unreachable deployment is skipped', { launches: [origins.global], unreachable: [origins.cn], expect: { origin: origins.global } });
    await exercise(browser, 'developer origin claims first', { launches: [custom], withCustom: true, expect: { origin: custom } });
    await exercise(browser, 'no launch offers the environment choice on this page', { launches: [], expect: { choose: 'global' } });
    await exercise(browser, 'no reachable deployment offers the choice too',
      { launches: [], unreachable: Object.values(origins), expect: { offers: ['cn', 'global'] } });
    await exercise(browser, 'without page choices it falls back to HA', { launches: [], choices: '', expect: { manual: true } });
    await exercise(browser, 'configured environment is reported, not duplicated', { launches: [origins.cn], configured: 'cn', expect: { error: 'environment_already_configured' } });
    // Deployments only return to HTTPS or local HA addresses; others get the choice, not a dead end.
    ha = 'http://homeassistant:8123';
    await exercise(browser, 'non-local plain-HTTP HA asks for a choice without leaving', { launches: [origins.uat], unreachable: [], expect: { manual: true } });
    ha = 'http://192.168.8.11:8123';
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
