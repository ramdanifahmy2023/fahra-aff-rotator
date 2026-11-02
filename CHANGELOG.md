# Changelog

## 2026-11-02

### Added

- Bridge 0.2.0 dengan ID tetap per profil Chrome, pengaturan nama akun melalui
  ikon extension, daftar kesiapan akun, dan pilihan akun pada dashboard.
- Routing job UI/API/MCP melalui account_id wajib; riwayat menyimpan ID dan nama
  akun saat job dibuat. Menambahkan GET /api/accounts dan shopee_list_accounts.
- Migrasi SQLite mempertahankan riwayat dan target akun saat restart. Antrean
  lama tanpa akun menjadi needs_session agar operator memilih akun saat kirim ulang.
- Tes regresi dengan database sementara untuk simulasi 10 akun, isolasi lease,
  expiry, restart, migrasi, idempotency, HTTP auth, MCP, dan polling extension.

### Fixed

- Job hanya dikirim sekali per lease ke bridge akun yang sesuai; hasil lintas
  akun, duplikat, dan lease kedaluwarsa ditolak. Akun offline tidak menghalangi
  antrean akun yang siap, dengan worker tetap serial.
- Mengunci polling sejak awal untuk mencegah klaim ganda dan mempertahankan
  heartbeat saat job berjalan. Tab Custom Link ganda dalam satu profil ditolak.
- Recovery hasil hanya memakai hasil sukses dengan ID job yang sama, sehingga
  modal lama tidak dibaca sebagai hasil job baru. Content script lama meminta reload.
- Completed memerlukan jumlah hasil yang sesuai dan short link Shopee valid.
  Idempotency key tidak dapat mengembalikan hasil akun atau input berbeda.

### Deployment

- Reload extension dan tab Custom Link secara manual pada setiap profil, login
  dashboard lokal, lalu isi nama akun lewat ikon extension. Client MCP perlu
  memuat ulang tools/list dan menyertakan account_id. Nama akun masih label manual;
  gunakan satu profil tetap per akun Shopee. Eksekusi paralel belum tersedia.

## 2026-09-27

### Added

- Menambahkan panel manajemen akses MCP pada dashboard untuk membuat token per
  client, menampilkan plaintext satu kali, menyalin konfigurasi OpenClaw/Codex/
  Claude/Hermes, serta mencabut atau menghapus token tanpa menyimpan secret di
  frontend atau SQLite.
- Menambahkan MCP Streamable HTTP terpisah pada port 8790 untuk OpenClaw dan agent
  lain, dengan token Bearer runtime, scope queue/read/health, rate limit, audit
  metadata, idempotency key, dan tool bounded wait.
- Menjalankan MCP bersama worker melalui LaunchAgent dan mengikat endpoint remote
  ke alamat Tailscale privat; cookie Shopee dan endpoint bridge tetap internal.

### Fixed

- Menambahkan ikon extension Fahra Affiliate Bridge dengan simbol chain-link
  beraksen oranye yang selaras dengan tema webapp, lengkap untuk ukuran Chrome 16px
  sampai 128px.
- Memperbaiki parser hasil batch extension agar beberapa short link yang berada
  dalam satu textarea modal Shopee diekstrak satu per satu, sehingga batch 3 URL
  tidak lagi menunggu sampai timeout 60 detik.
- Membatasi riwayat job menjadi lima item per halaman dengan navigasi responsif,
  serta menambahkan salin otomatis untuk short link hasil dengan notifikasi status.
- Memadatkan URL sumber pada kartu `Job terakhir` menjadi label path maksimal dua
  baris, sambil mempertahankan URL lengkap sebagai link yang dapat dibuka agar daftar
  job tidak melebar atau membengkak di viewport sempit.
- Memperbaiki layout responsif webapp dengan breakpoint berbasis lebar konten,
  susunan satu kolom di layar sempit, ukuran kontrol minimum 44px, dan pembungkusan
  URL atau ID job panjang agar tidak membuat scroll horizontal.
- Menambahkan commit dan rollback eksplisit pada wrapper SQLite agar job yang masuk
  antrean tidak hilang saat koneksi ditutup.
- Memindahkan database runtime LaunchAgent ke lokasi lokal yang stabil dan memulihkan
  job `running` menjadi `queued` ketika daemon dimulai ulang.
