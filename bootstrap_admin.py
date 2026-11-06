"""Create the first local admin without credentials in files or shell history."""

import getpass
import os

import app


def main():
    app.init_db()
    with app.db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role='superadmin' LIMIT 1").fetchone():
            raise SystemExit("Superadmin sudah ada. Ganti password melalui dashboard.")

    email = input(f"Email superadmin [{app.AUTH_EMAIL}]: ").strip() or app.AUTH_EMAIL
    if "@" not in email:
        raise SystemExit("Email tidak valid.")
    password = getpass.getpass("Password awal (minimal 12 karakter): ")
    try:
        app.validate_password(password)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if password != getpass.getpass("Ulangi password awal: "):
        raise SystemExit("Konfirmasi password tidak cocok. Jalankan bootstrap kembali.")

    os.environ["SUPERADMIN_EMAIL"] = email
    os.environ["SUPERADMIN_INITIAL_PASSWORD"] = password
    try:
        app.bootstrap_admin()
    finally:
        os.environ.pop("SUPERADMIN_INITIAL_PASSWORD", None)
    print(f"Login di http://127.0.0.1:{app.PORT} lalu ganti password di Pengaturan.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\nBootstrap dibatalkan. Jalankan kembali saat siap.") from None
