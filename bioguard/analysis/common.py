"""Shared helpers for the analysis engines."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import config
from ..pathogens import PATHOGENS


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def contains(self, d: date | None) -> bool:
        return d is not None and self.start <= d <= self.end

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    def __str__(self) -> str:
        return self.label


def reference_date(isolates, fallback: date | None = None) -> date:
    """Anchor 'now' to the newest *plausible* isolate date in the dataset.

    Using wall-clock time would silently report zero recent activity for a
    laboratory export that was produced last month, so the data itself defines
    the observation point. A mis-keyed or otherwise impossible record dated
    after today is ignored when choosing that point, because letting it win
    would drag the anchor forward and push the newest *real* cluster outside
    every recent/prior window at once. When no record lies in the future this
    returns exactly what it always did (the newest isolate date).
    """
    today = date.today()
    days = [i.day for i in isolates if i.day]
    observed = [d for d in days if d <= today]
    if observed:
        return max(observed)
    if days:
        # Every record is future-dated: fall back to the wall clock rather than
        # let impossible data define the observation point.
        return today
    return fallback or today


def event_signature(iso) -> tuple:
    """The finest laboratory-event identity the persisted data can represent.

    This mirrors the ``isolates`` UNIQUE key minus ``report_id``: within a single
    report the schema already stores only one row per (patient, organism,
    specimen, sample_date), so that same tuple arriving again from a different,
    overlapping report is the *same event re-imported*, not a second culture.
    Ward, room and confidence are attributes of an event and are deliberately
    excluded - the real data shows they never separate two same-signature rows.
    """
    return (iso.patient_id, iso.pathogen, iso.specimen_type, iso.sample_date or "")


def sensitivity_fingerprint(iso) -> tuple:
    """Order-independent digest of an isolate's antibiogram, for conflict checks."""
    return tuple(sorted((s.antibiotic, (s.result or "").upper(),
                         (s.mic or "").upper(), (s.category or "").upper())
                        for s in iso.sensitivities))


def distinct_events(isolates) -> list:
    """Collapse cross-report copies of one event to a single representative.

    Deterministic: keeps the earliest occurrence (lowest report_id, then id) so
    provenance is stable. Purely analytical - it never mutates, deletes or merges
    the underlying records, which remain in the database and the raw browser.
    """
    best: dict[tuple, object] = {}
    for iso in isolates:
        if not iso.pathogen:
            continue
        key = event_signature(iso)
        cur = best.get(key)
        if cur is None or (iso.report_id, iso.id) < (cur.report_id, cur.id):
            best[key] = iso
    return sorted(best.values(), key=lambda i: (i.report_id, i.id))


def variant_sensitivity_events(isolates) -> int:
    """Count events whose duplicate copies carry *differing* antibiograms.

    A non-zero value means the same (patient, organism, specimen, date) was
    reported with conflicting susceptibility data. We surface the count and
    preserve every raw row; we never silently pick one result over another.
    """
    groups: dict[tuple, set] = {}
    for iso in isolates:
        if not iso.pathogen:
            continue
        groups.setdefault(event_signature(iso), set()).add(sensitivity_fingerprint(iso))
    return sum(1 for fps in groups.values() if len(fps) > 1)


def window_ending_on(ref: date, length_days: int, offset_days: int = 0) -> Window:
    """Window of ``length_days`` ending ``offset_days`` before ``ref``."""
    end = ref - timedelta(days=offset_days)
    start = end - timedelta(days=length_days - 1)
    return Window(start, end)


def in_window(iso, win: Window) -> bool:
    return win.contains(iso.day)


def count_in(isolates, win: Window) -> int:
    return sum(1 for i in isolates if win.contains(i.day))


def patients_in(isolates, win: Window) -> set[str]:
    return {i.patient_id for i in isolates if win.contains(i.day)}


def wards_in(isolates, win: Window) -> set[str]:
    return {i.ward for i in isolates if win.contains(i.day) and i.ward}


def safe_div(numer: float, denom: float, default: float = 0.0) -> float:
    return numer / denom if denom else default


def pct(numer: float, denom: float, default: float = 0.0) -> float:
    """Percentage rounded to one decimal."""
    return round(100.0 * safe_div(numer, denom, default), 1)


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def bucket_length_days() -> int:
    return max(1, int(config.TREND_BUCKET_DAYS))


def weekly_buckets(ref: date, n_buckets: int) -> list[Window]:
    """``n_buckets`` consecutive weekly windows, oldest first, last one ends at ref."""
    step = bucket_length_days()
    out: list[Window] = []
    for k in range(n_buckets - 1, -1, -1):
        end = ref - timedelta(days=k * step)
        start = end - timedelta(days=step - 1)
        out.append(Window(start, end))
    return out


def bucket_label(win: Window) -> str:
    return win.end.strftime("%d %b")


