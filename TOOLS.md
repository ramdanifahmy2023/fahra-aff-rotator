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
  --data '{"links":["https://shopee.co.id/..."],"tags":["konten"]}'
```

Response sukses hanya berarti job masuk queue:

```json
{"id":"<job-id>","status":"queued"}
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
2. Buka satu tab `https://affiliate.shopee.co.id/offer/custom_link` yang sudah login.
3. Setelah perubahan file extension, tekan **Reload** pada kartu extension.
4. Reload tab Custom Link agar content script terbaru terpasang.
5. Pastikan `/api/health` menunjukkan `bridge.connected: true` dan UI menampilkan
   `Bridge aktif`.

Extension background mengambil job dari `/api/bridge/next`, mengoperasikannya pada
halaman Shopee, lalu mengirim hasil ke `/api/bridge/result`. `content.js` mengisi
URL dan tag, menekan `Buat Link`, lalu membaca short link dari modal Shopee.

Endpoint bridge adalah internal untuk extension dan tidak boleh diekspos ke
internet tanpa autentikasi, origin policy, dan TLS yang dirancang dengan benar.

## Validasi source

```sh
python3 -m py_compile app.py
node --check extension/content.js
node --check extension/background.js
git diff --check
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
bersifat private; tetap perlakukan semua data lokal sebagai rahasia.
