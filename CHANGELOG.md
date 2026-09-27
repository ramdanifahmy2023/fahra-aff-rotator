# Changelog

## 2026-09-27

### Fixed

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
