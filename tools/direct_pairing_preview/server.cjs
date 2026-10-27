/* Validation-only static entry: node tools/direct_pairing_preview/server.cjs
 * Reverse proxy ONLY /ha-connect-direct[/...] to port 8788.
 * No database, application secrets, or API access is needed.
 */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const files = {
  '/ha-connect-direct': ['index.html', 'text/html; charset=utf-8'],
  '/ha-connect-direct/': ['index.html', 'text/html; charset=utf-8'],
  '/ha-connect-direct/launch.js': ['launch.js', 'text/javascript; charset=utf-8'],
  '/ha-connect-direct/style.css': ['style.css', 'text/css; charset=utf-8'],
};
const bodies = Object.fromEntries(Object.entries(files).map(([url, [file, type]]) =>
  [url, { body: fs.readFileSync(path.join(__dirname, file)), type }]));
http.createServer((request, response) => {
  const resource = bodies[new URL(request.url, 'http://localhost').pathname];
  if (!['GET', 'HEAD'].includes(request.method) || !resource) {
    response.writeHead(404, { 'Cache-Control': 'no-store' });
    response.end();
    return;
  }
  response.writeHead(200, {
    'Content-Type': resource.type, 'Content-Length': resource.body.length,
    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
    'Cross-Origin-Opener-Policy': 'same-origin-allow-popups',
    'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  });
  response.end(request.method === 'HEAD' ? undefined : resource.body);
}).listen(8788, '0.0.0.0');
