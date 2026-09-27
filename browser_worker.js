#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const [linksJson, tagsJson] = process.argv.slice(2);
if (!linksJson || !tagsJson) {
  process.stdout.write(JSON.stringify({ error: 'links dan tags wajib diisi' }));
  process.exit(2);
}

const links = JSON.parse(linksJson);
const tags = JSON.parse(tagsJson);
const cookieFile = process.env.SHOPEE_COOKIE_FILE || path.join(__dirname, 'Credential', 'cookies.txt');
const origin = process.env.SHOPEE_ORIGIN || 'https://affiliate.shopee.co.id';
const executablePath = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const profile = process.env.SHOPEE_BROWSER_PROFILE || path.join(__dirname, 'data', 'shopee-browser-profile');
const headless = process.env.SHOPEE_HEADLESS === 'true';
const manualWaitMs = Number(process.env.SHOPEE_MANUAL_VERIFY_WAIT_SECONDS || 180) * 1000;

function readCookies() {
  const raw = fs.readFileSync(cookieFile, 'utf8').trim();
  return raw.split(';').map(part => part.trim()).filter(Boolean).map(part => {
    const at = part.indexOf('=');
    if (at < 1) return null;
    return { name: part.slice(0, at).trim(), value: part.slice(at + 1).trim(), domain: '.shopee.co.id', path: '/', secure: true };
  }).filter(Boolean);
}

async function main() {
  fs.mkdirSync(profile, { recursive: true, mode: 0o700 });
  const browser = await puppeteer.launch({
    executablePath,
    headless,
    userDataDir: profile,
    args: ['--no-first-run', '--no-default-browser-check'],
    defaultViewport: { width: 1440, height: 1000 },
  });
  try {
    const page = await browser.newPage();
    await page.goto(origin + '/offer/custom_link', { waitUntil: 'networkidle2', timeout: 60000 });
    let initialUrl = await page.url();
    if (initialUrl.includes('/login') || initialUrl.includes('/verify/')) {
      // Prefer the persisted browser session. Inject the exported cookie only when
      // the profile has no usable session yet, then let Shopee reload its own state.
      await page.setCookie(...readCookies());
      await page.goto(origin + '/offer/custom_link', { waitUntil: 'networkidle2', timeout: 60000 });
      initialUrl = await page.url();
    }
    if (initialUrl.includes('/login') || initialUrl.includes('/verify/')) {
      if (headless) {
        throw new Error('Sesi Shopee perlu login/verifikasi manual pada browser worker');
      }
      const deadline = Date.now() + manualWaitMs;
      while (Date.now() < deadline) {
        const currentUrl = await page.url();
        if (!currentUrl.includes('/login') && !currentUrl.includes('/verify/')) break;
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      const currentUrl = await page.url();
      if (currentUrl.includes('/login') || currentUrl.includes('/verify/')) {
        throw new Error('Sesi Shopee belum siap setelah menunggu login/verifikasi manual');
      }
    }
    try {
      await page.waitForSelector('textarea', { timeout: 30000 });
    } catch (error) {
      throw new Error(`Halaman custom link tidak siap (${await page.url()})`);
    }
    await page.$eval('textarea', (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }, links.join('\n'));
    for (let i = 0; i < 5; i++) {
      if (tags[i]) {
        await page.$eval(`#customLink_sub_id${i + 1}`, (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }, tags[i]);
      }
    }
    await page.evaluate(() => {
      const button = [...document.querySelectorAll('button')].find(el => /Buat Link/i.test(el.textContent));
      if (!button) throw new Error('Tombol Buat Link tidak ditemukan');
      button.click();
    });
    await page.waitForFunction(() => [...document.querySelectorAll('input')].some(el => /^https:\/\/s\.shopee\.co\.id\//.test(el.value)), { timeout: 60000 });
    const shortLinks = await page.$$eval('input', inputs => inputs.map(input => input.value).filter(value => /^https:\/\/s\.shopee\.co\.id\//.test(value)));
    if (!shortLinks.length) throw new Error('Shopee tidak mengembalikan short link');
    process.stdout.write(JSON.stringify({ results: shortLinks.map(shortLink => ({ shortLink, longLink: '', failCode: 0 })) }));
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  process.stdout.write(JSON.stringify({ error: String(error && error.message || error) }));
  process.exitCode = 1;
});
