# TOOLS.md

Referensi operasional untuk agent yang menjalankan, memeriksa, atau mengubah
Fahra Affiliate Rotator.

## Lokasi penting

| Komponen | Lokasi atau nilai |
|---|---|
| Project | `/Volumes/DATA KERJA /Project Dev/Fahra-aff-rotator` |
| Webapp | `http://127.0.0.1:8788/` |
| Health | `http://127.0.0.1:8788/api/health` |
| Job list | `http://127.0.0.1:8788/api/jobs` |
| Database runtime macOS | `/Users/fahra/fahra-affiliate-data/affiliate.sqlite3` pada `.env` aktif |
| Template database | `data/affiliate.sqlite3` pada `.env.example` |
| Cookie fallback | `Credential/cookies.txt` lokal, selalu ignored |
| Captured header fallback | `Credential/request_headers.json` lokal, selalu ignored |
| Extension | `extension/` |
| LaunchAgent | `~/Library/LaunchAgents/com.fahra.aff-rotator.plist` |
| Error log | `/tmp/fahra-aff-rotator.err` |
| Standard output | `/tmp/fahra-aff-rotator.out` |
| MCP endpoint | `http://100.99.69.106:8790/mcp` melalui Tailscale |
| MCP token | `/Users/fahra/fahra-affiliate-data/mcp-openclaw.token` mode 0600 |

## Menjalankan dan memeriksa service

Jalankan dari root project:

```sh
./start.sh
```

`start.sh` memuat `.env`, mengunci mode cookie ke `0600`, memeriksa port, lalu
menjalankan `app.py`. Jika port sudah dipakai, script tidak memulai process kedua.

Periksa process, port, dan health secara terpisah:

```sh
lsof -nP -iTCP:8788 -sTCP:LISTEN
curl -sS http://127.0.0.1:8788/api/health
tail -n 100 /tmp/fahra-aff-rotator.err
lsof -nP -iTCP:8790 -sTCP:LISTEN
```

Pada macOS, LaunchAgent biasanya menjaga process tetap hidup:

```sh
launchctl print gui/$(id -u)/com.fahra-aff-rotator
launchctl kickstart -k gui/$(id -u)/com.fahra-aff-rotator
```

Gunakan `kickstart` hanya setelah perubahan source atau saat service memang perlu
direstart. Setelah restart, cek kembali health dan database.

## Environment

`SHOPEE_TRANSPORT=extension` adalah jalur default dan yang direkomendasikan.

| Variable | Fungsi |
|---|---|
| `HOST` / `PORT` | Bind server lokal, default `127.0.0.1:8788` |
| `AFFILIATE_DB` | Path SQLite durable |
| `SHOPEE_TRANSPORT` | `extension`, `browser`, atau `http` diagnosis |
| `SHOPEE_BRIDGE_WAIT_SECONDS` | Batas tunggu bridge, default 180 detik |
| `MAX_RETRIES` | Retry worker untuk error yang dapat dicoba lagi |
| `WORKER_POLL_SECONDS` | Interval polling queue |
| `SHOPEE_COOKIE_FILE` | Cookie fallback lokal; jangan commit |
| `SHOPEE_REQUEST_HEADERS_FILE` | Header hasil capture fallback; jangan commit |
| `SHOPEE_BROWSER_PROFILE` | Profile fallback browser; jangan commit |
| `MCP_HOST` / `MCP_PORT` | Bind MCP terpisah dari webapp; LaunchAgent memakai Tailscale IP dan port 8790 |
| `MCP_PUBLIC_URL` | URL `/mcp` yang disalin ke konfigurasi client; gunakan alamat Tailscale/HTTPS yang benar-benar dapat dijangkau client |
| `MCP_ALLOWED_NETWORKS` | Allowlist sumber MCP, default loopback dan Tailscale `100.64.0.0/10` |
| `MCP_TOKEN_FILE` | File token MCP mode 0600; token plaintext tidak boleh masuk Git atau log |
| `MCP_MAX_WAIT_SECONDS` | Batas tunggu tool MCP, maksimum 300 detik |
| `MCP_RATE_LIMIT_PER_MINUTE` | Batas request per token |

Salin template hanya jika `.env` belum ada:

```sh
cp .env.example .env
```

Pada mesin ini LaunchAgent memakai database di luar volume project agar SQLite
tidak terganggu mount volume eksternal. Jangan menyalin path macOS tersebut ke
mesin lain tanpa menyesuaikannya.

## API lokal

Semua endpoint job dan bridge memerlukan session superadmin. `/api/health` tetap
memberi ringkasan aman tanpa path database. Login memakai:

```sh
curl -sS -c /tmp/fahra-session -H 'Content-Type: application/json' \
  -d '{"email":"...","password":"..."}' \
  http://127.0.0.1:8788/api/auth/login
```

Jangan menyimpan credential nyata di shell history atau dokumentasi. Untuk membuat
user awal, gunakan bootstrap sekali lalu ganti password dari UI:

