"""
Antimicrobial-resistance (AMR) engine.

Answers four questions for the infection-prevention team:

1. How resistant is each pathogen to each agent?  (:func:`resistance_matrix`)
2. How much of each pathogen population is MDR / XDR / PDR? (:func:`burden`)
3. Is resistance rising, falling or stable?  (:func:`trends`)
4. Which specific agent/pathogen combinations need action now? (:func:`alerts`)

Resistance is measured against **acquired** non-susceptibility only: an agent a
species is intrinsically resistant to (e.g. *Klebsiella* and ampicillin, or
*Proteus* and nitrofurantoin) is excluded from rates, otherwise every Gram-
negative panel would open at a misleading 100%.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from ..antibiotics import (ANTIBIOTICS, LAST_LINE_GRAM_NEGATIVE,
                           LAST_LINE_GRAM_POSITIVE, display_name, category_display)
from ..pathogens import PATHOGENS
from .common import (Window, clamp01, monthly_windows, pathogen_display, pct,
                     reference_date, safe_div)


# --------------------------------------------------------------------------
# Per-isolate classification (EUCAST/WHO-style definitions)
# --------------------------------------------------------------------------
@dataclass
class ResistanceClass:
    label: str = ""            # '', 'MDR', 'XDR', 'PDR'
    categories_tested: int = 0
    categories_resistant: int = 0
    resistant_drugs: list[str] = field(default_factory=list)
    critical_hit: str = ""     # agent of clinical last-resort that tested R

    @property
    def is_mdr_or_worse(self) -> bool:
        return self.label in ("MDR", "XDR", "PDR")

    @property
    def detail(self) -> str:
        if not self.label:
            return "No acquired resistance detected"
        return (f"{self.label}: resistant to {self.categories_resistant} of "
                f"{self.categories_tested} antimicrobial categories")


def classify_isolate(iso) -> ResistanceClass:
    """MDR / XDR / PDR classification for a single isolate."""
    tested = {s.category for s in iso.sensitivities
              if s.result in ("S", "I", "R") and not s.intrinsic}
    resistant = {s.category for s in iso.sensitivities
                 if s.result == "R" and not s.intrinsic}
    drugs = [s.antibiotic for s in iso.sensitivities
             if s.result == "R" and not s.intrinsic]

    critical = ""
    panel = PATHOGENS.get(iso.pathogen)
    critical_drugs = set(panel.critical_drugs) if panel else set()
    for s in iso.sensitivities:
        if s.result != "R" or s.intrinsic:
            continue
        if s.antibiotic in critical_drugs:
            critical = s.antibiotic
            break

    out = ResistanceClass(categories_tested=len(tested),
                          categories_resistant=len(resistant),
                          resistant_drugs=sorted(drugs), critical_hit=critical)

    if not resistant:
        out.label = ""
        return out
    if tested and resistant == tested and len(tested) >= 2:
        out.label = "PDR"
        return out
    remaining = tested - resistant
    if len(remaining) <= config.XDR_MAX_CATEGORIES_WITH_OPTION and len(resistant) >= 4:
        out.label = "XDR"
        return out
    if len(resistant) >= config.MDR_MIN_CATEGORIES_RESISTANT:
        out.label = "MDR"
        return out
    out.label = "Non-MDR resistant"
    return out


# --------------------------------------------------------------------------
# Resistance rates
# --------------------------------------------------------------------------
@dataclass
class AgentRate:
    pathogen: str
    antibiotic: str
    tested: int = 0
    resistant: int = 0
    intermediate: int = 0
    susceptible: int = 0
    rate: float = 0.0
    recent_rate: float | None = None
    is_critical: bool = False

    @property
    def antibiotic_display(self) -> str:
        return display_name(self.antibiotic)

    @property
    def category(self) -> str:
        ab = ANTIBIOTICS.get(self.antibiotic)
        return category_display(ab.category) if ab else "Other"


def resistance_matrix(isolates) -> dict[tuple[str, str], AgentRate]:
    """Resistance rate for every (pathogen, agent) pair that has been tested."""
    out: dict[tuple[str, str], AgentRate] = {}

    for iso in isolates:
        if not iso.pathogen:
            continue
        critical = (PATHOGENS[iso.pathogen].critical_drugs
                    if iso.pathogen in PATHOGENS else set())
        for s in iso.sensitivities:
            if s.result not in ("S", "I", "R") or s.intrinsic or not s.antibiotic:
                continue
            key = (iso.pathogen, s.antibiotic)
            rec = out.get(key)
            if rec is None:
                rec = out[key] = AgentRate(pathogen=iso.pathogen,
                                           antibiotic=s.antibiotic,
                                           is_critical=s.antibiotic in critical)
            rec.tested += 1
            if s.result == "R":
                rec.resistant += 1
            elif s.result == "I":
                rec.intermediate += 1
            else:
                rec.susceptible += 1
    for rec in out.values():
        rec.rate = round(100.0 * safe_div(rec.resistant, rec.tested), 1)
    return out


def agent_rows(isolates, pathogen: str | None = None, min_tests: int = 1,
               sort_by_resistance: bool = True) -> list[dict]:
    """Flat, UI-ready resistance table."""
    matrix = resistance_matrix(
        [i for i in isolates if not pathogen or i.pathogen == pathogen])
    rows = []
    for (pk, ab), rec in matrix.items():
        if rec.tested < min_tests:
            continue
        rows.append({
            "pathogen": pk,
            "pathogen_display": pathogen_display(pk),
            "antibiotic": ab,
            "antibiotic_display": display_name(ab),
            "category": category_display(ANTIBIOTICS[ab].category) if ab in ANTIBIOTICS else "Other",
            "tested": rec.tested,
            "resistant": rec.resistant,
            "intermediate": rec.intermediate,
            "susceptible": rec.susceptible,
            "rate": rec.rate,
            "non_susceptible": round(100.0 * safe_div(rec.resistant + rec.intermediate,
                                                      rec.tested), 1),
            "is_critical": rec.is_critical,
        })
    if sort_by_resistance:
        rows.sort(key=lambda r: (-r["rate"], -r["tested"]))
    else:
        rows.sort(key=lambda r: (r["pathogen_display"], r["antibiotic_display"]))
    return rows


def burden(isolates) -> dict:
    """MDR/XDR/PDR population statistics for one pathogen's isolates."""
    total = len(isolates)
    with_panel = 0
    counts = {"MDR": 0, "XDR": 0, "PDR": 0, "Non-MDR resistant": 0, "none": 0}
    critical_hits: dict[str, int] = {}
    per_patient: dict[str, str] = {}
    for iso in isolates:
        cls = classify_isolate(iso)
        if iso.sensitivities:
            with_panel += 1
        key = iso.acquisition_key
        rank = {"": 0, "Non-MDR resistant": 1, "MDR": 2, "XDR": 3, "PDR": 4}
        if key not in per_patient or rank[cls.label] > rank[per_patient[key]]:
            per_patient[key] = cls.label
        counts[cls.label if cls.label else "none"] = counts.get(
            cls.label if cls.label else "none", 0) + 1
        if cls.critical_hit:
            critical_hits[cls.critical_hit] = critical_hits.get(cls.critical_hit, 0) + 1

    episodes = list(per_patient.values())
    n_ep = len(episodes)
    # Distinct EPISODES (patient + organism) whose worst-presentation isolate
    # meets each criterion. Aggregated per episode so one patient's repeat,
    # same-day or duplicate-imported cultures can never contribute twice.
    mdr_ep = sum(1 for e in episodes if e in ("MDR", "XDR", "PDR"))
    xdr_ep = sum(1 for e in episodes if e in ("XDR", "PDR"))
    anyres_ep = sum(1 for e in episodes if e != "")
    return {
        # Laboratory-workload counts (ISOLATE-level), de-duplicated to one row
        # per distinct event by the caller; explicitly isolate-basis.
        "isolates": total,
        "isolates_with_panel": with_panel,
        "episodes": n_ep,
        "mdr": counts.get("MDR", 0),
        "xdr": counts.get("XDR", 0),
        "pdr": counts.get("PDR", 0),
        "any_resistance": counts.get("MDR", 0) + counts.get("XDR", 0)
        + counts.get("PDR", 0) + counts.get("Non-MDR resistant", 0),
        # Clinical prevalence (EPISODE denominator): an episode with several
        # qualifying cultures still counts once.
        "mdr_rate": pct(mdr_ep, n_ep),
        "xdr_rate": pct(xdr_ep, n_ep),
        # Laboratory burden (ISOLATE denominator, MDR-or-worse) - intentionally
        # DISTINCT from mdr_rate: "% of isolates that are MDR+" vs "% of episodes
        # with MDR-or-worse". Fed with deduplicated event isolates, so a
        # cross-report duplicate cannot inflate its numerator or denominator.
        "resistance_share": pct(counts.get("MDR", 0) + counts.get("XDR", 0)
                                + counts.get("PDR", 0), total),
        "critical_hits": sorted(critical_hits.items(), key=lambda kv: -kv[1])[:5],
        "by_episode": {
            "mdr_or_worse": mdr_ep,
            "xdr_or_pdr": xdr_ep,
            "any_resistant": anyres_ep,
        },
    }


