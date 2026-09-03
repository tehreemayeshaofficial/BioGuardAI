"""
Multi-tenant workspaces.

Each hospital owns a physically separate SQLite database and uploads folder::

    instance/
      accounts.db                 # registry: hospitals + users (auth, tenancy)
      hospitals/<slug>/bioguard.db  # that hospital's surveillance data
      hospitals/<slug>/uploads/     # that hospital's original uploaded files

Isolation is by construction, not by a ``hospital_id`` WHERE clause: the routing
layer (:func:`bioguard._prime_request`) resolves the signed-in user's hospital
once per request and points ``g.db_path`` / ``g.upload_dir`` at that workspace.
Every ``get_db()``-backed read and write therefore lands inside a single
hospital's file; another hospital's rows are in a different file and simply do
not exist from the current request's point of view.

This module is the only place that knows the on-disk layout, so it stays small
and auditable.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from flask import current_app, has_app_context

from . import database


# --------------------------------------------------------------------------
# Path resolvers (read config, or the live app config when in a request)
# --------------------------------------------------------------------------
def _accounts_db() -> Path:
    if has_app_context():
        return Path(current_app.config["ACCOUNTS_DB_PATH"])
    import config
    return Path(config.ACCOUNTS_DB_PATH)


def _base() -> Path:
    if has_app_context():
        return Path(current_app.config["HOSPITALS_DIR"])
    import config
    return Path(config.HOSPITALS_DIR)


def accounts_db_path() -> Path:
    return _accounts_db()


def hospital_dir(slug: str) -> Path:
    return _base() / slug


def data_db_path(slug: str) -> Path:
    return hospital_dir(slug) / "bioguard.db"


def upload_dir(slug: str) -> Path:
    return hospital_dir(slug) / "uploads"


def current_upload_dir() -> str:
    """The active workspace's uploads dir; falls back to the legacy config.

    Inside a request this is the tenant dir bound by ``_prime_request``. Outside
    one (CLI) it is unused by tenant commands, which pass an explicit path, so
    the fallback only keeps stray callers from crashing.
    """
    if has_app_context():
        from flask import g
        bound = getattr(g, "upload_dir", None)
        if bound:
            return str(bound)
        return str(current_app.config["UPLOAD_DIR"])
    import config
    return str(config.UPLOAD_DIR)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Slugs
# --------------------------------------------------------------------------
def slugify(name: str) -> str:
    """ASCII, lowercase, hyphen-separated identifier safe as a directory name."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "hospital"


def unique_slug(conn, name: str) -> str:
    """A directory-unique slug; colliding names get ``-2``, ``-3`` ... suffixes."""
    base = slugify(name)
    taken = {r["slug"] for r in conn.execute("SELECT slug FROM hospitals")}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


# --------------------------------------------------------------------------
# On-disk provisioning
# --------------------------------------------------------------------------
def ensure_dirs(slug: str) -> None:
    """Create a workspace's directory and (empty) uploads folder.

    The data database itself is created lazily by :func:`bioguard.get_db`, which
    connects to ``g.db_path`` and runs ``init_db`` - so a brand-new hospital has
    a valid, empty schema the first time any view reads it. Nothing here seeds
    data.
    """
    hospital_dir(slug).mkdir(parents=True, exist_ok=True)
    upload_dir(slug).mkdir(parents=True, exist_ok=True)


def get_hospital(conn, hospital_id: int):
    return conn.execute(
        "SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()


def get_hospital_by_slug(conn, slug: str):
    return conn.execute(
        "SELECT * FROM hospitals WHERE slug=?", (slug,)).fetchone()


def hospital_for_user(conn, uid: int):
    """The hospital row a user account belongs to, or ``None``."""
    return conn.execute(
        """SELECT h.* FROM hospitals h JOIN users u ON u.hospital_id = h.id
           WHERE u.id=?""", (uid,)).fetchone()


def create_hospital(conn, name: str):
    """Insert a hospital row and create its empty workspace on disk."""
    slug = unique_slug(conn, name)
    cur = conn.execute(
        "INSERT INTO hospitals (name, slug, created_at, status) VALUES (?,?,?,?)",
        ((name or "").strip(), slug, _now(), "active"))
    conn.commit()
    ensure_dirs(slug)
    return get_hospital(conn, int(cur.lastrowid))


def provision_workspace(conn, *, hospital_name: str, username: str,
                        password_hash: str, email: str = "",
                        ico_officer: str = ""):
    """Create a hospital + its owning account and an EMPTY workspace.

    Called on registration and by the migration. It never seeds any demo or
    synthetic data: a new hospital starts on a blank dataset. Returns
    ``(hospital_row, user_id)``.
    """
    hospital = create_hospital(conn, hospital_name)
    uid = database.create_user(
        conn, username=username, password_hash=password_hash,
        hospital_id=hospital["id"], ico_officer=ico_officer, email=email)
    # Materialise the tenant's EMPTY data database now so the workspace is
    # complete on disk (schema only - no rows, no demo/synthetic data). It would
    # also be created lazily by get_db(), but provisioning it eagerly keeps the
    # registration/migration state explicit and verifiable.
    data = database.connect(data_db_path(hospital["slug"]))
    try:
        database.init_db(data)
    finally:
        data.close()
    return hospital, uid


def list_hospitals(conn):
    return conn.execute("SELECT * FROM hospitals ORDER BY id").fetchall()


def count_hospitals(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM hospitals").fetchone()["c"])