```sh
SUPERADMIN_EMAIL="..." SUPERADMIN_INITIAL_PASSWORD="..." \
  AFFILIATE_DB="/path/ke/affiliate.sqlite3" python3 app.py --bootstrap-admin
```

Endpoint auth tambahan: `GET /api/auth/me`, `POST /api/auth/logout`, dan
`POST /api/auth/change-password` dengan body `current_password`, `new_password`,
dan `new_password_confirmation`. Password baru minimal 12 karakter. Endpoint ini
tersedia untuk edit password kapan saja dari dashboard; saat password bootstrap
belum diganti, panelnya otomatis terbuka dan job dikunci.

Membuat job:

```sh
curl -sS -b /tmp/fahra-session -X POST http://127.0.0.1:8788/api/jobs \
  -H 'Content-Type: application/json' \
  --data '{"account_id":"<id dari GET /api/accounts>","links":["https://shopee.co.id/..."],"tags":["konten"]}'
```

Response sukses hanya berarti job masuk queue:

```json
{"id":"<job-id>","status":"queued","account_id":"<id akun>","reused":false}
```

Periksa hasil sebenarnya:

```sh
curl -sS -b /tmp/fahra-session http://127.0.0.1:8788/api/jobs
curl -sS -b /tmp/fahra-session http://127.0.0.1:8788/api/jobs/<job-id>
```

Job selesai harus memiliki `status: "completed"` dan `result[].shortLink`.
`needs_session` berarti sesi atau tab perlu diperbaiki lalu job dikirim ulang.

## Extension bridge

1. Load unpacked folder `extension/` dari `chrome://extensions`.
2. Gunakan satu profil Chrome per akun. Login dashboard lokal pada setiap profil, klik ikon extension, lalu simpan nama akun yang sesuai dengan login Shopee. Buka satu tab `https://affiliate.shopee.co.id/offer/custom_link` yang sudah login per profil.
3. Setelah perubahan file extension, tekan **Reload** pada kartu extension.
4. Reload tab Custom Link agar content script terbaru terpasang.
5. Pastikan `/api/health` menunjukkan `bridge.connected: true` dan UI menampilkan
   `Bridge aktif`.

Extension background mengambil job dari `POST /api/bridge/next`, mengoperasikannya pada
halaman Shopee, lalu mengirim hasil ke `/api/bridge/result`. `content.js` mengisi
URL dan tag, menekan `Buat Link`, lalu membaca short link dari modal Shopee.

Endpoint bridge adalah internal untuk extension dan tidak boleh diekspos ke
internet tanpa autentikasi, origin policy, dan TLS yang dirancang dengan benar.

### Routing akun dan payload bridge v2

- `GET /api/accounts` (session superadmin) mengembalikan `accounts`, setiap item
  memiliki `id` (32 hex), `name`, `ready`, `busy`, dan `last_seen` (Unix timestamp
  atau null). ID dihasilkan per instalasi profil Chrome dan bukan ID Shopee.
- `POST /api/jobs` wajib menerima `account_id` yang terdaftar. Response `202`
  berisi `id`, `status`, `account_id`, `reused`. Detail/list job menyertakan
  `account_id` dan snapshot `account_name`. Nilainya null pada riwayat lama.
- Semua POST bridge memerlukan session aplikasi dan body `protocol_version: 2`,
  `account_id`. Extension lama serta GET next ditolak dengan HTTP 400.
- Ping: tambahkan `account_name` (1–80 karakter), `ready` (boolean), `url`.
  Response `{ "ok": true, "ready": true, "account_id": "<32 hex>" }`.
  URL siap harus persis halaman Custom Link. Lebih dari satu tab Custom Link
  dalam profil membuat bridge tidak siap. Heartbeat kedaluwarsa setelah 10 detik.
- Next: response `{ "job": null }` jika akun tidak cocok/tidak siap/tidak ada job;
  selain itu `{ "job": { "id": "...", "account_id": "...", "links": [],
  "tags": [] }, "lease_token": "<privat>" }`. Job hanya dikirim sekali per
  lease, bukan dikirim ulang setiap polling.
- Result: tambahkan `job_id`, `lease_token`, `results` (array short-link), `error`,
  `error_kind`. Response `{ "ok": true }`. Hasil akun lain, lease salah,
  hasil duplikat, atau hasil terlambat ditolak HTTP 400. Lease tidak boleh dicatat.
- `/api/health` menambahkan `bridge.protocol_version: 2` dan `ready_accounts`
  sebagai hitungan agregat tanpa mengekspos nama akun atau lease.

Worker masih serial. Job akun offline tetap queued dan tidak menghalangi akun
lain yang siap. Timeout job yang sudah diambil menjadi `needs_session`.
Restart mempertahankan target akun. Upgrade mengubah job lama tanpa akun yang
masih queued/running menjadi needs_session dengan pesan kirim ulang; riwayat
terminal tidak berubah. Pilih akun secara eksplisit untuk job penggantinya.

