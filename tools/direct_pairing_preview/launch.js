/* Validation-only launcher. Deploy on the originating app's own origin. */
(() => {
  'use strict';
  const environments = {
    cn: 'https://app.seenzus.cn', global: 'https://seenzus.ai', uat: 'https://uat.seenzus.ai',
  };
  const environment = Object.keys(environments).find(key => environments[key] === location.origin);
  const button = document.getElementById('connect');
  const status = document.getElementById('status');
  const callbackPath = '/api/seenzus_bridge/quick_pair/callback';
  let active;
  if (!environment) {
    button.disabled = true;
    status.textContent = '请从 seenzus 应用环境打开此验证入口。';
    return;
  }
  button.addEventListener('click', () => {
    if (active && !active.window.closed && performance.now() < active.expires) {
      active.window.focus();
      return;
    }
    const started = performance.now();
    const popup = window.open('https://my.home-assistant.io/redirect/config_flow_start/?domain=seenzus_bridge', '_blank');
    if (!popup) {
      status.textContent = '浏览器阻止了新窗口，请允许本页打开弹窗后重试。';
      return;
    }
    active = { window: popup, expires: started + 15 * 60_000, state: null };
    status.textContent = '已打开 HA。完成设备范围配置后，将自动回到当前环境授权。请保持本页打开。';
  });
  window.addEventListener('message', event => {
    if (!active || active.window.closed || performance.now() >= active.expires) return;
    const data = event.data;
    if (!data || data.type !== 'seenzus:pairing-origin:request'
        || typeof data.requestId !== 'string' || !/^[\w-]{16,128}$/.test(data.requestId)
        || typeof data.state !== 'string' || data.state.length < 16 || data.state.length > 8192) return;
    try {
      // Only the external window opened by this exact HA window can claim this
      // launch. Origin alone cannot distinguish concurrent flows or HA instances.
      if (!event.source || event.source.opener !== active.window) return;
      const callback = new URL(data.redirectUri);
      if (callback.origin !== event.origin || callback.pathname !== callbackPath
          || callback.username || callback.password || callback.search || callback.hash
          || !['http:', 'https:'].includes(callback.protocol)) return;
      // The signed state is opaque here. Window identity binds this launch;
      // comparing the PC clock with HA's clock would reject healthy instances.
      if (active.state && active.state !== data.state) return;
      active.state = data.state;
      event.source.postMessage({ type: 'seenzus:pairing-origin:response', requestId: data.requestId,
        state: data.state, environment }, event.origin);
      status.textContent = '已确认当前应用环境，正在打开授权页面。';
    } catch (_) { /* Broken window isolation or malformed routing hint: never guess. */ }
  });
})();
