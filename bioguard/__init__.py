"""
Bioguard AI - pathogen surveillance and outbreak intelligence.

The application is assembled here: configuration, a request-scoped SQLite
connection, the ingestion service, template helpers and the two blueprints
(html pages and JSON APIs).  ``create_app`` is deliberately side-effect-light
apart from schema creation and the optional first-run demo seed, so the app can
be built inside tests without touching real uploads.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import (Flask, g, jsonify, render_template, request, url_for)

__version__ = "1.0.0"

from .database import (connect as _connect, init_accounts as _init_accounts,
                       init_db as _init_db)  # noqa: E402


# --------------------------------------------------------------------------
# Request-scoped connection
# --------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    """The current hospital's data connection, created and migrated on use.

    ``g.db_path`` is bound per request by :func:`_prime_request`; it is ``None``
    on public pages, which must never touch surveillance data.
    """
    db_path = getattr(g, "db_path", None)
    if not db_path:
        raise RuntimeError("no hospital workspace is bound to this request")
    if "db" not in g:
        conn = _connect(db_path)
        _init_db(conn)
        g.db = conn
    return g.db


def get_accounts_db() -> sqlite3.Connection:
    """The shared registry connection (hospitals + users) for this request."""
    if "accounts_db" not in g:
        from . import tenancy
        conn = _connect(tenancy.accounts_db_path())
        _init_accounts(conn)
        g.accounts_db = conn
    return g.accounts_db


def close_db(_exc=None) -> None:
    for attr in ("db", "accounts_db"):
        conn = g.pop(attr, None)
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt_date(value, with_time: bool = False) -> str:
    """ISO string or datetime -> '29 Aug 2026' (labs think in day-month)."""
    if not value:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return str(value)
    out = f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}"
    if with_time:
        try:
            parsed = value if isinstance(value, datetime) \
                else datetime.fromisoformat(str(value))
            if parsed.hour or parsed.minute:
                out += f" {parsed:%H:%M}"
        except (TypeError, ValueError):
            pass
    return out


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(value) -> str:
    try:
        return f"{float(value):,.0f}" if float(value) == int(float(value)) \
            else f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _risk_class(level: str) -> str:
    return {"Critical": "critical", "High": "high", "Medium": "medium",
            "Low": "low"}.get(level or "", "none")


def _sign(value, digits: int = 1) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{v:+.{digits}f}"


# Navigation is grouped into the three sections of the surveillance workflow.
# ``References & Guide`` is an anchor into a page that already carries that
# section (the Settings reference block) - it is not a separate route.
# ``Ward Heatmap`` has its own ``/ward-heatmap`` route (which reuses the same
# ``_ward_heatmap.html`` partial the Trends page embeds), so the sidebar can
# mark it active server-side via ``page='ward_heatmap'``.
NAV = [
    ("Main", [
        ("dashboard", "Dashboard", "/"),
        ("lab_reports", "Lab Reports", "/isolates"),
    ]),
    ("Surveillance", [
        ("trends", "Infection Trends", "/trends"),
        ("amr", "MDR Analysis", "/amr"),
        ("ward_heatmap", "Ward Heatmap", "/ward-heatmap"),
        ("outbreak", "Outbreak Prediction", "/outbreak"),
        ("alerts", "Alerts", "/alerts"),
    ]),
    ("Management", [
        ("reports", "Reports & Export", "/reports"),
        ("references", "References & Guide", "/settings#references"),
        ("settings", "Settings", "/settings"),
    ]),
]


def _context():
    """Values every template needs, computed at most once per request."""
    from .pathogens import PATHOGENS, display_name
    from .scope import current_scope

    scope = current_scope()
    if "filter_options" not in g:
        from . import database
        if getattr(g, "db_path", None):
            conn = get_db()
            wards = database.available_wards(conn)
            present = [k for k in database.available_pathogens(conn) if k in PATHOGENS]
        else:
            wards, present = [], []
        g.filter_options = {
            "wards": wards,
            "pathogens": [
                {"key": k, "name": display_name(k), "full": PATHOGENS[k].name}
                for k in present
            ],
            "all_pathogens": [
                {"key": k, "name": PATHOGENS[k].short_name, "full": PATHOGENS[k].name}
                for k in PATHOGENS
            ],
        }
    # Alert preferences (Settings -> Alerts & notifications). Both default to
    # ON so existing workspaces are unaffected until the ICO flips a switch.
    # Templates gate the in-app alert displays on ``notif_flags.in_app``; the
    # request pipeline empties the alert assembly itself when
    # ``notif_flags.generation`` is off (see ``main._apply_generation_flag``).
    if "notif_flags" not in g:
        from . import database
        if getattr(g, "db_path", None):
            _conn = get_db()
            g.notif_flags = {
                "in_app": database.get_meta(_conn, "notif_in_app", "1") != "0",
                "generation": database.get_meta(_conn, "notif_generation", "1") != "0",
            }
        else:
            g.notif_flags = {"in_app": True, "generation": True}
    import config as _config
    return {
        "app_name": _config.APP_NAME,
        "app_tagline": _config.APP_TAGLINE,
        "official_name": _config.OFFICIAL_NAME,
        "app_version": __version__,
        "nav": NAV,
        "scope": scope,
        "options": g.filter_options,
        "notif_flags": g.notif_flags,
        "risk_colors": _config.RISK_COLORS,
        "recent_window_days": _config.RECENT_WINDOW_DAYS,
        "cluster_window_days": _config.CLUSTER_WINDOW_DAYS,
        "risk_thresholds": {"High": _config.RISK_HIGH_THRESHOLD,
                            "Medium": _config.RISK_MEDIUM_THRESHOLD},
        "min_resistance_tests": _config.MIN_ISOLATES_FOR_RESISTANCE_RATE,
        "amr_alert_rate": round(_config.AMR_ALERT_RATE * 100),
        "active_path": request.path,
    }


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
def _prime_request():
    """Bind the request to the signed-in hospital's isolated workspace.

    This is the single tenancy routing point: it reads the session, resolves the
    owning hospital from the registry, and points ``g.db_path`` / ``g.upload_dir``
    at that hospital's files. Anonymous requests (login/register/static) leave
    both ``None`` and never touch surveillance data.
    """
    from flask import session
    from . import tenancy
    g._datasets = {}
    g.hospital = None
    g.db_path = None
    g.upload_dir = None
    uid = session.get("uid")
    if uid is not None:
        hospital = tenancy.hospital_for_user(get_accounts_db(), uid)
        if hospital is not None:
            tenancy.ensure_dirs(hospital["slug"])
            g.hospital = hospital
            g.db_path = str(tenancy.data_db_path(hospital["slug"]))
            g.upload_dir = str(tenancy.upload_dir(hospital["slug"]))


def create_app(test_config=None) -> Flask:
    import config as _config

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(_config.Config)
    app.config.update(
        DB_PATH=str(_config.DB_PATH),
        UPLOAD_DIR=str(_config.UPLOAD_DIR),
        ACCOUNTS_DB_PATH=str(_config.ACCOUNTS_DB_PATH),
        HOSPITALS_DIR=str(_config.HOSPITALS_DIR),
        BACKUPS_DIR=str(_config.BACKUPS_DIR),
        MAX_CONTENT_LENGTH=_config.Config.MAX_CONTENT_LENGTH,
    )
    app.config["ALLOWED_EXTENSIONS"] = _config.ALLOWED_EXTENSIONS
    if test_config:
        app.config.update(test_config)

    # Registry + workspace roots. Tenant data DBs are created lazily on first
    # use (get_db -> init_db); only the shared accounts DB is prepared eagerly.
    Path(app.config["ACCOUNTS_DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["HOSPITALS_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["BACKUPS_DIR"]).mkdir(parents=True, exist_ok=True)
    _prepare_registry(app)

    app.before_request(_prime_request)
    app.teardown_appcontext(close_db)
    app.context_processor(_context)
    app.jinja_env.filters.update(
        iso_date=_fmt_date, when=lambda v: _fmt_date(v, True), pct=_fmt_pct,
        num=_fmt_num, risk_class=_risk_class, signed=_sign)
    app.jinja_env.globals.update(url_for_scope=_url_for_scope)

    @app.template_filter("jsonenc")
    def _jsonenc(value):
        """Embed a payload in a ``<script>`` block without double-escaping."""
        import json
        from markupsafe import Markup
        return Markup(json.dumps(value, default=str).replace("</", "<\\/"))

    from .routes.main import bp as main_bp
    from .routes.api import bp as api_bp
    from .routes.auth import bp as auth_bp, current_user, enforce_login
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)

    # Make the signed-in account available to every template (sidebar, logout).
    app.context_processor(lambda: {"current_user": current_user()})
    # Registered after _prime_request so the connection/g.db_path is ready when
    # the guard looks the session user up. Runs before every view.
    app.before_request(enforce_login)

    _register_errors(app)
    _register_commands(app)

    # One-time, non-destructive accounts-only migration when a legacy
    # single-database install is detected and the registry is still empty.
    # No demo/synthetic data is ever seeded here or on first login.
    from . import migration
    migration.auto_migrate_if_needed(app)
    return app


def _prepare_registry(app) -> None:
    """Ensure the shared accounts database exists and is on the current schema."""
    conn = _connect(app.config["ACCOUNTS_DB_PATH"])
    try:
        _init_accounts(conn)
    except sqlite3.Error:
        app.logger.exception("could not initialise the accounts registry")
    finally:
        conn.close()


def _url_for_scope(endpoint, **extra):
    """url_for that carries the active scope forward, so filtering sticks."""
    from .scope import current_scope
    args = current_scope().as_args(**extra)
    return url_for(endpoint, **args)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
def _is_api() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == \
        "application/json"


def _register_errors(app: Flask) -> None:
    from werkzeug.exceptions import HTTPException

    def payload(code: int, message: str, detail: str = ""):
        body = {"error": message, "code": code}
        if detail:
            body["detail"] = detail
        return jsonify(body), code

    @app.errorhandler(404)
    def not_found(err):
        if _is_api():
            return payload(404, "No such endpoint.")
        return render_template("errors.html", code=404,
                               title="Page not found",
                               message="That view does not exist. It may have been "
                                       "renamed in a previous session."), 404

    @app.errorhandler(400)
    def bad_request(err):
        msg = getattr(err, "description", None) or "The request could not be understood."
        if _is_api():
            return payload(400, msg)
        return render_template("errors.html", code=400, title="Bad request",
                               message=msg), 400

    @app.errorhandler(413)
    def too_large(err):
        limit = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
        msg = f"That file is larger than the {limit} MB upload limit."
        if _is_api():
            return payload(413, msg)
        return render_template("errors.html", code=413, title="File too large",
                               message=msg), 413

    @app.errorhandler(500)
    def server_error(err):
        app.logger.exception("Unhandled error")
        if _is_api():
            return payload(500, "Internal error.", str(err)[:300])
        return render_template("errors.html", code=500, title="Something went wrong",
                               message=str(err) if app.debug else
                               "The server hit an unexpected error. The details were "
                               "written to the console."), 500

    @app.errorhandler(Exception)
    def unhandled(err):
        if isinstance(err, HTTPException):
            return err
        app.logger.exception("Unhandled error")
        if _is_api():
            return payload(500, "Internal error.", f"{type(err).__name__}: {err}"[:300])
        return render_template("errors.html", code=500, title="Something went wrong",
                               message=f"{type(err).__name__}: {err}"), 500


# --------------------------------------------------------------------------
# ``flask`` CLI
# --------------------------------------------------------------------------
def _register_commands(app: Flask) -> None:
    """``flask`` CLI. Every data command targets one hospital via --hospital."""
    import click
    from . import database, ingest, tenancy

    def _open_workspace(slug: str):
        """Return (accounts_conn, hospital_row, data_conn) for a hospital slug."""
        reg = _connect(app.config["ACCOUNTS_DB_PATH"])
        _init_accounts(reg)
        hospital = tenancy.get_hospital_by_slug(reg, slug)
        if hospital is None:
            reg.close()
            raise click.ClickException(
                f"No hospital with slug '{slug}'. List them with: flask hospitals")
        tenancy.ensure_dirs(hospital["slug"])
        data = _connect(tenancy.data_db_path(hospital["slug"]))
        _init_db(data)
        return reg, hospital, data

    @app.cli.command("hospitals")
    def _hospitals_cmd():
        """List every registered hospital and its slug."""
        reg = _connect(app.config["ACCOUNTS_DB_PATH"])
        _init_accounts(reg)
        try:
            rows = tenancy.list_hospitals(reg)
            if not rows:
                click.echo("No hospitals registered yet.")
            for h in rows:
                click.echo(f"{h['id']:>4}  {h['slug']:<28} {h['name']}")
        finally:
            reg.close()

    @app.cli.command("seed-demo")
    @click.option("--hospital", "slug", required=True, help="Target hospital slug.")
    def _seed_cmd(slug):
        """Load the synthetic dataset into one hospital's workspace."""
        reg, _hosp, data = _open_workspace(slug)
        try:
            click.echo(ingest.seed_demo(data, force=True)["message"])
        finally:
            data.close()
            reg.close()

    @app.cli.command("reset-db")
    @click.option("--hospital", "slug", required=True, help="Target hospital slug.")
    @click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
    def _reset_cmd(slug, yes):
        """Delete every report, isolate and uploaded file in one workspace."""
        if not yes and not click.confirm(f"Delete ALL data for '{slug}'?"):
            raise click.Abort()
        reg, _hosp, data = _open_workspace(slug)
        try:
            ingest.reset_all(data, tenancy.upload_dir(slug))
            click.echo(f"Workspace '{slug}' cleared.")
        finally:
            data.close()
            reg.close()

    @app.cli.command("stats")
    @click.option("--hospital", "slug", required=True, help="Target hospital slug.")
    def _stats_cmd(slug):
        """Print row counts for one hospital's workspace."""
        reg, _hosp, data = _open_workspace(slug)
        try:
            for k, v in database.database_counts(data).items():
                click.echo(f"{k:>15}: {v}")
        finally:
            data.close()
            reg.close()

    @app.cli.command("migrate-multiuser")
    @click.option("--dry-run", is_flag=True,
                  help="Report what would happen without writing anything.")
    def _migrate_cmd(dry_run):
        """Provision per-hospital workspaces from a legacy single DB."""
        from . import migration
        report = migration.run(app, dry_run=dry_run)
        for line in report["lines"]:
            click.echo(line)
