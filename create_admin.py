#!/usr/bin/env python3
"""
Vytvorí alebo aktualizuje používateľský účet v DB.
Použitie:
    python create_admin.py                  # rola admin (default)
    python create_admin.py --role power
    python create_admin.py --role user
"""
import argparse
import getpass
import sys

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from config.config import get_db_engine, init_cli

VALID_ROLES = ("admin", "power", "user")


def main():
    init_cli("create_admin")

    p = argparse.ArgumentParser()
    p.add_argument("--role", choices=VALID_ROLES, default="admin",
                   help="Rola používateľa (default: admin)")
    args = p.parse_args()

    default_username = "admin" if args.role == "admin" else ""
    username = input(f"Username [{default_username}]: ").strip() or default_username
    if not username:
        print("Username nesmie byť prázdny.")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Heslo nesmie byť prázdne.")
        sys.exit(1)

    password_hash = generate_password_hash(password)
    engine = get_db_engine()

    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE username = :u"),
            {"u": username},
        ).fetchone()

        if existing:
            conn.execute(
                text("UPDATE users SET password_hash = :h, role = :r WHERE username = :u"),
                {"h": password_hash, "r": args.role, "u": username},
            )
            print(f"Používateľ '{username}' aktualizovaný (role={args.role}, nové heslo).")
        else:
            conn.execute(
                text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, :r)"),
                {"u": username, "h": password_hash, "r": args.role},
            )
            print(f"Používateľ '{username}' vytvorený (role={args.role}).")


if __name__ == "__main__":
    main()