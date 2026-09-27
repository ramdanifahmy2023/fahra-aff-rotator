# Changelog

## 2026-09-27

### Fixed

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

- Menambahkan ignore rules untuk `.env`, cookie, header sesi, database, profile browser,
  log runtime, cache Python, dan `node_modules`.
- Mengubah `.env.example` menjadi portable dengan path relatif; credential tetap hanya
  berada di mesin lokal.

### UI

- Memperkuat layout webapp pada viewport tablet dan mobile, termasuk wrapping URL/job,
  target sentuh minimum, grid satu kolom, dan fokus keyboard yang tetap terlihat.
