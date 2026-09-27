# PRD — Fahra Affiliate Rotator

**Status:** berjalan pada runtime lokal, masih dalam tahap hardening bridge<br>
**Pemilik:** Fahra<br>
**Tanggal konteks:** 2026-09-27<br>
**Repository:** `ramdanifahmy2023/fahra-aff-rotator`

## Ringkasan

Fahra Affiliate Rotator membantu operator affiliate membuat custom link Shopee
dari satu sampai lima URL dalam satu batch. Operator memasukkan URL dan tag pada
webapp lokal. Worker menyimpan job secara durable, lalu extension mengerjakan
job melalui tab Shopee Affiliate yang sudah login di browser pengguna.

Produk sengaja memakai sesi browser pengguna sebagai jalur utama. Shopee Open API
tidak tersedia untuk akun ini, dan replay HTTP mentah berisiko terkena anti-bot.

## Masalah yang diselesaikan

Proses manual membuat custom link berulang, sulit dilacak, dan rentan kehilangan
hasil ketika halaman atau worker berubah state. Operator membutuhkan satu tempat
untuk:

- memasukkan beberapa URL sekaligus;
- memberi tag tracking yang konsisten;
- melihat antrean dan status hasil;
- mempertahankan job ketika service restart;
- tetap memakai sesi Shopee yang sudah dipercaya browser.

## Pengguna sasaran

Operator affiliate yang mengelola banyak link produk Shopee dan sudah memiliki
akun Affiliate Shopee aktif pada Chrome. Pengguna memahami bahwa login atau
verifikasi manual tetap dilakukan pada browser ketika Shopee memintanya.

## Tujuan produk

1. Membuat custom link dari 1–5 URL Shopee dalam satu job.
2. Menjaga antrean dan hasil secara durable dengan SQLite.
3. Mengurangi pemicu anti-bot dengan memakai halaman Affiliate resmi pada sesi
   browser yang sudah login.
4. Menampilkan hasil short link yang dapat langsung disalin.
5. Menyediakan jejak perubahan teknis melalui Git, changelog, dan dokumentasi.

## Bukan tujuan produk

- Mengakses Shopee Open API.
- Mengalahkan CAPTCHA, verifikasi, fingerprinting, atau anti-bot Shopee.
- Membuka browser baru atau membuat profile stealth sebagai jalur utama.
- Menyimpan cookie atau token sesi di GitHub, database publik, atau frontend.
- Menjadi SaaS publik tanpa autentikasi, TLS, tenancy, dan desain keamanan baru.
- Menjamin semua job berhasil ketika Shopee meminta interaksi manual.

## Alur utama pengguna

1. Pengguna membuka Chrome dengan akun Affiliate Shopee yang sudah login.
2. Pengguna membuka satu tab Custom Link.
3. Pengguna menjalankan worker lokal dan membuka `http://127.0.0.1:8788`.
4. Pengguna memasukkan 1–5 URL Shopee dan maksimal 5 tag alfanumerik.
5. Webapp membuat job berstatus `queued`.
6. Worker mengambil job ketika extension melaporkan tab siap.
7. Extension mengisi URL/tag pada halaman Shopee dan menekan `Buat Link`.
8. Extension membaca short link dari modal Shopee dan mengirim hasil ke worker.
9. Worker menyimpan `completed` dan webapp menampilkan link.
10. Jika login/verifikasi diperlukan, job menjadi `needs_session`; pengguna
    memperbaiki sesi lalu mengirim job baru.

## Persyaratan fungsional

### Input dan validasi

- Menerima 1–5 URL per job.
- Menolak URL di luar domain Shopee Indonesia yang divalidasi aplikasi.
- Menerima maksimal 5 tag.
- Tag hanya alfanumerik sesuai batas halaman Shopee.
- Menolak payload kosong, batch terlalu besar, atau URL tidak valid.

### Queue dan persistence

- Status minimum: `queued`, `running`, `completed`, `failed`, `needs_session`.
- Job memiliki ID, waktu dibuat, waktu diperbarui, input URL, tag, attempt, error,
  dan hasil.
- INSERT/UPDATE harus di-commit dan rollback saat exception.
- Koneksi SQLite ditutup setelah setiap operasi.
- Job `running` dipulihkan menjadi `queued` ketika daemon restart.
- Hasil hanya dianggap selesai jika row database memiliki `completed` dan short link.

### Extension bridge

- Hanya memakai tab Affiliate Shopee yang sudah dibuka pengguna.
- Memprioritaskan tab Custom Link.
- Mengisi URL/tag lewat DOM halaman, bukan request mentah.
- Membaca hasil dari modal, termasuk field `input` atau `textarea` yang disabled.
- Memiliki recovery ketika channel pesan Chrome terputus atau halaman merender ulang.
- Tidak mengirim cookie Shopee ke webapp; request bridge hanya membawa session
  aplikasi HttpOnly agar endpoint lokal terlindungi.