# --------------------------------------------------------------------------
# Time-series resistance trends
# --------------------------------------------------------------------------
def trends(isolates, ref=None, n_months: int = 6) -> dict:
    """Monthly resistance rates per pathogen, plus per-agent rise/fall."""
    ref = ref or reference_date(isolates)
    wins = monthly_windows(ref, n_months)
    labels = [w.end.strftime("%b %Y") for w in wins]

    by_pathogen: dict[str, list[dict]] = {}
    buckets_by_pathogen: dict[str, list[list]] = {
        pk: [[] for _ in wins] for pk in PATHOGENS}
    agent_series: dict[tuple[str, str], list[list]] = {}

    for iso in isolates:
        if not iso.pathogen:
            continue
        idx = _window_index(iso, wins)
        if idx is None:
            continue
        for s in iso.sensitivities:
            if s.result not in ("S", "I", "R") or s.intrinsic or not s.antibiotic:
                continue
            buckets_by_pathogen[iso.pathogen][idx].append(s.result == "R")
            key = (iso.pathogen, s.antibiotic)
            agent_series.setdefault(key, [[] for _ in wins])
            agent_series[key][idx].append(s.result == "R")

    for pk, buckets in buckets_by_pathogen.items():
        series = []
        for w, vals in zip(wins, buckets):
            series.append({
                "label": w.end.strftime("%b %Y"),
                "tests": len(vals),
                "rate": pct(sum(vals), len(vals)) if vals else None,
            })
        by_pathogen[pk] = series

    movements = []
    for (pk, ab), buckets in agent_series.items():
        first_n, first_r, last_n, last_r = 0, 0, 0, 0
        for vals in buckets[:max(1, len(buckets) // 2)]:
            first_n += len(vals); first_r += sum(vals)
        for vals in buckets[max(1, len(buckets) // 2):]:
            last_n += len(vals); last_r += sum(vals)
        if first_n < 4 or last_n < 4:
            continue
        a = 100.0 * first_r / first_n
        b = 100.0 * last_r / last_n
        movements.append({
            "pathogen": pk,
            "pathogen_display": pathogen_display(pk),
            "antibiotic": ab,
            "antibiotic_display": display_name(ab),
            "from_rate": round(a, 1),
            "to_rate": round(b, 1),
            "delta_pp": round(b - a, 1),
            "tests_early": first_n,
            "tests_recent": last_n,
            "direction": "rising" if b - a >= config.AMR_RISE_ALERT_PP else (
                "falling" if a - b >= config.AMR_RISE_ALERT_PP else "stable"),
        })
    movements.sort(key=lambda m: -m["delta_pp"])

    return {"labels": labels, "by_pathogen": by_pathogen, "movements": movements,
            "windows": [str(w) for w in wins]}


def _window_index(iso, wins: list[Window]) -> int | None:
    d = iso.day
    if d is None:
        return None
    for i, w in enumerate(wins):
        if w.contains(d):
            return i
    return None


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def alerts(isolates, top_n: int = 8) -> list[dict]:
    """Highest-priority AMR findings, ranked by clinical consequence."""
    rows = agent_rows(isolates, min_tests=config.MIN_ISOLATES_FOR_RESISTANCE_RATE)
    out = []
    for r in rows:
        score = r["rate"]
        if r["is_critical"]:
            score += 25
        if r["tested"] >= 20:
            score += 5
        if r["rate"] < config.AMR_ALERT_RATE * 100:
            continue
        out.append({**r, "score": score,
                    "severity": _severity(r["rate"], r["is_critical"], r["tested"])})
    out.sort(key=lambda d: -d["score"])
    return out[:top_n]


def _severity(rate: float, critical: bool, tested: int) -> str:
    if critical and rate >= 30:
        return "Critical"
    if critical and rate >= config.AMR_ALERT_RATE * 100:
        return "High"
    if rate >= 50:
        return "High"
    if tested >= 20 and rate >= 35:
        return "High"
    return "Medium"


# --------------------------------------------------------------------------
# Whole-dataset AMR summary
# --------------------------------------------------------------------------
def summary(isolates, ref=None) -> dict:
    """Aggregate AMR picture across every tracked pathogen."""
    ref = ref or reference_date(isolates)
    per_pathogen = {}
    for pk in PATHOGENS:
        subset = [i for i in isolates if i.pathogen == pk]
        if not subset:
            continue
        b = burden(subset)
        rows = agent_rows(subset, min_tests=1)
        top = rows[0] if rows else None
        per_pathogen[pk] = {
            "pathogen": pk,
            "pathogen_display": pathogen_display(pk),
            **b,
            "top_agent": top["antibiotic_display"] if top else "",
            "top_rate": top["rate"] if top else 0,
            "worst_agents": rows[:4],
        }
    all_rows = agent_rows(isolates, min_tests=1)
    gram_neg = [i for i in isolates if i.pathogen in PATHOGENS
                and PATHOGENS[i.pathogen].gram == "negative"]
    gn_b = burden(gram_neg)
    gp = [i for i in isolates if i.pathogen in PATHOGENS
          and PATHOGENS[i.pathogen].gram == "positive"]
    gp_b = burden(gp)
    total_ep = sum(v["episodes"] for v in per_pathogen.values())
    total_mdr = sum(v["by_episode"]["mdr_or_worse"] for v in per_pathogen.values())

    return {
        "as_of": ref.isoformat(),
        "per_pathogen": per_pathogen,
        "ranked": sorted(per_pathogen.values(),
                         key=lambda v: (-v["mdr_rate"], -v["isolates"])),
        "gram_negative": gn_b,
        "gram_positive": gp_b,
        "overall_mdr_rate": pct(total_mdr, total_ep),
        "highest_resistance": all_rows[:12],
        "alerts": alerts(isolates),
        "movements": trends(isolates, ref)["movements"][:10],
        "last_line_exhausted": _last_line_exhaustion(isolates),
    }


def _last_line_exhaustion(isolates) -> list[dict]:
    """Isolates non-susceptible to every last-resort agent that was tested."""
    out = []
    for iso in isolates:
        if not iso.pathogen or iso.pathogen not in PATHOGENS:
            continue
        gram = PATHOGENS[iso.pathogen].gram
        reserve = (LAST_LINE_GRAM_NEGATIVE if gram == "negative"
                   else LAST_LINE_GRAM_POSITIVE)
        tested = [s for s in iso.sensitivities
                  if s.antibiotic in reserve and s.result in ("S", "I", "R")
                  and not s.intrinsic]
        if len(tested) >= 2 and all(s.result != "S" for s in tested):
            out.append({
                "patient_id": iso.patient_id,
                "ward": iso.ward,
                "date": iso.sample_date,
                "pathogen": iso.pathogen,
                "pathogen_display": pathogen_display(iso.pathogen),
                "agents": sorted({display_name(s.antibiotic) for s in tested}),
                "id": iso.id,
            })
    out.sort(key=lambda d: d["date"], reverse=True)
    return out


def panel_for(pathogen_key: str) -> list[dict]:
    """Agents a laboratory would typically report - used to explain gaps."""
    from ..demo_data import DEFAULT_PANELS
    keys = DEFAULT_PANELS.get(pathogen_key, [])
    return [{"antibiotic": k, "name": display_name(k),
             "category": category_display(ANTIBIOTICS[k].category)}
            for k in keys if k in ANTIBIOTICS]
