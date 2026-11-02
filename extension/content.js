let activeJobId = null;
let jobRunning = false;
let completedJobResult = null;

function visible(element) {
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
}

const SHORT_LINK_PATTERN = /https:\/\/s\.shopee\.co\.id\/[A-Za-z0-9]+/g;

function extractShortLinks(value) {
  return String(value || '').match(SHORT_LINK_PATTERN) || [];
}

function shortLinks() {
  const values = [...document.querySelectorAll('input, textarea')]
    .filter(visible)
    .flatMap(input => extractShortLinks(input.value));
  const textMatches = extractShortLinks(document.body.innerText);
  const hrefMatches = [...document.querySelectorAll('a[href]')]
    .flatMap(link => extractShortLinks(link.href));
  return [...new Set([...values, ...textMatches, ...hrefMatches])];
}

function resultModalVisible() {
  return [...document.querySelectorAll('body *')]
    .some(element => visible(element) && /Mohon salin link singkat|Link dari Link Khusus/i.test(element.textContent || ''));
}

function ready() {
  if (location.pathname !== '/offer/custom_link') return false;
  return Boolean(document.querySelector('textarea') && [...document.querySelectorAll('button')].some(button => /Buat Link/i.test(button.textContent || '')));
}

function setValue(selector, value) {
  const element = document.querySelector(selector);
  if (!element) return false;
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  setter?.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
  element.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function closeResultModal() {
  const close = [...document.querySelectorAll('button')]
    .find(button => visible(button) && /^(Close|Tutup)$/i.test((button.textContent || '').trim()));
  if (close) {
    close.click();
    await wait(250);
  }
}

async function runJob(job) {
  if (location.pathname.includes('/login') || location.pathname.includes('/verify/')) {
    return { error: 'Tab Shopee perlu login/verifikasi manual', error_kind: 'needs_session' };
  }
  if (!ready()) return { error: 'Halaman Link Khusus belum siap', error_kind: 'needs_session' };
  await closeResultModal();
  const before = shortLinks().join('|');
  if (!setValue('textarea', job.links.join('\n'))) return { error: 'Textarea URL tidak ditemukan' };
  for (let i = 0; i < 5; i++) {
    setValue(`#customLink_sub_id${i + 1}`, job.tags?.[i] || '');
  }
  const button = [...document.querySelectorAll('button')].find(element => /Buat Link/i.test(element.textContent || ''));
  if (!button) return { error: 'Tombol Buat Link tidak ditemukan' };
  button.click();
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const links = shortLinks();
    const freshLinks = links.filter(link => !before.includes(link));
    if (freshLinks.length >= job.links.length && (resultModalVisible() || freshLinks.length > 0)) {
      return { results: freshLinks.slice(0, job.links.length).map(shortLink => ({ shortLink, longLink: '', failCode: 0 })) };
    }
    if (location.pathname.includes('/login') || location.pathname.includes('/verify/')) {
      return { error: 'Shopee meminta login/verifikasi manual', error_kind: 'needs_session' };
    }
    await wait(500);
  }
  return { error: 'Shopee belum mengembalikan short link dalam 60 detik. Periksa tab akun ini sebelum mengirim ulang.', error_kind: 'needs_session' };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'ping') {
    sendResponse({ ready: ready(), protocol_version: 2 });
    return false;
  }
  if (message?.type === 'run_job') {
    if (jobRunning || activeJobId === message.job.id) {
      sendResponse({ error: 'Job sudah diterima tab ini. Periksa hasil sebelum mengirim ulang.', error_kind: 'needs_session' });
      return false;
    }
    activeJobId = message.job.id;
    completedJobResult = null;
    jobRunning = true;
    runJob(message.job).then(result => { completedJobResult = result?.results ? result : null; sendResponse(result); }).catch(error => sendResponse({ error: String(error?.message || error), error_kind: 'needs_session' })).finally(() => { jobRunning = false; });
    return true;
  }
  if (message?.type === 'collect_result') {
    sendResponse(activeJobId === message.job_id && !jobRunning ? completedJobResult : null);
    return false;
  }
  return false;
});
