const APP_URL = 'http://127.0.0.1:8788';
const POLL_MS = 1500;
let busy = false;
let lastError = '';

const identityReady = (async () => {
  const stored = await chrome.storage.local.get('accountId');
  if (!stored.accountId) await chrome.storage.local.set({ accountId: crypto.randomUUID().replaceAll('-', '') });
})();

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function request(path, body) {
  const response = await fetch(APP_URL + path, {
    method: 'POST', cache: 'no-store', credentials: 'include',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Bridge HTTP ${response.status}`);
  return data;
}

async function findReadyTab() {
  const tabs = (await chrome.tabs.query({ url: ['https://affiliate.shopee.co.id/*'] }))
    .filter(tab => tab.url === 'https://affiliate.shopee.co.id/offer/custom_link');
  if (tabs.length > 1) throw new Error('Sisakan satu tab Custom Link pada profil ini.');
  if (!tabs.length) return null;
  try {
    const state = await chrome.tabs.sendMessage(tabs[0].id, { type: 'ping' });
    return state?.ready && state.protocol_version === 2 ? tabs[0] : null;
  } catch (_) {
    return null;
  }
}

async function recoverResult(tabId, job, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const result = await chrome.tabs.sendMessage(tabId, { type: 'collect_result', job_id: job.id, count: job.links.length });
      if (result?.results?.length === job.links.length) return result;
    } catch (_) {
      // Only the tab that accepted this job may provide its recovered result.
    }
    await wait(500);
  }
  return null;
}

async function tick() {
  if (busy) return;
  busy = true;
  let heartbeat;
  try {
    await identityReady;
    const config = await chrome.storage.local.get(['accountId', 'accountName']);
    if (!config.accountName) {
      lastError = 'Klik ikon extension dan isi nama akun untuk profil ini.';
      return;
    }
    const identity = { protocol_version: 2, account_id: config.accountId };
    let tab = null;
    let tabError = '';
    try { tab = await findReadyTab(); } catch (error) { tabError = error.message; }
    const ping = () => request('/api/bridge/ping', {
      ...identity, account_name: config.accountName, ready: Boolean(tab), url: tab?.url || '',
    });
    await ping();
    lastError = tabError || (tab ? '' : 'Buka satu tab Custom Link, login Shopee, lalu reload tab.');
    if (!tab) return;
    const next = await request('/api/bridge/next', identity);
    if (!next.job) return;
    if (next.job.account_id !== config.accountId) throw new Error('Akun job tidak cocok dengan profil ini.');
    heartbeat = setInterval(() => { ping().catch(() => {}); }, 2000);
    let result;
    try {
      result = await chrome.tabs.sendMessage(tab.id, { type: 'run_job', job: next.job });
    } catch (_) {
      result = await recoverResult(tab.id, next.job);
    }
    if (!result?.results?.length && !result?.error) result = await recoverResult(tab.id, next.job);
    await request('/api/bridge/result', {
      ...identity, job_id: next.job.id, lease_token: next.lease_token,
      results: result?.results || null,
      error: result?.error || (result?.results ? '' : 'Hasil job tidak dapat dipastikan. Periksa tab akun ini lalu kirim ulang bila diperlukan.'),
      error_kind: result?.error_kind || (result?.results ? '' : 'needs_session'),
    });
  } catch (error) {
    lastError = error.message || 'Webapp lokal belum dapat dihubungi.';
  } finally {
    clearInterval(heartbeat);
    busy = false;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'bridge_status') return false;
  sendResponse({ busy, error: lastError });
  return false;
});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create('fahra-bridge-wake', { periodInMinutes: 0.5 });
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'fahra-bridge-wake') tick();
});
function schedule() {
  tick().finally(() => setTimeout(schedule, POLL_MS));
}
schedule();
