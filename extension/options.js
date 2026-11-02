const nameInput = document.querySelector('#account-name');
const message = document.querySelector('#message');
async function load() {
  const config = await chrome.storage.local.get('accountName');
  nameInput.value = config.accountName || '';
}
async function refreshStatus() {
  try {
    const status = await chrome.runtime.sendMessage({ type: 'bridge_status' });
    document.querySelector('#bridge-status').textContent = status?.error || (status?.busy ? 'Bridge sedang memeriksa atau memproses antrean.' : 'Bridge berjalan. Periksa kesiapan akun pada dashboard.');
  } catch (_) {
    document.querySelector('#bridge-status').textContent = 'Reload extension untuk mengaktifkan bridge.';
  }
}
document.querySelector('#account-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    const accountName = nameInput.value.trim();
    if (!accountName || accountName.length > 80) throw new Error('Isi nama akun, maksimal 80 karakter.');
    await chrome.storage.local.set({ accountName });
    message.textContent = 'Nama tersimpan. Akun akan muncul setelah bridge terhubung ke dashboard lokal.';
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
load().catch(() => { message.textContent = 'Pengaturan belum dapat dibaca. Reload extension.'; });
refreshStatus();
setInterval(refreshStatus, 2500);
