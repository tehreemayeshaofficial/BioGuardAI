"""
Authentication: the login and registration screens that gate the dashboard,
plus the request guard that keeps every other view behind a session.

Accounts are stored in the same SQLite database as the surveillance record
(see :mod:`bioguard.database`).  Passwords are hashed with Werkzeug's
``generate_password_hash``; the e-mail address doubles as the login handle, so
the registration form can stay short (no separate username) while the sign-in
form still asks for a "username" - either the e-mail or the stored handle works.
"""

from __future__ import annotations

from flask import (Blueprint, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .. import database, get_accounts_db, tenancy

bp = Blueprint("auth", __name__)

# Endpoints reachable without a session. Everything else is guarded.
PUBLIC_ENDPOINTS = {"auth.login", "auth.register", "auth.logout", "static"}


# --------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------
def current_user():
    """The signed-in user row (a ``sqlite3.Row``), cached on ``g``. ``None`` if anonymous."""
    if "user" not in g:
        user = None
        uid = session.get("uid")
        if uid is not None:
            user = database.get_user(get_accounts_db(), uid)
        g.user = user
    return g.user


def login_user(user) -> None:
    session.clear()
    session["uid"] = user["id"]


def logout_user() -> None:
    session.clear()
    g.pop("user", None)


def _safe_next(target):
    """Accept only same-site relative paths as a post-login redirect target."""
    if not target or target.startswith("//") or "://" in target:
        return None
    return target


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("main.dashboard"))

    error = hospital = identifier = ""
    if request.method == "POST":
        hospital = (request.form.get("hospital_name") or "").strip()
        identifier = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = database.find_user(get_accounts_db(), identifier) if identifier else None
        if not user or not check_password_hash(user["password_hash"], password):
            error = ("We couldn't sign you in. Check your username and password, "
                     "or create an account below.")
        elif hospital and user["hospital_name"].strip().lower() != hospital.lower():
            error = "That account isn't registered to this hospital."
        else:
            login_user(user)
            flash(f"Welcome back, {user['hospital_name']}.", "success")
            return redirect(_safe_next(request.args.get("next"))
                            or url_for("main.dashboard"))

    return render_template("login.html", error=error, hospital_name=hospital,
                           username=identifier, page="login")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("main.dashboard"))

    form = {"hospital_name": "", "ico_officer": "", "email": "",
            "password": "", "confirm_password": ""}
    error = None
    if request.method == "POST":
        for key in form:
            form[key] = (request.form.get(key) or "").strip()
        hospital = form["hospital_name"]
        officer = form["ico_officer"]
        email = form["email"].lower()
        password = form["password"]
        confirm = form["confirm_password"]

        if not (hospital and email and password):
            error = "Hospital name, e-mail and password are all required."
        elif "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            error = "Enter a valid e-mail address."
        elif len(password) < 6:
            error = "Choose a password of at least 6 characters."
        elif password != confirm:
            error = "The two passwords don't match."
        elif database.username_taken(get_accounts_db(), email):
            error = "An account already exists for that e-mail - sign in instead."
        else:
            _hospital, uid = tenancy.provision_workspace(
                get_accounts_db(), hospital_name=hospital, username=email,
                email=email, password_hash=generate_password_hash(password),
                ico_officer=officer)
            login_user(database.get_user(get_accounts_db(), uid))
            flash("Account created. Welcome to Bioguard AI.", "success")
            return redirect(url_for("main.dashboard"))

    return render_template("register.html", error=error, page="register", **form)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    logout_user()
    flash("You've been signed out.", "info")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------
def _wants_json() -> bool:
    return request.path.startswith("/api/") or \
        request.accept_mimetypes.best == "application/json"


def enforce_login():
    """``before_request`` hook: bounce anonymous traffic to the login screen."""
    endpoint = request.endpoint
    # Unmatched paths (404) and explicitly public endpoints pass straight through.
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return None
    if current_user() is not None:
        return None
    if session.get("uid"):
        # A session naming a user (or hospital) that no longer exists: drop the
        # stale id so the next request starts clean at the login screen.
        logout_user()
    if _wants_json():
        return jsonify({"error": "Authentication required.", "code": 401}), 401
    return redirect(url_for("auth.login", next=request.full_path))
