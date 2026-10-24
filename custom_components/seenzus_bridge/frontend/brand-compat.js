// Compatibility for legacy HACS dashboards, including hacs-china 2.0.5.3.
// Only seenzus images inside <hacs-frontend> are changed. No HACS file,
// prototype, renderer, authentication token, or network API is modified.
const key = Symbol.for("seenzus.hacsIconCompatibility");
globalThis[key]?.stop();

const moduleUrl = new URL(import.meta.url);
const brandBase = new URL("../brand/", moduleUrl);
const version = moduleUrl.searchParams.get("v");
const failed = new WeakMap();
const pending = new WeakMap();
let scopes = [];
let nextDiscovery = 0;
let timer;
let stopped = false;

function imageUrl(source) {
  let remote;
  try {
    remote = new URL(source, moduleUrl);
  } catch {
    return undefined;
  }
  if (remote.origin !== "https://brands.home-assistant.io") return undefined;
  const match = remote.pathname.match(
    /^\/(?:_\/|marketplace\/)?seenzus_bridge\/(?:dark_)?((?:icon|logo)(?:@2x)?\.png)$/,
  );
  if (!match) return undefined;
  // Our transparent light assets are also suitable on dark backgrounds.
  const local = new URL(match[1], brandBase);
  if (version) local.searchParams.set("v", version);
  return local.href;
}

function rewrite(image) {
  const source = image.getAttribute("src");
  if (!source || failed.get(image) === source) return;
  const local = imageUrl(source);
  if (!local) return;
  pending.get(image)?.();
  // On an unavailable local asset retain HACS's existing fallback, without
  // retrying the same failing URL on every scan.
  const cleanup = () => {
    image.removeEventListener("error", onError);
    image.removeEventListener("load", cleanup);
    pending.delete(image);
  };
  const onError = () => {
    cleanup();
    if (image.src !== local) return;
    failed.set(image, source);
    image.setAttribute("src", source);
  };
  pending.set(image, cleanup);
  image.addEventListener("error", onError, { once: true });
  image.addEventListener("load", cleanup, { once: true });
  image.setAttribute("src", local);
}

function frameDocument(frame) {
  try {
    return frame.contentDocument;
  } catch {
    return undefined;
  }
}

function discover(root, found, visited = new Set()) {
  if (!root || visited.has(root)) return;
  visited.add(root);
  for (const element of root.querySelectorAll("*")) {
    if (element.localName === "hacs-frontend") found.push(element);
    if (element.shadowRoot) discover(element.shadowRoot, found, visited);
    if (element.localName === "iframe") discover(frameDocument(element), found, visited);
  }
}

function update(root) {
  for (const image of root.querySelectorAll("img")) rewrite(image);
  if (root.shadowRoot) update(root.shadowRoot);
  for (const element of root.querySelectorAll("*")) {
    if (element.shadowRoot) update(element.shadowRoot);
  }
}

function connected(scope) {
  if (!scope.isConnected) return false;
  if (scope.ownerDocument === document) return true;
  try {
    return Boolean(scope.ownerDocument.defaultView?.frameElement?.isConnected);
  } catch {
    return false;
  }
}

function tick() {
  if (stopped || document.hidden || !/(?:^|\/)hacs(?:\/|$)/.test(location.pathname)) return;
  scopes = scopes.filter(connected);
  // Cache the HACS scope; do not continually traverse the outer HA dashboard.
  if (!scopes.length || Date.now() >= nextDiscovery) {
    const found = [];
    discover(document, found);
    scopes = [...new Set(found)];
    nextDiscovery = Date.now() + 5000;
  }
  for (const scope of scopes) update(scope);
}

function pause() {
  if (timer !== undefined) clearInterval(timer);
  timer = undefined;
}

function resume() {
  pause();
  if (stopped || document.hidden) return;
  tick();
  // The bounded scan also handles late Lit shadow roots, virtualized rows,
  // iframe reloads and theme changes without modifying attachShadow.
  timer = setInterval(tick, 1000);
}

function stop() {
  stopped = true;
  pause();
  scopes = [];
  removeEventListener("location-changed", tick);
  removeEventListener("popstate", tick);
  removeEventListener("pagehide", pause);
  removeEventListener("pageshow", resume);
  document.removeEventListener("visibilitychange", resume);
}

globalThis[key] = { stop };
addEventListener("location-changed", tick);
addEventListener("popstate", tick);
addEventListener("pagehide", pause);
addEventListener("pageshow", resume);
document.addEventListener("visibilitychange", resume);
resume();
