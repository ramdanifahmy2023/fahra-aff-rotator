# AGENTS.md

Panduan ini adalah konteks kerja untuk Codex atau agent lain yang mengubah
repository `fahra-aff-rotator`. Baca file ini bersama `PRD.md`, `TOOLS.md`,
`README.md`, dan `CHANGELOG.md` sebelum mengubah kode.

## Arah aplikasi

Fahra Affiliate Rotator adalah webapp lokal dan worker durable untuk membuat
custom link Shopee Affiliate dari satu sampai lima URL. Jalur utama memakai
extension bridge ke tab Chrome Affiliate Shopee yang sudah login. Worker tidak
membuka browser baru, tidak memakai Shopee Open API, dan tidak mengulang raw
HTTP sebagai jalur produksi karena gateway Shopee dapat menolak request dengan
anti-bot `90309999`.

## Aturan yang wajib dipertahankan

- Credential tetap lokal. Jangan commit `.env`, cookie, request header, database,
  profile browser, log runtime, atau token sesi.
- Jangan mencetak nilai cookie, header sensitif, lease token, atau response sesi ke
  log, issue, commit, atau output final.
- Jangan mengganti extension bridge dengan stealth browser, bypass CAPTCHA, atau
  replay request mentah sebagai solusi default.
- Pertahankan satu tab `https://affiliate.shopee.co.id/offer/custom_link` yang
  sudah login. Extension memilih tab itu dan mengirim job melalui halaman Shopee.
- Status `HTTP 200`, `202 queued`, atau `bridge connected` bukan bukti link selesai.
  Selalu cek status job dan `result.shortLink` dari `/api/jobs`.
- Perubahan extension harus menaikkan versi di `extension/manifest.json` dan
  didokumentasikan di `CHANGELOG.md`. Setelah itu extension dan tab Shopee harus
  di-reload manual.
- Perubahan database harus menjaga commit/rollback eksplisit dan menutup koneksi.
  Job `running` harus dapat dipulihkan setelah daemon restart.
- Jangan menghapus data runtime atau mengubah LaunchAgent secara destruktif tanpa
  alasan yang jelas dan bukti bahwa data tersebut disposable.

## Urutan audit sebelum mengubah kode

1. Baca `PRD.md` untuk tujuan produk dan batas scope.
2. Baca `TOOLS.md` untuk port, process, database, extension, dan perintah validasi.
3. Periksa `git status --short --branch`.
4. Periksa runtime:

   ```sh
   curl -sS http://127.0.0.1:8788/api/health
   curl -sS http://127.0.0.1:8788/api/jobs
   launchctl print gui/$(id -u)/com.fahra-aff-rotator 2>/dev/null || true
   ```

5. Pisahkan temuan source, database, worker, bridge, browser, dan UI. Jangan
   menganggap salah satunya membuktikan yang lain.

## Aturan implementasi

- Pertahankan Python standard library untuk server lokal kecuali ada kebutuhan
  yang benar-benar jelas.
- Pertahankan extension Manifest V3 dan komunikasi lokal ke `127.0.0.1:8788`.
- Validasi URL tetap membatasi host ke `shopee.co.id` atau subdomain yang diizinkan
  oleh validasi saat ini. Jangan melonggarkan validasi hanya agar test lewat.
- Gunakan status job yang sudah ada: `queued`, `running`, `completed`, `failed`,
  dan `needs_session`.
- Error ke user harus menjelaskan tindakan berikutnya, misalnya reload tab,
  login/verifikasi manual, atau kirim ulang job.
- Jika menambah endpoint, dokumentasikan payload dan response di `TOOLS.md` serta
  README.
- Untuk UI, pertahankan arah visual dari `gamified-app.zip`: dark warm stage,
  kartu terang sebagai permukaan kerja, tipografi editorial, dan layout responsive.
  Jangan mengubah UI hanya untuk merapikan kode tanpa kebutuhan produk.

## Validasi wajib sebelum commit

```sh
python3 -m py_compile app.py
node --check extension/content.js
node --check extension/background.js
git diff --check
```

Jika worker atau bridge berubah, lakukan validasi runtime juga:

```sh
curl -sS http://127.0.0.1:8788/api/health
curl -sS http://127.0.0.1:8788/api/jobs
```

Untuk test end-to-end, gunakan satu URL Shopee yang memang diberikan user atau
URL test yang jelas, satu tag yang mudah dikenali, lalu tunggu `completed` dan
verifikasi short link terlihat pada UI/API. Jangan menganggap modal Shopee saja
sebagai persistence sampai row database berubah.

## Commit dan dokumentasi

- Commit harus menjelaskan perilaku yang berubah, bukan hanya nama file.
- Tambahkan entry `CHANGELOG.md` jika ada perubahan fungsional, runtime, keamanan,
  atau workflow.
- Jangan memasukkan log percakapan, screenshot, cookie, atau database ke commit.
- Setelah commit, cek remote dan branch:

  ```sh
  git status --short --branch
  git log --oneline -3
  git push origin main
  ```

## Batas yang masih dikenal

- Shopee tetap dapat meminta login atau verifikasi manual. Agent tidak boleh
  menyelesaikan CAPTCHA atau mengakali verifikasi.
- Bridge bergantung pada content script yang termuat di tab Custom Link. Setelah
  update extension, reload extension dan tab Shopee adalah bagian dari deployment.
- Mode `SHOPEE_TRANSPORT=http` hanya untuk diagnosis dan bisa terkena anti-bot.
- Mode multi-device atau public deployment belum menjadi bagian dari runtime aman;
  jangan mengekspos port lokal tanpa desain autentikasi dan TLS.

## Auth dan UI yang sudah diterapkan

- Webapp memiliki satu role `superadmin` dan session cookie HttpOnly yang disimpan
  sebagai hash di SQLite. API job dan semua endpoint bridge memerlukan session.
- Bootstrap admin dijalankan sekali dengan environment sementara:
  `SUPERADMIN_EMAIL=... SUPERADMIN_INITIAL_PASSWORD=... python3 app.py --bootstrap-admin`.
  Password awal wajib diganti dari panel dashboard; nilainya tidak boleh ditulis ke
  source, docs, log, atau commit.
- Origin bridge hanya direfleksikan untuk origin extension Chrome yang meminta
  credential. Wildcard CORS tidak digunakan.
- Dashboard dan halaman login mendukung tema Sistem, Terang, dan Gelap, dengan
  layout responsif untuk mobile. Setelah mengubah file extension, reload extension
  dan tab Custom Link agar worker memakai `credentials: include` terbaru.
