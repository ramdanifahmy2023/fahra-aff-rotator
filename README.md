# Fahra Affiliate Rotator

Webapp lokal dan worker durable untuk membuat custom link Shopee Affiliate dari satu sampai lima URL per job.

Repository ini sengaja tidak menyimpan `.env`, cookie, header sesi, database runtime,
profile browser, atau `node_modules`. Siapkan file lokal dari `.env.example` dan isi
credential hanya pada mesin yang menjalankan worker.

## Menjalankan

```sh
./start.sh
```

Buka `http://127.0.0.1:8788`. Worker menyimpan antrean dan hasil pada lokasi `AFFILIATE_DB`; template macOS memakai `~/fahra-affiliate-data/affiliate.sqlite3` agar SQLite tidak bergantung pada volume project eksternal.

Saat pertama kali dipasang, seed superadmin sekali dari terminal. Gunakan password
bootstrap yang hanya ada di environment sementara, lalu ganti password dari panel
dashboard. Jangan menaruh password nyata di file atau commit:

```sh
SUPERADMIN_EMAIL="..." SUPERADMIN_INITIAL_PASSWORD="..." \
  AFFILIATE_DB="/path/ke/affiliate.sqlite3" python3 app.py --bootstrap-admin
```

Dashboard memakai sidebar responsif untuk Dashboard, MCP, Pengaturan, Riwayat, dan Keluar. Dashboard menampilkan lima hasil generate terbaru untuk salin cepat, sedangkan Riwayat menyimpan arsip lengkap. Sidebar desktop dapat diperkecil menjadi ikon. Riwayat memiliki badge job aktif dan filter status/tanggal, sedangkan MCP memiliki tombol tes koneksi. Tema Sistem/Terang/Gelap serta perubahan password tersedia di Pengaturan.
Pembuatan job terkunci sampai password bootstrap diganti.

Pada macOS worker dijalankan mandiri oleh LaunchAgent `com.fahra.aff-rotator`, sehingga tidak perlu menjalankan `./start.sh` setiap kali. Jika perintah itu dijalankan saat service sudah aktif, script hanya menampilkan alamat service lalu selesai.

Status service dapat diperiksa dengan:

```sh
launchctl print gui/$(id -u)/com.fahra.aff-rotator
curl http://127.0.0.1:8788/api/health
```

## MCP untuk OpenClaw dan agent lain

MCP berjalan pada port terpisah `8790` dan tidak memakai session cookie webapp.
Agent menggunakan token Bearer khusus yang dibuat otomatis sekali pada file
runtime `mcp-openclaw.token` dengan mode 0600. Cookie Shopee, lease bridge, dan
header rekaman tidak pernah dikirim ke MCP.

Pada Mac ini LaunchAgent membuka port MCP dengan allowlist loopback dan jaringan
Tailscale. Perangkat pada tailnet dapat memakai alamat Tailscale Mac ini:

```text
http://100.99.69.106:8790/mcp
```

Alamat tersebut hanya boleh diakses melalui jaringan privat Tailscale. Jangan
membuka port ini ke internet publik tanpa HTTPS reverse proxy, ACL, dan rotasi
token. Konfigurasi OpenClaw menggunakan transport `streamable-http`:

```json
{
  "mcp": {
    "servers": {
      "fahra-aff-rotator": {
        "url": "http://100.99.69.106:8790/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer ${FAHRA_AFF_MCP_TOKEN}"
        }
      }
    }
  }
}
```

Tool yang tersedia: `shopee_list_accounts`, `shopee_generate_short_links`, `shopee_get_job`,
`shopee_wait_for_job`, `shopee_health`, dan `shopee_list_recent_jobs`. Panggilan
generate wajib menyertakan `account_id` dari `shopee_list_accounts` dan mengembalikan `job_id`; hasil dianggap selesai hanya ketika status
`completed` dan `result[].shortLink` tersedia. `needs_session` berarti tab
Custom Link Shopee perlu login atau verifikasi manual.

Token MCP dapat dikelola dari menu **MCP** setelah login dashboard:
buat token per client, salin konfigurasi untuk OpenClaw/Codex/Claude/Hermes,
cabut akses, atau hapus token. Plaintext token hanya tampil sekali saat dibuat;
server menyimpan hash token dan metadata saja. Atur `MCP_PUBLIC_URL` agar URL yang
ditampilkan panel sesuai alamat yang dapat dijangkau client, misalnya alamat Tailscale.

## Extension bridge

1. Buka Chrome dengan akun Affiliate Shopee yang sudah login.
2. Buka tab `https://affiliate.shopee.co.id/offer/custom_link` dan biarkan tab itu terbuka.
3. Masuk ke `chrome://extensions`, aktifkan Developer mode, pilih Load unpacked, lalu pilih folder `extension/`.
4. Kembali ke tab Shopee lalu reload sekali agar content script extension terpasang.
5. Jika `.env` belum ada, buat dari template: `cp .env.example .env` (template sudah memakai `SHOPEE_TRANSPORT=extension`).
6. Jalankan `./start.sh`.

Extension hanya bekerja pada tab Affiliate yang sudah ada. Ia tidak membuka browser
baru dan tidak membaca cookie Shopee. Request bridge membawa session cookie aplikasi
HttpOnly agar endpoint lokal tidak terbuka tanpa login. Jika tab belum siap atau
verifikasi muncul, antrean menunggu dan job akan diberi status `needs_session` bila
batas waktunya lewat.

