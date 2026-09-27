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

Pada macOS worker dijalankan mandiri oleh LaunchAgent `com.fahra.aff-rotator`, sehingga tidak perlu menjalankan `./start.sh` setiap kali. Jika perintah itu dijalankan saat service sudah aktif, script hanya menampilkan alamat service lalu selesai.

Status service dapat diperiksa dengan:

```sh
launchctl print gui/$(id -u)/com.fahra.aff-rotator
curl http://127.0.0.1:8788/api/health
```

## Extension bridge

1. Buka Chrome dengan akun Affiliate Shopee yang sudah login.
2. Buka tab `https://affiliate.shopee.co.id/offer/custom_link` dan biarkan tab itu terbuka.
3. Masuk ke `chrome://extensions`, aktifkan Developer mode, pilih Load unpacked, lalu pilih folder `extension/`.
4. Kembali ke tab Shopee lalu reload sekali agar content script extension terpasang.
5. Jika `.env` belum ada, buat dari template: `cp .env.example .env` (template sudah memakai `SHOPEE_TRANSPORT=extension`).
6. Jalankan `./start.sh`.

Extension hanya bekerja pada tab Affiliate yang sudah ada. Ia tidak membuka browser baru, tidak membaca cookie, dan tidak mengirim token sesi ke worker. Jika tab belum siap atau verifikasi muncul, antrean menunggu dan job akan diberi status `needs_session` bila batas waktunya lewat.

Gunakan satu tab `Link Khusus (Custom Link)` saja. Setelah mengubah file extension, tekan **Reload** pada kartu extension di `chrome://extensions`, lalu reload tab Shopee tersebut sekali agar content script versi terbaru terpasang.

`Credential/cookies.txt` dibaca sebagai Cookie header oleh browser worker dan tidak pernah dikirim ke browser webapp. File cookie diberi mode 0600. Worker tidak lagi mengekspor header anti-bot ke startup.

Fallback `SHOPEE_TRANSPORT=browser` masih tersedia untuk profile browser khusus yang persisten. Jalur utama extension lebih aman untuk sesi karena tidak membuat browser atau profile baru.

Jika job berstatus `needs_session`, selesaikan login/verifikasi pada jendela worker lalu kirim job baru. Mode HTTP mentah tetap tersedia dengan `SHOPEE_TRANSPORT=http` untuk diagnosis saja dan dapat ditolak Shopee dengan `90309999`.

Payload `batchCustomLink` dari rekaman MCP dipakai sebagai referensi schema dan diagnosis. Pembuatan link tetap dilakukan melalui halaman Affiliate yang dibuka pada browser dengan sesi cookie pengguna.

## API lokal

- `POST /api/jobs` dengan `{ "links": ["https://shopee.co.id/..."], "tags": ["konten"] }`
- `GET /api/jobs` untuk job terbaru
- `GET /api/jobs/<id>` untuk hasil lengkap
- `GET /api/health` untuk status worker

Worker memvalidasi domain, membatasi batch menjadi lima URL dan lima tag, melakukan retry terbatas untuk error jaringan, dan tidak mencatat nilai cookie atau header sensitif ke log.

## Pengembangan dan perubahan

Setiap perubahan fungsional dicatat di `CHANGELOG.md` dan commit Git harus menjelaskan
perilaku yang diperbaiki. Validasi dasar sebelum push:

```sh
python3 -m py_compile app.py
node --check extension/content.js
node --check extension/background.js
```