ID bridge disimpan di storage lokal extension dan nama akun diisi manual. Bridge
belum membaca identitas Shopee, jadi gunakan profil tetap per akun. Jangan clone
folder profil atau mengganti login Shopee ketika ada job. Reset storage extension
membuat ID baru; tidak ada pemindahan otomatis antrean dari ID lama.

Sesudah deployment 0.2.0, reload extension dan tab Shopee secara manual pada tiap
profil, isi nama melalui ikon extension, lalu cek daftar akun dashboard. Client
MCP harus memperbarui cache tools/list dan menyertakan account_id saat generate.
Idempotency key yang sama dengan akun/URL/tag berbeda ditolak, bukan mengembalikan
hasil akun lain.

## MCP OpenClaw

MCP memakai Streamable HTTP pada `/mcp` dengan Bearer token terpisah dari cookie
webapp. Tool yang diekspos hanya queue dan pembacaan hasil:

- `shopee_list_accounts`: daftar ID/nama akun dan kesiapan bridge, scope `jobs:read`.
- `shopee_generate_short_links`: wajib `account_id` dari daftar akun; menerima 1–5 URL dan maksimal 5 tag; mendukung
  `idempotency_key` dan `wait_seconds`.
- `shopee_get_job`: membaca satu job.
- `shopee_wait_for_job`: polling bounded sampai terminal atau timeout.
- `shopee_health`: status worker dan bridge tanpa path lokal.
- `shopee_list_recent_jobs`: riwayat terbatas maksimal 50 item.

MCP tidak mengekspos `/api/bridge/next`, `/api/bridge/result`, lease token, cookie,
request header Shopee, atau database path. Request diaudit sebagai metadata aman.
Token pertama dibuat otomatis pada `MCP_TOKEN_FILE`; untuk OpenClaw, sediakan
nilainya melalui environment `FAHRA_AFF_MCP_TOKEN` dan gunakan transport
`streamable-http` pada URL Tailscale. Jangan menaruh token di repository atau
pesan log.

Untuk agent tambahan, buat token terpisah dan salin nilai yang dicetak sekali
melalui kanal rahasia:

```sh
/opt/homebrew/bin/python3 app.py --create-mcp-token nama-agent
```

Database hanya menyimpan hash token. Token yang sudah terlanjur terekspos harus
dicabut sebelum membuat token pengganti:

```sh
/opt/homebrew/bin/python3 app.py --revoke-mcp-token nama-agent
```

### Manajemen token melalui dashboard

Setelah login dan mengganti password bootstrap, buka menu **MCP** pada sidebar dashboard.
Gunakan nama token yang menjelaskan client, misalnya `openclaw-mac`, `codex-laptop`,
`claude-desktop`, atau `hermes-agent`. Token plaintext hanya dikembalikan satu kali
saat dibuat; daftar token berikutnya hanya menampilkan nama, scope, waktu dibuat,
waktu terakhir dipakai, dan status. Simpan token pada environment atau secret manager
client, jangan di repository atau localStorage.

Panel menyediakan contoh konfigurasi untuk OpenClaw, Codex CLI, Claude Code, dan
Hermes Agent. Semua client memakai endpoint Streamable HTTP `/mcp` dan header
`Authorization: Bearer ...`. Tombol **Cabut akses** menonaktifkan token segera;
**Hapus** menghapus record token dan langsung mencabut aksesnya. Nama yang dihapus
dapat digunakan kembali untuk membuat token baru dari panel. Token default
`MCP_TOKEN_NAME` ditandai `deleted` sebagai tombstone agar daemon restart tidak
menghidupkan kembali token file lama; token non-default benar-benar dihapus.

Endpoint dashboard yang dipakai panel adalah `GET /api/mcp/tokens`, `POST
/api/mcp/tokens` dengan body `{ "name": "codex-laptop" }`, `POST
/api/mcp/tokens/<name>/revoke`, `DELETE /api/mcp/tokens/<name>`, dan `POST
/api/mcp/test` untuk memeriksa listener MCP tanpa mengembalikan token. Semua endpoint
memerlukan session superadmin yang sudah mengganti password bootstrap dan menolak
request lintas origin.

## Validasi source

```sh
python3 -m py_compile app.py
node --check extension/content.js
node --check extension/background.js
git diff --check
python3 -m unittest discover -s tests -v
node --test tests/test_extension.cjs
```

Periksa file yang akan masuk commit:

```sh
git status --short --ignored
git diff --cached --name-only
```

Credential harus terlihat sebagai ignored, bukan staged.

## GitHub workflow

```sh
git status --short --branch
git add AGENTS.md TOOLS.md PRD.md README.md CHANGELOG.md app.py extension/
git commit -m "docs: update project context"
git push origin main
```

Gunakan commit kecil yang menjelaskan perubahan perilaku. Repository GitHub
bersifat public; hanya source yang boleh dipush. Tetap perlakukan semua data lokal sebagai rahasia.
