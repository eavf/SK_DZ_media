#!/usr/bin/env python3
"""
Vytvorí alebo aktualizuje admin účet v DB.
Použitie:
    python create_admin.py
"""
import getpass
import sys

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from config.config import get_db_engine, init_cli


def main():
    init_cli("create_admin")

    username = input("Username [admin]: ").strip() or "admin"
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
                text("UPDATE users SET password_hash = :h, role = 'admin' WHERE username = :u"),
                {"h": password_hash, "u": username},
            )
            print(f"Používateľ '{username}' aktualizovaný (role=admin, nové heslo).")
        else:
            conn.execute(
                text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, 'admin')"),
                {"u": username, "h": password_hash},
            )
            print(f"Admin '{username}' vytvorený.")


if __name__ == "__main__":
    main()