- Menutup koneksi SQLite pada setiap request untuk mencegah kebocoran file descriptor.
- Memperkuat extension bridge agar memilih satu tab Custom Link yang sudah login,
  membaca hasil short link dari `input` maupun `textarea`, dan mengenali modal hasil.
- Menambahkan recovery polling ketika channel pesan Chrome terputus saat Shopee
  merender ulang halaman.
- Menambahkan validasi sintaks Python dan JavaScript pada alur pengembangan.

### Security

- Menambahkan autentikasi superadmin berbasis session cookie HttpOnly, hash password
  scrypt, rate limit login, bootstrap admin satu kali, dan proteksi API job/bridge.
- Menghapus kebocoran path database dari endpoint health dan mengganti CORS wildcard
  bridge dengan origin extension yang direfleksikan bersama credential.
- Memperketat validasi hostname agar domain mirip `shopee.co.id` tidak lolos.

- Menambahkan ignore rules untuk `.env`, cookie, header sesi, database, profile browser,
  log runtime, cache Python, dan `node_modules`.
- Mengubah `.env.example` menjadi portable dengan path relatif; credential tetap hanya
  berada di mesin lokal.

### UI

- Menambahkan favicon web berbasis simbol chain-link dengan palet dark warm,
  krem, oranye, dan coral yang konsisten dengan sidebar serta extension.
- Menambahkan blok Hasil terbaru di Dashboard yang menampilkan lima job terakhir beserta short link siap salin; halaman Riwayat tetap menjadi arsip lengkap.
- Memperbaiki mode sidebar ikon agar label menu Dashboard, MCP, Pengaturan, dan Riwayat benar-benar tersembunyi secara visual saat sidebar diperkecil, sementara nama aksesibilitas tetap tersedia.
- Memperbaiki sidebar yang dapat terbuka dalam mode ikon akibat preferensi lama; state collapse kini memakai kunci versi baru dan dashboard kembali expanded pada migrasi pertama.
- Memperbaiki badge kosong pada menu Riwayat agar benar-benar tersembunyi ketika tidak ada job aktif.
- Menambahkan badge jumlah job `queued` dan `running` pada menu Riwayat agar antrean aktif terlihat tanpa membuka halaman.
- Menambahkan tombol Tes koneksi pada halaman MCP yang memeriksa listener dan respons endpoint melalui server lokal tanpa menampilkan token.
- Menambahkan mode sidebar desktop yang dapat diperkecil menjadi ikon dan menyimpan pilihan pengguna di browser.
- Menambahkan filter riwayat berdasarkan status serta rentang tanggal pembuatan, lengkap dengan ringkasan hasil dan reset filter.
- Menata dashboard menjadi sidebar minimalis yang sticky di desktop dan berubah menjadi navigasi ringkas yang dapat digeser di layar kecil; Dashboard, MCP, Pengaturan, Riwayat, dan Keluar kini memiliki ruang yang jelas.
- Memindahkan password superadmin dan tema ke halaman Pengaturan, memisahkan riwayat ke halaman Riwayat, serta menambahkan navigasi berbasis hash dengan state aktif yang tetap terbaca pembaca layar.
- Memperkuat layout webapp pada viewport tablet dan mobile, termasuk wrapping URL/job,
  target sentuh minimum, grid satu kolom, dan fokus keyboard yang tetap terlihat.
- Menambahkan halaman login superadmin, panel wajib ganti password awal, logout, dan
  pemilih tema Sistem/Terang/Gelap yang tersimpan di browser.
- Menambahkan tombol Edit password di dashboard dengan konfirmasi password baru;
  panel tetap tersedia setelah bootstrap selesai dan session lain dicabut saat
  password berhasil diubah.
- Merapikan toolbar dashboard menjadi kontrol icon yang tetap accessible melalui
  `aria-label` dan tooltip, memperbaiki kontras header pada tema terang, serta
  memastikan toolbar dan kartu tetap rapih di viewport mobile.
- Memperbaiki tema Sistem agar mengikuti perubahan mode OS, menormalisasi nilai tema
  yang rusak di localStorage, dan menghindari flash gelap saat tema terang dipilih.
- Mengunci form job sampai status autentikasi selesai dan membersihkan field password
  ketika panel edit ditutup.