def month_labels(ref: date, n_months: int = 12) -> list[str]:
    out = []
    y, m = ref.year, ref.month
    for _ in range(n_months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def monthly_windows(ref: date, n_months: int = 6) -> list[Window]:
    """Calendar-month windows, oldest first."""
    out: list[Window] = []
    year, month = ref.year, ref.month
    keys: list[str] = []
    for _ in range(n_months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    for key in reversed(keys):
        y, m = int(key[:4]), int(key[5:7])
        start = date(y, m, 1)
        end = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
        end = min(end, ref)
        if end >= start:
            out.append(Window(start, end))
    return out


def pathogen_display(key: str) -> str:
    p = PATHOGENS.get(key)
    return p.short_name if p else key


def risk_level_from_score(score: float) -> str:
    if score >= config.RISK_HIGH_THRESHOLD:
        return "High"
    if score >= config.RISK_MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def escalate(level: str, steps: int = 1) -> str:
    order = ["Low", "Medium", "High"]
    if level not in order:
        return level
    idx = min(len(order) - 1, order.index(level) + steps)
    return order[idx]


def is_high_consequence_ward(ward: str) -> bool:
    """True for ICUs, neonatal units, oncology/transplant and other fragile cohorts."""
    from ..textutil import normalise
    tokens = set(normalise(ward or "").split(" "))
    if tokens & config.HIGH_CONSEQUENCE_WARDS:
        return True
    compacted = normalise(ward or "").replace(" ", "")
    return any(code in compacted for code in
               ("icu", "nicu", "picu", "nsicu", "transplant", "oncology",
                "haematology", "dialysis", "burns", "scbu"))


def episode_dedupe(isolates) -> list:
    """One isolate per patient + pathogen (first positive), for incidence rates.

    Repeat cultures from the same patient describe the same acquisition event and
    would otherwise inflate an outbreak signal.
    """
    seen: set[tuple[str, str]] = set()
    out = []
    for i in sorted(isolates, key=lambda x: (x.sample_date or "9999", x.id)):
        k = i.acquisition_key
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def historical_expectation(items, recent: Window, length_days: int):
    """Mean/SD count per completed ``length_days`` window BEFORE ``recent``.

    Comparing a pathogen against its own endemic history is what stops a busy
    unit from reading as a permanent outbreak. Returns ``(mean, sd, bins)``.
    """
    days = [i.day for i in items if i.day]
    counts: list[int] = []
    if not days:
        return 0.0, 0.0, 0
    earliest = min(days)
    k = 0
    while k < 36:
        end = recent.start - timedelta(days=1 + k * length_days)
        start = end - timedelta(days=length_days - 1)
        if end < earliest:
            break
        if start < earliest and (end - earliest).days < length_days * 0.5:
            break
        counts.append(sum(1 for i in items
                          if Window(max(start, earliest), end).contains(i.day)))
        k += 1
    if not counts:
        return 0.0, 0.0, 0
    mean = sum(counts) / len(counts)
    var = sum((c - mean) ** 2 for c in counts) / len(counts)
    return mean, var ** 0.5, len(counts)


def endemic_daily_rate(items, span_days: int | None = None) -> float:
    """Episodes per day.

    ``span_days`` should be the length of the whole observation period, not the
    gap between the first and last of ``items``: a unit that saw two cases five
    days apart must not be told its own endemic rate is one case every two days.
    Without an explicit span the item span is used.
    """
    days = [i.day for i in items if i.day]
    if not days:
        return 0.0
    if span_days is None:
        if len(days) < 2:
            return 0.0
        span_days = (max(days) - min(days)).days + 1
    return safe_div(len(items), max(1, span_days))


def observation_span(isolates) -> int:
    """Number of calendar days covered by a dataset (0 when empty)."""
    days = [i.day for i in isolates if i.day]
    return (max(days) - min(days)).days + 1 if len(days) > 1 else (1 if days else 0)


def excess_threshold(rate_per_day: float, window_days: int, factor: float,
                     floor: int) -> int:
    """Patient count a ``window_days``-long group must exceed to be interesting.

    Turns "two isolates happened to be 14 days apart" into a decision against
    the organism's own background rate instead of a fixed magic number.
    """
    expected = rate_per_day * window_days
    return max(int(floor), int(math.ceil(factor * expected)))


def windowed_excess_threshold(episode_days, anchor: date, span_days: int,
                              window_days: int, factor: float, floor: int) -> int:
    """Patient count the window starting at ``anchor`` must exceed to be a cluster.

    The endemic expectation is learned only from episodes falling *outside* the
    window, over the days the window does not cover. Deriving it from the whole
    history instead lets a dense early group set its own bar - its cases *are*
    the background rate - so it could never exceed it and the first genuine
    outbreak in a young dataset would go unreported. When there is little
    history beyond the window the outside rate collapses toward ``floor`` and a
    real cluster surfaces on concentration alone; a broad endemic background
    keeps the bar high so ordinary co-incidence stays unreported.
    """
    end = anchor + timedelta(days=window_days - 1)
    outside = sum(1 for d in episode_days if d and (d < anchor or d > end))
    outside_days = max(0, int(span_days) - int(window_days))
    rate = safe_div(outside, outside_days)
    return excess_threshold(rate, window_days, factor, floor)

