"""
The active surveillance scope.

Every page and every API endpoint answers questions about *the same* filtered
dataset: a date range, an optional ward and an optional pathogen. The choice is
kept in the session so that moving between the dashboard, the trend page and the
AMR page does not silently reset what the user is looking at, while query
arguments still win so a filtered view can be shared as a link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from flask import g, has_request_context, request, session

from . import database
from .analysis.common import reference_date
from .pathogens import PATHOGENS, display_name

SESSION_KEY = "scope"


@dataclass
class Scope:
    """Filters applied to every query. Empty strings mean 'no filter'."""
    date_from: str = ""
    date_to: str = ""
    ward: str = ""
    pathogen: str = ""
    include_nontarget: bool = False
    window_days: int | None = None

    # ---- presentation ----------------------------------------------------
    @property
    def is_filtered(self) -> bool:
        return bool(self.date_from or self.date_to or self.ward
                    or self.pathogen or self.include_nontarget)

    @property
    def pathogen_display(self) -> str:
        return display_name(self.pathogen) if self.pathogen else ""

    def chips(self) -> list[dict]:
        """Active filters as removable badges for the UI."""
        out = []
        if self.date_from:
            out.append({"key": "date_from", "label": f"from {self.date_from}"})
        if self.date_to:
            out.append({"key": "date_to", "label": f"to {self.date_to}"})
        if self.ward:
            out.append({"key": "ward", "label": f"ward: {self.ward}"})
        if self.pathogen:
            out.append({"key": "pathogen", "label": f"bug: {self.pathogen_display}"})
        if self.include_nontarget:
            out.append({"key": "include_nontarget", "label": "incl. non-target"})
        return out

    def as_args(self, **extra) -> dict:
        args = {"date_from": self.date_from, "date_to": self.date_to,
                "ward": self.ward, "pathogen": self.pathogen,
                "include_nontarget": "1" if self.include_nontarget else ""}
        args.update({k: v for k, v in extra.items() if v not in (None, "")})
        return {k: v for k, v in args.items() if v != ""}

    def args_without(self, key: str) -> dict:
        """Query args with one filter explicitly emptied.

        ``as_args`` drops blank values, which is wrong for a "remove this
        filter" link: an *omitted* key falls back to the stored session scope,
        so the filter would silently survive the click. Emitting the key with an
        empty value is what actually clears it.
        """
        data = {"date_from": self.date_from, "date_to": self.date_to,
                "ward": self.ward, "pathogen": self.pathogen,
                "include_nontarget": "1" if self.include_nontarget else ""}
        data[key] = ""
        return data

    @property
    def label(self) -> str:
        bits = []
        if self.pathogen:
            bits.append(self.pathogen_display)
        if self.ward:
            bits.append(self.ward)
        if self.date_from or self.date_to:
            bits.append(f"{self.date_from or 'start'} to {self.date_to or 'now'}")
        return " · ".join(bits) if bits else "all data"


def _clean(raw) -> str:
    text = (str(raw) if raw is not None else "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _get(args, key: str):
    """Last value wins for a repeated key.

    HTML forms put a hidden ``0`` before a checkbox so that an *unchecked* box
    still reports something; query dicts and MultiDicts otherwise disagree.
    """
    getter = getattr(args, "getlist", None)
    values = getter(key) if callable(getter) else (
        [args[key]] if key in args else [])
    return values[-1] if values else ""


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_scope(args=None, store=True) -> Scope:
    """Merge any query arguments over the session scope, and remember the result."""
    args = args if args is not None else (request.args if has_request_context() else {})
    saved = dict(session.get(SESSION_KEY, {})) if store or has_request_context() else {}
    scope = Scope(
        date_from=saved.get("date_from", ""),
        date_to=saved.get("date_to", ""),
        ward=saved.get("ward", ""),
        pathogen=saved.get("pathogen", ""),
        include_nontarget=_truthy(saved.get("include_nontarget")),
    )
    if "clear" in args:
        scope = Scope()
        if store and has_request_context():
            session.pop(SESSION_KEY, None)
        return scope
    if "date_from" in args:
        scope.date_from = _clean(_get(args, "date_from"))
    if "date_to" in args:
        scope.date_to = _clean(_get(args, "date_to"))
    if "ward" in args:
        scope.ward = str(_get(args, "ward") or "").strip()
    if "pathogen" in args:
        pk = str(_get(args, "pathogen") or "").strip()
        scope.pathogen = pk if pk in PATHOGENS else ""
    if "include_nontarget" in args:
        scope.include_nontarget = _truthy(_get(args, "include_nontarget"))
    if store and has_request_context():
        session[SESSION_KEY] = {
            "date_from": scope.date_from, "date_to": scope.date_to,
            "ward": scope.ward, "pathogen": scope.pathogen,
            "include_nontarget": "1" if scope.include_nontarget else "",
        }
        session.modified = True
    return scope


def current_scope() -> Scope:
    """The scope for this request, cached in ``flask.g``."""
    if not has_request_context():
        return resolve_scope({}, store=False)
    if "scope" not in g:
        g.scope = resolve_scope()
    return g.scope


# --------------------------------------------------------------------------
# Dataset access
# --------------------------------------------------------------------------
def dataset(conn=None, scope: Scope | None = None, *, target_only=True):
    """Isolates for the current scope, cached once per request."""
    scope = scope or current_scope()
    key = ("dataset", target_only, scope.date_from, scope.date_to, scope.ward,
           scope.pathogen, scope.include_nontarget)
    cache = getattr(g, "_datasets", None) if has_request_context() else None
    if cache is not None and key in cache:
        return cache[key]

    own_conn = conn is None
    conn = conn or database.connect(_db_path())
    try:
        isolates = database.load_dataset(
            conn, target_only=target_only,
            date_from=scope.date_from, date_to=scope.date_to,
            ward=scope.ward, pathogen=scope.pathogen,
            include_suppressed=scope.include_nontarget)
    finally:
        # Inside a request the pool in `main` owns the connection; outside one
        # we opened it, so we must close it or a WAL file handle leaks.
        if own_conn:
            conn.close()

    if cache is not None:
        cache[key] = isolates
    return isolates


def dataset_cache_reset() -> None:
    """Drop the per-request memo so a fresh upload is visible immediately."""
    if has_request_context():
        g._datasets = {}


def as_of(isolates) -> date:
    """'Now' for the dataset: the newest isolate, never the wall clock."""
    return reference_date(isolates)


def _db_path() -> str:
    if has_request_context():
        from flask import current_app
        return current_app.config["DB_PATH"]
    import config
    return config.Config.DB_PATH