Gunakan satu tab `Link Khusus (Custom Link)` per profil Chrome. Setelah mengubah file extension, tekan **Reload** pada kartu extension di `chrome://extensions`, lalu reload tab Shopee tersebut sekali agar content script versi terbaru terpasang.

`Credential/cookies.txt` dibaca sebagai Cookie header oleh browser worker dan tidak pernah dikirim ke browser webapp. File cookie diberi mode 0600. Worker tidak lagi mengekspor header anti-bot ke startup.

Fallback `SHOPEE_TRANSPORT=browser` masih tersedia untuk profile browser khusus yang persisten. Jalur utama extension lebih aman untuk sesi karena tidak membuat browser atau profile baru.

Jika job berstatus `needs_session`, selesaikan login/verifikasi pada jendela worker lalu kirim job baru. Mode HTTP mentah tetap tersedia dengan `SHOPEE_TRANSPORT=http` untuk diagnosis saja dan dapat ditolak Shopee dengan `90309999`.

Payload `batchCustomLink` dari rekaman MCP dipakai sebagai referensi schema dan diagnosis. Pembuatan link tetap dilakukan melalui halaman Affiliate yang dibuka pada browser dengan sesi cookie pengguna.

## Banyak akun affiliate (bridge 0.2.0)

Setiap akun memakai profil Chrome terpisah, bukan beberapa tab akun dalam profil
sama. Pasang extension pada setiap profil, login dashboard lokal di profil itu,
lalu klik ikon extension dan simpan nama akun. ID profil dibuat otomatis di
`chrome.storage.local`, tidak memakai cookie Shopee. Jangan menyalin folder profil
Chrome untuk membuat akun baru karena ID extension dapat ikut tersalin.

Buka satu tab Custom Link yang sudah login per profil. Setelah bridge terhubung,
pilih **Buat link dari akun** pada dashboard. Riwayat menyimpan ID akun dan nama
saat job dibuat. Akun offline tetap bisa dipilih; job menunggu sampai profilnya
siap, tanpa berpindah ke akun lain. Worker masih memproses satu job pada satu
waktu; peningkatan ini menyediakan pemisahan akun, belum eksekusi paralel.

Nama akun adalah label operator, bukan identitas Shopee yang diverifikasi otomatis.
Pastikan profil tetap login ke akun Shopee yang sesuai. Untuk akun berbeda,
gunakan profil baru. Mengganti nama hanya mengganti label, bukan tujuan akun Shopee.
Jangan berganti login Shopee ketika ada job. Menghapus storage atau memasang ulang
extension dapat menghasilkan ID baru; job untuk ID lama tidak otomatis dipindahkan.

Sesudah upgrade, reload extension **dan** tab Shopee pada setiap profil, lalu isi
nama akun lewat ikon extension. Bridge lama ditolak. Riwayat lama dipertahankan;
job lama `queued`/`running` yang belum memiliki akun menjadi `needs_session` dan
perlu dikirim ulang dengan pilihan akun. Job baru yang sudah memiliki akun tetap
pulih menjadi `queued` setelah restart, dengan tujuan akun yang sama.

## API lokal

- `GET /api/accounts` mengembalikan `{ "accounts": [{ "id": "<32 hex>", "name": "Nama akun", "ready": true, "busy": false, "last_seen": 0 }] }`; `last_seen` adalah Unix timestamp atau null.
- `POST /api/jobs` dengan `{ "account_id": "<id dari /api/accounts>", "links": ["https://shopee.co.id/..."], "tags": ["konten"] }`; response `202` berisi `id`, `status`, `account_id`, dan `reused`.
- `GET /api/jobs` untuk job terbaru
- `GET /api/jobs/<id>` untuk hasil lengkap, termasuk `account_id` dan `account_name` (null untuk riwayat lama)
- `GET /api/health` untuk status worker
- `GET /api/auth/me`, `POST /api/auth/login`, `POST /api/auth/logout`, dan
  `POST /api/auth/change-password` untuk session superadmin.

Protokol bridge v2 memakai `POST /api/bridge/ping`, `/api/bridge/next`, dan
`/api/bridge/result`, semuanya dengan `protocol_version: 2` dan `account_id`.
Ping menambahkan `account_name`, `ready`, dan `url`; response berisi `ok`, `ready`,
dan `account_id`. Next mengembalikan `job: null` atau job dengan `id`, `account_id`,
`links`, `tags`, beserta lease privat. Result mengirim `job_id`, lease privat,
`results`, `error`, dan `error_kind`; response `{ "ok": true }`. Lihat TOOLS.md
untuk aturan delivery. Jangan menyalin lease ke log atau client MCP.

Endpoint akun, job dan bridge membutuhkan session login; `/api/health` hanya mengembalikan
ringkasan worker dan bridge tanpa path database internal.

Worker memvalidasi domain, membatasi batch menjadi lima URL dan lima tag, melakukan retry terbatas untuk error jaringan, dan tidak mencatat nilai cookie atau header sensitif ke log.

## Pengembangan dan perubahan

Setiap perubahan fungsional dicatat di `CHANGELOG.md` dan commit Git harus menjelaskan
perilaku yang diperbaiki. Validasi dasar sebelum push:

```sh
python3 -m py_compile app.py
node --check extension/content.js
node --check extension/background.js
```

Tes regresi bridge: `python3 -m unittest discover -s tests -v` dan `node --test tests/test_extension.cjs`. Semua memakai database sementara dan simulasi, tanpa membuat link Shopee nyata.
