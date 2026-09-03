"""
Outbreak risk engine.

Produces an explainable 0-100 score per pathogen (and per pathogen-in-ward unit)
from six components whose weights live in ``config.RISK_WEIGHTS``:

  excess_frequency (36)   recent episodes vs. the organism's own endemic rate
  patient_spread     (18)  how many *different* patients are involved
  case_volume        (12)  absolute number of recent episodes
  ward_concentration (10)  patients on the single most-affected ward
  resistance_burden  (14)  share of MDR/XDR/PDR and critical-agent resistance
  pathogen_threat    (10)  intrinsic severity x transmissibility of the organism

The four "activity" components (spread, volume, concentration, and nothing else)
are multiplied by an **endemic gate**: a busy hospital that is simply producing
its normal background of *E. coli* cannot accumulate a High score, because
frequency alone is not an outbreak - *excess* frequency is. Resistance burden and
intrinsic threat are never gated, since an endemic XDR organism is still a
serious problem.

``score -> High >= 70 >= Medium >= 40 >= Low`` (config thresholds), then capped
escalation rules fire for situations a weighted sum would under-call: four or
more patients linked on one ward inside the cluster window, a dominant single
ward, MDRO transmission inside a protected cohort (ICU/NICU/oncology...), the
presence of XDR or PDR phenotypes, and notifiable organisms in several patients.
Every escalation requires activity to be above the endemic expectation first.

Every assessment carries its component breakdown so the dashboard can show
*why* something is red rather than asserting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from ..pathogens import PATHOGENS
from .amr import classify_isolate, resistance_matrix
from .common import (clamp01, count_in, episode_dedupe, escalate,
                     historical_expectation, is_high_consequence_ward, patients_in,
                     pathogen_display, reference_date, risk_level_from_score, safe_div,
                     window_ending_on, wards_in)
from .trends import detect_clusters, ward_clusters


@dataclass
class Component:
    name: str
    label: str
    points: float = 0.0
    max_points: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label,
                "points": round(self.points, 1), "max_points": self.max_points,
                "detail": self.detail}


@dataclass
class RiskAssessment:
    pathogen: str
    score: float = 0.0
    base_level: str = "Low"
    level: str = "Low"
    window: str = ""
    recent_cases: int = 0
    recent_patients: int = 0
    recent_wards: int = 0
    baseline_cases: int = 0
    expected_cases: float = 0.0
    excess_ratio: float = 0.0
    z_score: float = 0.0
    endemic_gate: float = 1.0
    baseline_bins: int = 0
    growth_ratio: float = 0.0
    mdr_rate: float = 0.0
    components: list[Component] = field(default_factory=list)
    escalation_reasons: list[str] = field(default_factory=list)
    de_escalated: bool = False
    clusters: list = field(default_factory=list)
    hot_wards: list[tuple[str, int]] = field(default_factory=list)
    patients: list[str] = field(default_factory=list)
    confidence_avg: float = 1.0
    last_isolate: str = ""
    first_isolate: str = ""

    # ---- presentation ----------------------------------------------------
    @property
    def pathogen_display(self) -> str:
        return pathogen_display(self.pathogen)

    @property
    def full_name(self) -> str:
        p = PATHOGENS.get(self.pathogen)
        return p.name if p else self.pathogen

    @property
    def color(self) -> str:
        return config.RISK_COLORS.get(self.level, "#64748b")

    @property
    def top_driver(self) -> str:
        if not self.components:
            return ""
        biggest = max(self.components, key=lambda c: c.points)
        return f"{biggest.label}: {biggest.detail}" if biggest.points else ""

    @property
    def recommended_action(self) -> str:
        return recommend(self.pathogen, self.level, self)

    def as_dict(self) -> dict:
        return {
            "pathogen": self.pathogen,
            "pathogen_display": pathogen_display(self.pathogen),
            "full_name": self.full_name,
            "score": round(self.score, 1),
            "base_level": self.base_level,
            "level": self.level,
            "color": self.color,
            "window": self.window,
            "recent_cases": self.recent_cases,
            "recent_patients": self.recent_patients,
            "recent_wards": self.recent_wards,
            "baseline_cases": self.baseline_cases,
            "expected_cases": round(self.expected_cases, 1),
            "excess_ratio": round(self.excess_ratio, 2),
            "z_score": round(self.z_score, 2),
            "endemic_gate": round(self.endemic_gate, 2),
            "baseline_bins": self.baseline_bins,
            "growth_ratio": round(self.growth_ratio, 2),
            "mdr_rate": round(self.mdr_rate, 1),
            "components": [c.as_dict() for c in self.components],
            "escalation_reasons": self.escalation_reasons,
            "de_escalated": self.de_escalated,
            "hot_wards": [{"ward": w, "count": n} for w, n in self.hot_wards],
            "n_patients": len(self.patients),
            "clusters": [c.as_dict() for c in self.clusters[:4]],
            "recommended_action": self.recommended_action,
            "actions": recommend_items(self.pathogen, self.level, self),
            "first_isolate": self.first_isolate,
            "last_isolate": self.last_isolate,
        }


# --------------------------------------------------------------------------
# Component maths
# --------------------------------------------------------------------------
def _saturating(value: float, ceiling: float) -> float:
    """Linear 0..1 mapping that saturates at ``ceiling``."""
    return clamp01(safe_div(value, ceiling))


def _endemic_gate(ratio: float, has_baseline: bool) -> float:
    """Multiplier in [RISK_ENDEMIC_GATE_MIN, 1] applied to activity signals.

    ``ratio`` is recent episodes divided by the endemic expectation. At or below
    ~0.8x the gate collapses to its floor; at ``gate_ratio`` (2x) it is fully
    open. Without any usable history the gate stays open, because the absolute
    volume then *is* the only evidence available.
    """
    if not has_baseline or ratio <= 0:
        return 1.0
    lo = config.RISK_SATURATION["gate_floor"]
    hi = config.RISK_SATURATION["gate_ratio"]
    frac = clamp01((ratio - lo) / max(0.1, hi - lo))
    floor = config.RISK_ENDEMIC_GATE_MIN
    return floor + (1.0 - floor) * frac


def assess_pathogen(pathogen: str, isolates, ref=None,
                    recent_days: int | None = None,
                    baseline_days: int | None = None) -> RiskAssessment:
    """Full weighted assessment for one pathogen."""
    recent_days = recent_days or config.RECENT_WINDOW_DAYS
    baseline_days = baseline_days or config.BASELINE_WINDOW_DAYS
    ref = ref or reference_date(isolates)

    recent = window_ending_on(ref, recent_days)
    previous = window_ending_on(ref, baseline_days, offset_days=recent_days)

    subset = [i for i in isolates if i.pathogen == pathogen]
    # Count *acquisitions*, not repeat cultures from the same patient.
    episodes = episode_dedupe(subset)
    recent_eps = [i for i in episodes if recent.contains(i.day)]
    prev_eps = [i for i in episodes if previous.contains(i.day)]

    cases = len(recent_eps)
    patients = sorted({i.patient_id for i in recent_eps})
    wards = sorted({i.ward for i in recent_eps if i.ward and i.ward != "Unspecified"})
    prev = len(prev_eps)

    mean, sd, nbins = historical_expectation(episodes, recent, recent_days)
    expected = mean if nbins >= config.BASELINE_MIN_BINS else 0.0
    p = PATHOGENS[pathogen]

    # ---------------- frequency relative to endemic expectation ----------
    if nbins >= config.BASELINE_MIN_BINS:
        ratio = (cases + 0.5) / max(expected, 1.0)
        z = safe_div(cases - expected, max(sd, (max(expected, 1.0) ** 0.5)))
        excess_frac = clamp01((ratio - 1.0) / (config.RISK_SATURATION["excess_ratio"] - 1.0))
        if ratio >= 2.0 and cases >= 3:
            excess_frac = max(excess_frac, 0.6)
        if z >= 2.5 and cases >= 3:
            excess_frac = max(excess_frac, 0.6)
        excess_detail = (f"{cases} case{'s' if cases != 1 else ''} against an endemic "
                         f"expectation of {expected:.1f} per {recent_days} days "
                         f"({ratio:.1f}x, z={z:+.1f}, {nbins} historical windows)")
    else:
        # Not enough history to learn an endemic rate - fall back to the
        # immediately preceding window and, failing that, absolute volume.
        if prev:
            ratio = (cases + 0.5) / (prev + 0.5)
            excess_frac = clamp01((ratio - 1.0) / 2.0)
            excess_detail = (f"{cases} vs {prev} in the preceding {baseline_days} days "
                             f"({ratio:.1f}x); no longer history available")
        else:
            # Truly no evidence of an endemic rate to measure a rise against:
            # not enough history for a baseline AND nothing in the preceding
            # window. The excess-frequency component therefore has no baseline,
            # so it contributes nothing rather than an arbitrary bonus; a
            # cold-start score then rests only on the evidence that actually
            # exists (case volume / patient spread / ward concentration /
            # resistance / intrinsic threat), never on a fabricated expectation.
            excess_frac = 0.0
            ratio = 0.0
            excess_detail = ("no endemic baseline or prior activity to measure excess "
                             "against - excess-frequency unavailable") if cases \
                            else "no recent activity"
        z = 0.0
    if cases == 0:
        excess_frac = 0.0

    # ---------------- concentration (transmission happens in a ward) ------
    per_ward_patients: dict[str, set] = {}
    for i in recent_eps:
        per_ward_patients.setdefault(i.ward, set()).add(i.patient_id)
    hot = sorted(((w, len(ps)) for w, ps in per_ward_patients.items()),
                 key=lambda kv: (-kv[1], kv[0]))
    top_ward_patients = hot[0][1] if hot else 0

    # endemic activity is dampened so that background volume cannot dominate
    has_baseline = nbins >= config.BASELINE_MIN_BINS
    gate = _endemic_gate(ratio, has_baseline)
    at_or_above_baseline = (not has_baseline) or ratio >= 1.0

    spread_frac = _saturating(len(patients), config.RISK_SATURATION["patients_recent"])
    conc_frac = _saturating(max(0, top_ward_patients - 1),
                            config.RISK_SATURATION["ward_patients"] - 1)

    # ---------------- resistance burden ----------------------------------
    panel = recent_eps if len(recent_eps) >= 3 else episodes
    with_panel = [i for i in panel if i.sensitivities]
    mdr_n = sum(1 for i in panel if classify_isolate(i).is_mdr_or_worse)
    xdr_n = sum(1 for i in panel if classify_isolate(i).label in ("XDR", "PDR"))
    matrix = resistance_matrix(panel)
    critical_r = sum(1 for (pk, ab), r in matrix.items()
                     if pk == pathogen and ab in p.critical_drugs and r.rate >= 20)
    mdr_rate = 100.0 * safe_div(mdr_n, len(panel))
    burden_frac = clamp01(safe_div(mdr_n, max(1, len(panel)))
                          / config.RISK_SATURATION["resistance_rate"]) if panel else 0.0
    burden_frac = clamp01(burden_frac + clamp01(critical_r / 3.0) * 0.3)

    W, S = config.RISK_WEIGHTS, config.RISK_SATURATION
    comps: list[Component] = []

    def add(name, label, fraction, detail, gated=False):
        applied = fraction * gate if gated else fraction
        pts = round(W[name] * clamp01(applied), 1)
        comps.append(Component(name, label, pts, W[name], detail))

    add("excess_frequency", "Frequency vs. endemic baseline", excess_frac, excess_detail)
    add("case_volume", "Recent case volume", _saturating(cases, S["cases_recent"]),
        f"{cases} new episode{'s' if cases != 1 else ''} in the last {recent_days} days",
        gated=True)
    add("patient_spread", "Patient spread", spread_frac,
        (f"{len(patients)} distinct patients" if at_or_above_baseline else
         f"{len(patients)} distinct patients, at endemic level "
         f"(x{gate:.2f} endemic damping)"),
        gated=True)
    add("ward_concentration", "Ward concentration",
        _saturating(max(0, top_ward_patients - 1), S["ward_patients"] - 1),
        (f"{top_ward_patients} patients on {hot[0][0]}" if hot else
         "no ward-level activity"),
        gated=True)
    add("resistance_burden", "Resistance burden", burden_frac,
        (f"{mdr_n}/{len(panel)} isolates MDR or worse ({mdr_rate:.0f}%)"
         + (f", {xdr_n} XDR/PDR" if xdr_n else "") if with_panel else
         "no susceptibility panel available"))
    add("pathogen_threat", "Pathogen threat", p.threat,
        f"{p.short_name}: severity {p.severity:.2f}, transmissibility "
        f"{p.transmissibility:.2f} ({p.contact_precautions.lower()})")

    score = round(sum(c.points for c in comps), 1)
    if cases == 0:
        score = min(score, 5.0)
    base_level = risk_level_from_score(score) if cases else "Low"
    level = base_level

    reasons: list[str] = []
    steps = 0
    # Escalation only ever looks at events inside the surveillance window; the
    # endemic rate behind them is still learned from the full history.
    recent_window_clusters = [c for c in detect_clusters(subset, within=recent)
                              if c.n_patients >= 2]
    local_ward_clusters = [c for c in ward_clusters(subset, within=recent)
                           if c.n_patients >= 2]
    # Background noise must never be escalated a band by a rule alone, and a
    # rule cannot rescue a pathogen that is scoring low on its own merits.
    may_escalate = (((not has_baseline) or ratio >= config.ESCALATION_MIN_RATIO)
                    and score >= config.ESCALATION_MIN_SCORE)

    def bump(reason: str) -> bool:
        nonlocal level, steps
        if not may_escalate or steps >= config.MAX_ESCALATION_STEPS:
            return False
        steps += 1
        level = escalate(level)
        reasons.append(reason)
        return True

    # --- linked transmission on one ward ---------------------------------
    # A protected cohort needs fewer cases to matter: the same number of
    # isolates in an ICU is far more dangerous than on a general ward.
    def _ward_rank(c):
        protected = is_high_consequence_ward(c.wards[0])
        need = (config.PROTECTED_WARD_CLUSTER_PATIENTS if protected
                else config.LARGE_WARD_CLUSTER_PATIENTS)
        return c.n_patients - need if c.n_patients >= need else -1

    linked = [(c, _ward_rank(c)) for c in local_ward_clusters]
    linked = [(c, r) for c, r in linked if r >= 0]
    linked.sort(key=lambda cr: -cr[1])
    ward_hit = False
    for c, _ in linked:
        protected = is_high_consequence_ward(c.wards[0])
        if bump(f"{c.n_patients} patient{'s' if c.n_patients != 1 else ''} linked "
                f"on {c.wards[0]} within {c.span_days} day"
                f"{'s' if c.span_days != 1 else ''}"
                + (" - a protected cohort" if protected else "")):
            ward_hit = True
        if protected:
            break

    # --- pan-ward spread of a notifiable organism -------------------------
    spread = next((c for c in recent_window_clusters
                   if c.n_patients >= config.SPREAD_CLUSTER_MIN_PATIENTS
                   and c.n_wards >= 2), None)
    if spread and p.public_health:
        bump(f"notifiable organism in {spread.n_patients} patients across "
             f"{spread.n_wards} wards within {spread.span_days} days - "
             f"epidemiological linkage required")

    # --- resistance that removes the treatment options --------------------
    if xdr_n and cases >= 2 and not ward_hit:
        bump(f"{xdr_n} XDR/PDR isolate{'s' if xdr_n != 1 else ''} in the "
             f"recent window - limited treatment options")

    de_escalated = False
    if has_baseline and cases and ratio < config.ENDEMIC_SUPPRESS_RATIO:
        if level != "Low":
            de_escalated = True
        level = "Low"
        reasons.append(f"activity below endemic expectation "
                       f"({cases} vs {expected:.1f}) - treated as background noise")

    a = RiskAssessment(
        pathogen=pathogen, score=round(score, 1), base_level=base_level, level=level,
        window=f"{recent.start.isoformat()} to {recent.end.isoformat()}",
        recent_cases=cases, recent_patients=len(patients), recent_wards=len(wards),
        baseline_cases=prev, expected_cases=expected, excess_ratio=ratio, z_score=z,
        endemic_gate=gate,
        baseline_bins=nbins, growth_ratio=safe_div(cases, prev, float(cases > 0)),
        mdr_rate=mdr_rate, components=comps, escalation_reasons=reasons,
        de_escalated=de_escalated, hot_wards=hot[:5], patients=patients,
        confidence_avg=safe_div(sum(i.confidence for i in recent_eps), len(recent_eps), 1.0),
        last_isolate=max((i.sample_date for i in subset if i.sample_date), default=""),
        first_isolate=min((i.sample_date for i in subset if i.sample_date), default=""),
    )
    a.clusters = recent_window_clusters
    return a


def assess_all(isolates, ref=None, recent_days: int | None = None) -> list[RiskAssessment]:
    """Assess every tracked pathogen, highest risk first."""
    ref = ref or reference_date(isolates)
    out = [assess_pathogen(pk, [i for i in isolates if i.pathogen == pk], ref,
                           recent_days)
           for pk in PATHOGENS if any(i.pathogen == pk for i in isolates)]
    out.sort(key=lambda a: (-a.score, a.pathogen))
    return out


# --------------------------------------------------------------------------
# Ward-level unit assessments
# --------------------------------------------------------------------------
def ward_assessments(isolates, ref=None, recent_days: int | None = None) -> list[dict]:
    """Score each (ward, pathogen) pair - where an intervention actually happens.

    A ward x organism unit is scored with the *same* engine used for whole-
    organism outbreak assessment (``assess_pathogen``), fed only that ward's
    isolates. This replaces a second, unrelated ward risk formula (its own
    24/34/16/16/10 weights, no endemic baseline, no gate and ungated patient-
    count escalation) with one defensible methodology. Reusing the engine keeps
    the ward evidence genuinely ward-specific while guaranteeing parity:

      * identical component set, weights, saturation ceilings and thresholds,
      * a ward-specific endemic baseline and excess-frequency term, learned
        from this ward's own history for this organism,
      * the same endemic gate on the activity components, so a busy ward's
        ordinary background volume cannot quietly accumulate a High,
      * the same Phase 5 cold-start rule (no valid baseline -> the excess-
        frequency component contributes nothing rather than an arbitrary bonus),
      * the same escalation philosophy (protected-cohort / linked transmission),
        gated so background noise is never escalated a band,
      * the same endemic de-escalation.

    A ward and the organism it hosts are *not* required to land on the same
    number - their denominators and local histories legitimately differ - but
    every point is now explainable against the single methodology, component by
    component.
    """
    recent_days = recent_days or config.RECENT_WINDOW_DAYS
    ref = ref or reference_date(isolates)
    recent = window_ending_on(ref, recent_days)
    grouped: dict[tuple[str, str], list] = {}
    for iso in isolates:
        if iso.pathogen:
            grouped.setdefault((iso.ward, iso.pathogen), []).append(iso)

    rows = []
    for (ward, pk), subset in grouped.items():
        eps = episode_dedupe(subset)
        recent_eps = [i for i in eps if recent.contains(i.day)]
        patients = sorted({i.patient_id for i in recent_eps})
        if not patients:
            continue
        # Same engine, ward-scoped data: assess_pathogen relearns the endemic
        # expectation and applies the gate / cold-start / escalation rules using
        # only this ward's isolates, so the baseline stays ward-specific.
        a = assess_pathogen(pk, subset, ref, recent_days)
        mdr = sum(1 for i in recent_eps if classify_isolate(i).is_mdr_or_worse)
        rows.append({
            "ward": ward, "pathogen": pk, "pathogen_display": pathogen_display(pk),
            "patients": patients, "n_patients": len(patients),
            "isolates": len(recent_eps), "total_isolates": len(subset),
            "mdr": mdr, "score": a.score, "level": a.level,
            "color": config.RISK_COLORS.get(a.level, "#64748b"),
            "high_consequence": is_high_consequence_ward(ward),
            "dates": sorted({i.sample_date for i in recent_eps if i.sample_date}),
            "span_days": _span_days(recent_eps),
            "components": [c.as_dict() for c in a.components],
            "escalation_reasons": a.escalation_reasons,
        })
    rows.sort(key=lambda r: (-r["n_patients"], -r["score"]))
    return rows


def _span_days(isolates) -> int:
    days = [i.day for i in isolates if i.day]
    return (max(days) - min(days)).days + 1 if len(days) > 1 else (1 if days else 0)


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------
ACTIONS = {
    "e_coli": ["Review catheter-associated UTI bundle compliance",
               "Audit third-generation cephalosporin and fluoroquinolone usage",
               "Confirm ESBL phenotype reporting is active in the LIS"],
    "mrsa": ["Place patients in single rooms with contact precautions",
             "Screen and decolonise close contacts and roommates",
             "Audit hand-hygiene compliance on the affected ward",
             "Review shared equipment and theatre scheduling"],
    "klebsiella_pneumoniae": ["Contact isolation and cohort nursing",
                              "Carbapenem stewardship review",
                              "Environmental sampling of sinks and taps",
                              "Screen contacts for carriage"],
    "salmonella": ["Trigger food-and-water history interview for every case",
                   "Notify public health / health protection team",
                   "Review ward kitchen and patient meal distribution",
                   "Enhanced enteric precautions and hand hygiene"],
    "pseudomonas_aeruginosa": ["Environmental audit: sinks, taps, aerators, humidifiers",
                               "Review respiratory equipment disinfection",
                               "Suspend use of multi-dose saline/sterile water",
                               "Contact isolation for MDRO cases"],
    "acinetobacter_baumannii": ["Close the bay/ward to new admissions",
                                "Deep clean with sporicidal/premium-grade disinfectant",
                                "Active surveillance screening of all patients on the unit",
                                "Cohort nursing and dedicated equipment"],
    "enterococcus": ["Contact isolation for VRE", "Review antibiotic pressure (cephalosporins, fluoroquinolones)",
                     "Screen roommates and contacts", "Enhanced environmental cleaning"],
    "staph_aureus": ["Confirm oxacillin/cefoxitin testing to exclude MRSA",
                     "Wound-care and hand-hygiene audit",
                     "Screen staff carriage if cases are surgically linked"],
    "streptococcus": ["Droplet precautions for invasive disease",
                      "Chemoprophylaxis review for close contacts",
                      "Staff screening where cases follow one another in time"],
    "clostridioides_difficile": ["Switch to soap-and-water hand hygiene and sporicidal cleaning",
                                 "Review PPI and antibiotic usage on the ward",
                                 "Confirm testing algorithm (toxin vs PCR) has not changed",
                                 "Isolate cases with dedicated bathroom"],
    "proteus_mirabilis": ["Catheter-care bundle review; remove unnecessary catheters",
                          "Review hand hygiene for incontinence care"],
    "serratia_marcescens": ["Trace all injectable/flush products and disinfectants used",
                            "Quarantine and culture any suspected product batch",
                            "Contact isolation; review aseptic technique"],
}

GENERIC = {
    "High": ["Notify the Infection Prevention & Control lead today",
             "Convene an outbreak investigation with line-listing of all cases",
             "Initiate enhanced precautions and active surveillance on the affected area",
             "Escalate to the health protection/security network as applicable"],
    "Medium": ["Add to the weekly IPC review agenda",
               "Verify precaution compliance on the affected ward",
               "Passive-enhanced surveillance for a further two weeks"],
    "Low": ["Continue routine surveillance", "No action beyond standard precautions"],
}


def recommend_items(pathogen: str, level: str,
                    assessment: "RiskAssessment | None" = None) -> list[str]:
    """Ordered control measures: generic IPC steps first, organism-specific next."""
    generic = GENERIC.get(level, GENERIC["Low"])
    specific = ACTIONS.get(pathogen, [])
    items = list(generic) + specific[:3] if level != "Low" else list(generic)
    return [x.strip() for x in items if x.strip()]


def recommend(pathogen: str, level: str, assessment: RiskAssessment | None = None) -> str:
    return " \u2022 ".join(recommend_items(pathogen, level, assessment))


# --------------------------------------------------------------------------
# Dashboard roll-up
# --------------------------------------------------------------------------
def summary(isolates, ref=None) -> dict:
    ref = ref or reference_date(isolates)
    assessments = assess_all(isolates, ref)
    recent = window_ending_on(ref, config.RECENT_WINDOW_DAYS)
    by_pathogen_clusters = {a.pathogen: a.clusters for a in assessments}
    all_clusters = sorted(
        [c for cs in by_pathogen_clusters.values() for c in cs],
        key=lambda c: (-c.n_patients, c.start))
    high = [a for a in assessments if a.level == "High"]
    medium = [a for a in assessments if a.level == "Medium"]
    total_recent = count_in([i for i in isolates if i.pathogen], recent)
    hc_wards = {w for w in wards_in(isolates, recent) if is_high_consequence_ward(w)}
    return {
        "as_of": ref.isoformat(),
        "window": str(recent),
        "recent_isolates": total_recent,
        "recent_patients": len(patients_in(isolates, recent)),
        "assessments": assessments,
        "ranked": [a.as_dict() for a in assessments],
        "high": [a.as_dict() for a in high],
        "medium": [a.as_dict() for a in medium],
        "low": [a.as_dict() for a in assessments if a.level == "Low"],
        "counts": {"High": len(high), "Medium": len(medium),
                   "Low": len(assessments) - len(high) - len(medium)},
        "clusters": [c.as_dict() for c in all_clusters[:20]],
        "cluster_count": len(all_clusters),
        "ward_clusters": [c.as_dict() for c in ward_clusters(isolates, ref)[:20]],
        "ward_assessments": ward_assessments(isolates, ref)[:20],
        "high_consequence_wards": sorted(hc_wards),
        "overall_level": ("High" if high else ("Medium" if medium else "Low")),
        "overall_score": round(max((a.score for a in assessments), default=0.0), 1),
    }
