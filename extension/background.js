const APP_URL = 'http://127.0.0.1:8788';
const POLL_MS = 1500;
let busy = false;

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function request(path, options = {}) {
  const response = await fetch(APP_URL + path, {
    cache: 'no-store',
    credentials: 'include',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Bridge HTTP ${response.status}`);
  return body;
}

async function findReadyTab() {
  const active = await chrome.tabs.query({ active: true, lastFocusedWindow: true, url: ['https://affiliate.shopee.co.id/*'] });
  const tabs = await chrome.tabs.query({ url: ['https://affiliate.shopee.co.id/*'] });
  const ordered = [...new Map([...active, ...tabs].map(tab => [tab.id, tab])).values()]
    .sort((a, b) =>
      Number(b.url?.includes('/offer/custom_link')) - Number(a.url?.includes('/offer/custom_link')) ||
      Number(b.active) - Number(a.active) ||
      Number(b.lastAccessed || 0) - Number(a.lastAccessed || 0));
  for (const tab of ordered) {
    try {
      const state = await chrome.tabs.sendMessage(tab.id, { type: 'ping' });
      if (state?.ready) return tab;
    } catch (_) {
      // The content script is not ready on this tab yet.
    }
  }
  return null;
}

async function recoverResult(tabId, count, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const result = await chrome.tabs.sendMessage(tabId, { type: 'collect_result', count });
      if (result?.results?.length >= count) return result;
    } catch (_) {
      // The tab may be reloading; retry after the content script is ready.
    }
    await wait(500);
  }
  return null;
}

async function tick() {
  if (busy) return;
  let tab;
  try {
    tab = await findReadyTab();
    await request('/api/bridge/ping', {
      method: 'POST',
      body: JSON.stringify({ ready: Boolean(tab), url: tab?.url || '' }),
    });
    if (!tab) return;
    const next = await request('/api/bridge/next');
    if (!next.job) return;
    busy = true;
    let result;
    try {
      result = await chrome.tabs.sendMessage(tab.id, { type: 'run_job', job: next.job });
    } catch (error) {
      result = await recoverResult(tab.id, next.job.links.length);
      if (!result) result = { error: String(error?.message || error), error_kind: 'needs_session' };
    }
    if (!result?.results?.length) {
      const recovered = await recoverResult(tab.id, next.job.links.length);
      if (recovered) result = recovered;
    }
    await request('/api/bridge/result', {
      method: 'POST',
      body: JSON.stringify({
        job_id: next.job.id,
        lease_token: next.lease_token,
        results: result?.results || null,
        error: result?.error || '',
        error_kind: result?.error_kind || '',
      }),
    });
  } catch (error) {
    // Keep polling. The local webapp may be stopped while Chrome remains open.
  } finally {
    busy = false;
  }
}

function schedule() {
  tick().finally(() => setTimeout(schedule, POLL_MS));
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create('fahra-bridge-wake', { periodInMinutes: 0.5 });
  schedule();
});
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'fahra-bridge-wake') tick();
});
schedule();
