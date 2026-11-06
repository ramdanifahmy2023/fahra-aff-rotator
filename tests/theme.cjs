const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const puppeteer = require('puppeteer-core');

const root = path.resolve(__dirname, '..');
const html = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-c',
  'import app,json; print(json.dumps({"login":app.LOGIN_HTML,"dashboard":app.INDEX_HTML}))'
], { cwd: root, encoding: 'utf8', maxBuffer: 1024 * 1024 }));
const jobs = Array.from({ length: 7 }, (_, i) => ({
  id: `theme-fixture-${i}`, status: i === 0 ? 'queued' : 'completed',
  links: ['https://shopee.co.id/produk-uji-i.123.456'], tags: ['test'],
  created_at: '2026-10-20T03:00:00Z', updated_at: '2026-10-20T03:00:00Z',
  result: i === 0 ? [] : [{ shortLink: 'https://s.shopee.co.id/test' }],
}));
const server = http.createServer((req, res) => {
  let value;
  if (req.url === '/api/auth/me') value = { user: { email: 'test@example.invalid', must_change_password: false } };
  else if (req.url === '/api/health') value = { worker: true, bridge: { connected: true } };
  else if (req.url === '/api/jobs') value = { jobs };
  else if (req.url === '/api/mcp/tokens') value = { mcp_url: 'http://127.0.0.1:18790/mcp', tokens: [{ name: 'codex-ubuntu', status: 'active' }] };
  else if (req.url.startsWith('/api/')) { res.writeHead(405); res.end(); return; }
  else {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(req.url === '/login' ? html.login : html.dashboard);
    return;
  }
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(value));
});

async function contrast(page, selector) {
  return page.$eval(selector, element => {
    const rgb = value => value.match(/[\d.]+/g).map(Number);
    const luminance = color => color.slice(0, 3).map(v => {
      v /= 255;
      return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
    let background;
    for (let node = element; node; node = node.parentElement) {
      const color = rgb(getComputedStyle(node).backgroundColor);
      if (color.length === 3 || color[3] === 1) { background = color; break; }
    }
    const foreground = rgb(getComputedStyle(element).color);
    const a = luminance(foreground), b = luminance(background || [255, 255, 255]);
    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  });
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const executablePath = [process.env.CHROME_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'
  ].filter(Boolean).find(file => fs.existsSync(file));
  assert(executablePath, 'Set CHROME_PATH to a Chrome/Chromium executable.');
  const browser = await puppeteer.launch({ executablePath, headless: true });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1050 });
    await page.setRequestInterception(true);
    page.on('request', request => request.url().startsWith(url) ? request.continue() : request.abort());
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const screenshots = process.env.UI_SCREENSHOT_DIR;
    if (screenshots) fs.mkdirSync(screenshots, { recursive: true });
    for (const route of ['/login', '/#settings']) {
      await page.goto(url + route);
      if (route !== '/login') await page.waitForFunction(() => document.querySelector('#account-email').textContent.includes('@'));
      for (const theme of ['light', 'dark']) {
        await page.select('#theme-select', theme);
        assert.equal(await page.$eval('html', el => el.dataset.theme), theme);
        await page.reload();
        assert.equal(await page.$eval('#theme-select', el => el.value), theme, 'Theme persists after reload');
        if (route === '/login') {
          for (const selector of ['.dek', '.theme', '#login-button', '#email']) {
            assert(await contrast(page, selector) >= 4.5, `${theme} login contrast: ${selector}`);
          }
        } else {
          for (const view of ['dashboard', 'history', 'mcp', 'settings']) {
            await page.click(`[data-view-target="${view}"]`);
            await page.waitForSelector(`#view-${view}:not([hidden])`);
            for (const selector of ['.sidebar-brand strong', '.nav-item[aria-current="page"]', '.sidebar-user', '#view-dek', `#view-${view} .hint`]) {
              const ratio = await contrast(page, selector);
              assert(ratio >= 4.5, `${theme} ${view} contrast: ${selector} = ${ratio.toFixed(2)}`);
            }
            const button = { dashboard: '#submit', history: '.pagination button[aria-current="page"]', mcp: '#mcp-test', settings: '#password-form button' }[view];
            assert(await contrast(page, button) >= 4.5, `${theme} button contrast: ${button}`);
            await page.hover('[data-view-target="history"]');
            assert(await contrast(page, '[data-view-target="history"]') >= 4.5, `${theme} navigation hover contrast`);
            if (screenshots) await page.screenshot({ path: path.join(screenshots, `${theme}-${view}.png`), fullPage: true });
          }
          await page.evaluate(() => {
            window.themeSelectWrites = 0;
            const select = document.querySelector('#theme-select');
            const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
            Object.defineProperty(select, 'value', {
              get() { return descriptor.get.call(this); },
              set(value) { window.themeSelectWrites++; descriptor.set.call(this, value); },
              configurable: true,
            });
          });
          await page.evaluate(() => refresh());
          assert.equal(await page.evaluate(() => window.themeSelectWrites), 0, 'Polling must not reset an open theme selector');
        }
      }
      await page.select('#theme-select', 'system');
      for (const scheme of ['light', 'dark']) {
        await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
        await page.waitForFunction(theme => document.documentElement.dataset.theme === theme, {}, scheme);
      }
      await page.select('#theme-select', 'light');
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
      assert.equal(await page.$eval('html', el => el.dataset.theme), 'light', 'Explicit preference overrides OS');
      await page.evaluate(() => localStorage.setItem('fahra-theme', 'invalid'));
      await page.reload();
      assert.equal(await page.$eval('#theme-select', el => el.value), 'system', 'Invalid preference falls back to System');
    }
    const other = await browser.newPage();
    await other.goto(url + '/login');
    await other.select('#theme-select', 'light');
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light');
    assert.equal(await page.$eval('#theme-select', el => el.value), 'light', 'Preference synchronizes between tabs');
    await other.close();
    for (const width of [375, 768]) {
      await page.setViewport({ width, height: 900 });
      for (const view of ['dashboard', 'history', 'mcp', 'settings']) {
        await page.click(`[data-view-target="${view}"]`);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
        assert(!overflow, `${view} overflows at ${width}px`);
      }
    }
    assert.deepEqual(errors, [], 'No JavaScript errors');
    console.log('Theme checks passed: switching, reload, polling, OS preference, cross-tab sync, contrast, responsive views.');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { server.close(); console.error(error); process.exitCode = 1; });
