"""
Tests for password change (app.py): vlastná zmena hesla, vynútenie adminom, horné menu.
DB je falošná — malý slovník používateľov, SQL sa rozpoznáva podľa začiatku príkazu.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module


class FakeDB:
    def __init__(self, migrated: bool = True):
        self.migrated = migrated
        self.users = {
            1: {"id": 1, "username": "admin", "password_hash": generate_password_hash("admin-heslo"),
                "role": "admin", "must_change_password": 0, "password_changed_at": None, "created_at": "2026-01-01"},
            2: {"id": 2, "username": "user", "password_hash": generate_password_hash("user-heslo"),
                "role": "user", "must_change_password": 0, "password_changed_at": None, "created_at": "2026-01-01"},
        }

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        p = params or {}
        res = MagicMock()
        if "information_schema.COLUMNS" in sql:
            res.scalar.return_value = 1 if self.migrated else 0
            return res
        if sql.startswith("SELECT"):
            rows = list(self.users.values())
            if "WHERE id = :id" in sql:
                rows = [u for u in rows if u["id"] == p["id"]]
            if "WHERE username = :u" in sql:
                rows = [u for u in rows if u["username"] == p["u"]]
            cols = [c.strip() for c in re.search(r"SELECT (.*?) FROM", sql).group(1).split(",")]

            def value(u, c):
                if " AS " in c:
                    lit, name = c.split(" AS ")
                    return None if lit == "NULL" else int(lit)
                return 0 if c == "0" else u[c]
            names = [c.split(" AS ")[-1] for c in cols]
            tuples = [tuple(value(u, c) for c in cols) for u in rows]
            res.fetchone.return_value = tuples[0] if tuples else None
            res.mappings.return_value.fetchall.return_value = [dict(zip(names, t)) for t in tuples]
            return res
        if sql.startswith("UPDATE users"):
            u = self.users.get(p["id"])
            res.rowcount = 1 if u else 0
            if u:
                if "password_hash = :h" in sql:
                    u["password_hash"] = p["h"]
                if "must_change_password = :f" in sql:
                    u["must_change_password"] = p["f"]
                if "must_change_password = 1" in sql:
                    u["must_change_password"] = 1
                if "must_change_password = 0" in sql:
                    u["must_change_password"] = 0
                if "password_changed_at = NOW()" in sql:
                    u["password_changed_at"] = "teraz"
            return res
        raise AssertionError(f"neočakávané SQL: {sql}")


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    conn = MagicMock()
    conn.execute.side_effect = fake.execute
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.begin.return_value.__enter__.return_value = conn
    monkeypatch.setattr(app_module, "get_db_engine", lambda: engine)
    monkeypatch.setattr(app_module, "_password_columns_ready", False)
    monkeypatch.setitem(app_module.app.config, "SECRET_KEY", "test-secret")
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    return fake


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


class TestOwnPasswordChange:
    def test_change_ok(self, db):
        c = app_module.app.test_client()
        login(c, "user", "user-heslo")
        r = c.post("/account/password", data={"current_password": "user-heslo",
                                               "new_password": "nove-heslo-1", "confirm_password": "nove-heslo-1"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/")
        assert check_password_hash(db.users[2]["password_hash"], "nove-heslo-1")
        assert db.users[2]["password_changed_at"] == "teraz"

    @pytest.mark.parametrize("current,new,confirm", [
        ("zle-heslo", "nove-heslo-1", "nove-heslo-1"),     # zlé súčasné
        ("user-heslo", "kratke", "kratke"),                 # krátke
        ("user-heslo", "nove-heslo-1", "ine-heslo-1"),      # nezhoda
        ("user-heslo", "user-heslo", "user-heslo"),         # rovnaké ako súčasné
    ])
    def test_rejected(self, db, current, new, confirm):
        c = app_module.app.test_client()
        login(c, "user", "user-heslo")
        c.post("/account/password", data={"current_password": current, "new_password": new, "confirm_password": confirm})
        assert check_password_hash(db.users[2]["password_hash"], "user-heslo")

    def test_requires_login(self, db):
        r = app_module.app.test_client().get("/account/password")
        assert r.status_code == 302 and "/login" in r.headers["Location"]

    def test_without_migration(self, db):
        db.migrated = False
        c = app_module.app.test_client()
        login(c, "user", "user-heslo")
        c.post("/account/password", data={"current_password": "user-heslo",
                                           "new_password": "nove-heslo-1", "confirm_password": "nove-heslo-1"})
        assert check_password_hash(db.users[2]["password_hash"], "nove-heslo-1")
        assert db.users[2]["password_changed_at"] is None


class TestForcedChange:
    def test_admin_forces_and_user_is_redirected(self, db):
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        a.post("/user/2/force-password", data={"force": "1"})
        assert db.users[2]["must_change_password"] == 1

        u = app_module.app.test_client()
        login(u, "user", "user-heslo")
        r = u.get("/browse")
        assert r.status_code == 302 and r.headers["Location"].endswith("/account/password")
        assert u.get("/account/password").status_code == 200
        u.post("/account/password", data={"current_password": "user-heslo",
                                           "new_password": "nove-heslo-1", "confirm_password": "nove-heslo-1"})
        assert db.users[2]["must_change_password"] == 0

    def test_admin_can_unforce(self, db):
        db.users[2]["must_change_password"] = 1
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        a.post("/user/2/force-password", data={"force": "0"})
        assert db.users[2]["must_change_password"] == 0

    def test_reset_sets_temp_password_and_forces(self, db):
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        a.post("/user/2/reset-password", data={"password": "docasne-123"})
        assert check_password_hash(db.users[2]["password_hash"], "docasne-123")
        assert db.users[2]["must_change_password"] == 1

    def test_not_for_own_account_or_non_admin(self, db):
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        a.post("/user/1/force-password", data={"force": "1"})
        assert db.users[1]["must_change_password"] == 0
        u = app_module.app.test_client()
        login(u, "user", "user-heslo")
        assert u.post("/user/1/force-password", data={"force": "1"}).status_code == 403

    def test_force_without_migration(self, db):
        db.migrated = False
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        a.post("/user/2/force-password", data={"force": "1"})
        assert db.users[2]["must_change_password"] == 0


class TestTopMenu:
    def test_links_in_top_bar(self, db):
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        html = a.get("/account/password").get_data(as_text=True)
        top = html.split('class="adminbar"')[0]
        assert 'href="/account/password"' in top and 'href="/users"' in top
        assert 'href="/users"' not in html.split('class="adminbar"')[1]

    def test_users_link_only_for_admin(self, db):
        u = app_module.app.test_client()
        login(u, "user", "user-heslo")
        html = u.get("/account/password").get_data(as_text=True)
        assert 'href="/account/password"' in html and 'href="/users"' not in html


class TestUsersPage:
    @pytest.mark.parametrize("migrated", [True, False])
    def test_renders(self, db, migrated):
        db.migrated = migrated
        db.users[2]["must_change_password"] = 1
        a = app_module.app.test_client()
        login(a, "admin", "admin-heslo")
        html = a.get("/users").get_data(as_text=True)
        assert "/user/2/reset-password" in html
        assert ("musí zmeniť heslo" in html and "/user/2/force-password" in html) == migrated
        assert ("005_user_password_change.sql" in html) == (not migrated)
