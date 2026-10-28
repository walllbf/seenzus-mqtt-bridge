/* Cross-repository browser check. Arguments: HA view fixture, application public directory. */
const { chromium } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const app = path.join(process.argv[3], 'ha-origin-start');
const ha = 'http://192.168.8.11:8123';
const callback = `${ha}/api/seenzus_bridge/quick_pair/callback`;
const origins = { cn: 'https://app.seenzus.cn', global: 'https://seenzus.ai', uat: 'https://uat.seenzus.ai' };
const customOrigin = 'http://dev.localhost:3000';

async function exercise(browser, origin, blocked) {
  const context = await browser.newContext();
  const pages = [];
  const seen = [];
  context.on('page', page => pages.push(page));
  if (blocked) await context.addInitScript(() => {
    if (location.pathname === '/ha-origin-start/') {
      const open = window.open.bind(window);
      let first = true;
      window.open = (...args) => { if (first) { first = false; return null; } return open(...args); };
    }
  });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    seen.push(url.origin);
    if (url.origin === origin && url.pathname === '/start') return route.fulfill({ headers: {
      'Content-Type': 'text/html', 'Cross-Origin-Opener-Policy': 'same-origin',
    }, body: `<button onclick="window.open('/ha-origin-start/?auto=1','_blank')">Connect</button>` });
    if (url.origin === origin && url.pathname.startsWith('/ha-origin-start/')) {
      const file = url.pathname.endsWith('.js') ? 'launch.js' : url.pathname.endsWith('.css') ? 'style.css' : 'index.html';
      return route.fulfill({ headers: { 'Content-Type': file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
        'Cross-Origin-Opener-Policy': file === 'index.html' ? 'same-origin-allow-popups' : 'same-origin',
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
      }, body: fs.readFileSync(path.join(app, file)) });
    }
    if (url.origin === 'https://my.home-assistant.io') return route.fulfill({contentType:'text/html',body:`<script>location.replace('${ha}/config/integrations')</script>`});
    if (url.origin === ha && url.pathname === '/config/integrations') return route.fulfill({contentType:'text/html',body:'<button onclick="window.open(\'/api/seenzus_bridge/quick_pair/origin\')">Authorize</button>'});
    if (url.origin === ha && url.pathname.endsWith('/origin')) {
      const params = { ...fixture.params, redirect_uri: callback, configured_environments: '' };
      params.state = `fixture.${Buffer.from(JSON.stringify({flow_id:'fixture',redirect_uri:callback})).toString('base64url')}.fixture`;
      const body = fixture.html.replace(/(<script id=context type=application\/json>).*?(<\/script>)/s,
        (_, before, after) => before + JSON.stringify({params,chinese:true,origins:{...origins,custom:customOrigin}}).replaceAll('<','\\u003c') + after);
      return route.fulfill({status:200, headers:fixture.headers, body});
    }
    return route.fulfill({contentType:'text/html',body:'Authorization fixture'});
  });
  const waitPage = async prefix => {
    for (let n = 0; n < 100; n++) {
      const found = pages.find(page => page.url().startsWith(prefix));
      if (found) return found;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`Missing ${prefix}; pages=${pages.map(page => page.url())}`);
  };
  try {
    const source = await context.newPage();
    await source.goto(origin + '/start');
    await source.locator('button').click();
    const helper = await waitPage(origin + '/ha-origin-start/');
    if (blocked) {
      await helper.waitForFunction(() => /阻止|blocked/i.test(document.querySelector('#status')?.textContent ?? ''));
      await helper.locator('#connect').click();
    }
    const home = await waitPage(ha + '/config/integrations');
    const opened = context.waitForEvent('page');
    await home.locator('button').click();
    const external = await opened;
    await external.waitForURL(origin + '/pair/bootstrap?**');
    assert(!seen.some(value => Object.values(origins).includes(value) && value !== origin));
    console.log(`PASS ${origin} ${blocked ? 'blocked-popup fallback' : 'single click'}`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({headless:true, ignoreDefaultArgs:['--disable-popup-blocking']});
  try {
    for (const origin of Object.values(origins)) await exercise(browser, origin, false);
    await exercise(browser, customOrigin, false);
    await exercise(browser, origins.uat, true);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