### Webapp dan API

- UI mengikuti referensi `gamified-app.zip`: dark warm stage, permukaan kartu terang,
  tipografi editorial, dan responsive layout.
- UI menampilkan sinyal worker/bridge secara eksplisit.
- UI menampilkan job terbaru, status, error, dan short link.
- `POST /api/jobs` membuat job.
- `GET /api/jobs` menampilkan daftar job.
- `GET /api/jobs/<id>` menampilkan detail job.
- `GET /api/health` menampilkan status worker dan bridge tanpa membocorkan path
  filesystem internal.
- Halaman login membatasi akses ke satu superadmin. Session cookie HttpOnly dipakai
  untuk webapp dan extension bridge, dengan rate limit login dan password bootstrap
  yang wajib diganti.
- Dashboard menyediakan pemilih tema sistem/terang/gelap dan layout mobile friendly.

## Keamanan dan privasi

- `.env`, cookie, captured headers, database, browser profile, log, dan dependency
  hasil install tidak boleh masuk repository.
- Port default hanya bind ke `127.0.0.1`.
- Jangan menulis cookie, header, lease token, atau payload sesi ke log.
- Mode raw HTTP hanya untuk diagnosis dan harus tetap dianggap tidak andal terhadap
  anti-bot.
- Public deployment memerlukan autentikasi, TLS, CSRF/origin policy, rate limit,
  dan pemisahan data pengguna sebelum dipertimbangkan.

## Kriteria penerimaan

Fitur dianggap selesai bila:

1. Satu job dengan satu URL dapat dibuat dari UI.
2. Job bertahan setelah worker restart.
3. Dengan tab Shopee yang siap, job berubah menjadi `completed` dan short link muncul
   pada API serta UI.
4. Modal hasil yang dirender sebagai field disabled tetap dapat dibaca.
5. Saat sesi tidak siap, job mendapat `needs_session` dengan tindakan yang jelas.
6. Halaman job tidak dapat dibaca atau ditulis tanpa session superadmin.
7. Tema terang, gelap, dan sistem dapat dipilih pada login serta dashboard.
8. Tidak ada credential atau database runtime pada commit Git.
9. Validasi Python/JavaScript dan `git diff --check` lulus.

## Kondisi saat ini

Yang sudah berjalan:

- Webapp lokal dan worker Python.
- SQLite durable dengan recovery saat restart.
- Extension Manifest V3 untuk bridge tab browser.
- Fallback browser worker dan HTTP diagnosis tetap tersedia.
- Repository private dengan README dan changelog.
- Auth superadmin, session SQLite, bootstrap password satu kali, dan proteksi bridge.
- Halaman login, ganti password awal, tema sistem/terang/gelap, serta responsive UI.

Yang masih perlu diperbaiki:

- Recovery hasil ketika tab Shopee mengganti document di tengah job harus diuji pada
  beberapa bentuk modal dan navigasi.
- Pesan `needs_session` perlu membedakan login, CAPTCHA/verifikasi, tab salah, dan
  timeout hasil agar troubleshooting lebih cepat.
- Akses lintas-device masih memerlukan deployment terpisah dengan TLS dan kontrol
  jaringan; runtime saat ini tetap bind ke loopback.
- Belum ada observability terstruktur untuk latency job tanpa mencatat data sensitif.

## Roadmap

### Tahap 1 — Reliability lokal

- Stabilkan parser modal dan recovery channel extension.
- Tambahkan test fixture untuk response DOM Shopee yang berubah.
- Tambahkan command audit yang merangkum queue, lease, dan process tanpa rahasia.

### Tahap 2 — Operasional pribadi

- Tambahkan export hasil CSV atau copy batch.
- Tambahkan filter job berdasarkan tag/status/tanggal.
- Tambahkan retry manual untuk job `needs_session` dengan konfirmasi pengguna.

### Tahap 3 — Akses perangkat lain

- Pisahkan webapp dari worker browser.
- Tambahkan autentikasi dan TLS.
- Rancang pairing satu browser worker ke satu akun tanpa mengekspos cookie.
- Tambahkan audit log yang aman dan batas rate.

## Keputusan arsitektur yang tidak boleh dilupakan

Extension bridge adalah jalur produksi karena Shopee menjalankan pembuatan link
di halaman resmi dengan sesi browser yang sudah ada. Cookie file dan captured
headers adalah fallback/diagnosis lokal, bukan alasan untuk memindahkan credential
ke server atau frontend.
