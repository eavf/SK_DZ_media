#!/usr/bin/env python3
"""
setup_db.py — Jednorazová inicializácia databázy pre DZ News Monitor.

Čo robí:
  1. Vytvorí databázu a všetky tabuľky (migrations/000_init_schema.sql)
  2. Interaktívne vytvorí prvého admin používateľa

Použitie:
    python setup_db.py

Vyžaduje nakonfigurované .env (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS).
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env() -> dict:
    env_file = ROOT / ".env"
    env_local = ROOT / ".env.local"
    result = {}
    for f in [env_file, env_local]:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _get_root_engine(env: dict):
    import sqlalchemy as sa
    host = env.get("DB_HOST", "127.0.0.1")
    port = env.get("DB_PORT", "3306")
    user = env.get("DB_USER", "root")
    password = env.get("DB_PASS", "")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/?charset=utf8mb4"
    return sa.create_engine(url, echo=False)


def _get_db_engine(env: dict):
    import sqlalchemy as sa
    host = env.get("DB_HOST", "127.0.0.1")
    port = env.get("DB_PORT", "3306")
    user = env.get("DB_USER", "root")
    password = env.get("DB_PASS", "")
    db = env.get("DB_NAME", "dz_news")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"
    return sa.create_engine(url, echo=False)


def step_create_schema(env: dict) -> None:
    print("\n[1/2] Vytváram databázu a tabuľky...")
    sql_file = ROOT / "migrations" / "000_init_schema.sql"
    if not sql_file.exists():
        print(f"  CHYBA: {sql_file} neexistuje.")
        sys.exit(1)

    from sqlalchemy import text
    sql = sql_file.read_text(encoding="utf-8")

    # Rozdelíme SQL na jednotlivé príkazy (ignorujeme komentáre a prázdne riadky)
    statements = []
    current: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip().rstrip(";")
            if stmt:
                statements.append(stmt)
            current = []

    engine = _get_root_engine(env)
    with engine.begin() as conn:
        # MySQL C extension niekedy blokuje multi-statement — spúšťame po jednom
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                # USE dz_news môže zlyhať ak driver mení DB kontext inak
                if "USE" in stmt.upper():
                    continue
                print(f"  VAROVANIE: {e}")

    print("  Databáza a tabuľky sú pripravené.")


def step_create_admin(env: dict) -> None:
    print("\n[2/2] Vytvorenie prvého admin používateľa...")

    from sqlalchemy import text
    from werkzeug.security import generate_password_hash

    engine = _get_db_engine(env)
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()

    if count and count > 0:
        ans = input(f"  V DB už existuje {count} používateľ(ov). Pridať ďalšieho? [a/N]: ").strip().lower()
        if ans != "a":
            print("  Preskakujem vytváranie používateľa.")
            return

    username = input("  Username [admin]: ").strip() or "admin"
    password = getpass.getpass("  Password: ")
    if not password:
        print("  Heslo nesmie byť prázdne. Používateľ nevytvorený.")
        return
    confirm = getpass.getpass("  Potvrď heslo: ")
    if password != confirm:
        print("  Heslá sa nezhodujú. Používateľ nevytvorený.")
        return

    password_hash = generate_password_hash(password)
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).fetchone()
        if existing:
            conn.execute(
                text("UPDATE users SET password_hash = :h, role = 'admin' WHERE username = :u"),
                {"h": password_hash, "u": username},
            )
            print(f"  Používateľ '{username}' aktualizovaný (role=admin).")
        else:
            conn.execute(
                text("INSERT INTO users (username, password_hash, role) VALUES (:u, :h, 'admin')"),
                {"u": username, "h": password_hash},
            )
            print(f"  Používateľ '{username}' vytvorený (role=admin).")


def main() -> None:
    print("=== DZ News Monitor – inicializácia databázy ===")

    env = _load_env()
    if not env.get("DB_NAME"):
        print("CHYBA: DB_NAME nie je nastavené v .env")
        sys.exit(1)

    print(f"  DB: {env.get('DB_USER')}@{env.get('DB_HOST', '127.0.0.1')}:{env.get('DB_PORT', '3306')}/{env.get('DB_NAME')}")
    ans = input("  Pokračovať? [A/n]: ").strip().lower()
    if ans == "n":
        sys.exit(0)

    step_create_schema(env)
    step_create_admin(env)

    print("\nHotovo. Spusti aplikáciu:")
    print("  python app.py")
    print("  alebo: docker compose up --build")


if __name__ == "__main__":
    main()