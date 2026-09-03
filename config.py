"""
Bioguard AI - central configuration.

Every tunable threshold used by the detection / trend / AMR / outbreak engines
lives here so that a hospital infection-prevention team can adapt the tool to
its own surveillance policy without touching application code.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
DB_PATH = Path(os.environ.get("BIOGUARD_DB", INSTANCE_DIR / "bioguard.db"))

# Multi-tenant layout. The registry (accounts) is shared; every hospital gets a
# physically separate data DB + uploads folder under HOSPITALS_DIR/<slug>/.
# DB_PATH / UPLOAD_DIR above are now only legacy inputs for the migration.
ACCOUNTS_DB_PATH = Path(os.environ.get("BIOGUARD_ACCOUNTS_DB",
                                       INSTANCE_DIR / "accounts.db"))
HOSPITALS_DIR = INSTANCE_DIR / "hospitals"
BACKUPS_DIR = INSTANCE_DIR / "backups"

for _d in (INSTANCE_DIR, UPLOAD_DIR, HOSPITALS_DIR, BACKUPS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Flask
# --------------------------------------------------------------------------
class Config:
    SECRET_KEY = os.environ.get("BIOGUARD_SECRET_KEY", "bioguard-dev-secret-change-me")
    MAX_CONTENT_LENGTH = int(os.environ.get("BIOGUARD_MAX_UPLOAD_MB", "25")) * 1024 * 1024
    UPLOAD_DIR = str(UPLOAD_DIR)
    DB_PATH = str(DB_PATH)
    ACCOUNTS_DB_PATH = str(ACCOUNTS_DB_PATH)
    HOSPITALS_DIR = str(HOSPITALS_DIR)
    BACKUPS_DIR = str(BACKUPS_DIR)
    JSON_SORT_KEYS = False
    TEMPLATES_AUTO_RELOAD = True


# --------------------------------------------------------------------------
# Upload handling
# --------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".txt", ".pdf"}
KEEP_UPLOADED_FILES = os.environ.get("BIOGUARD_KEEP_UPLOADS", "1") not in ("0", "false", "False")

# --------------------------------------------------------------------------
# Organism matching
# --------------------------------------------------------------------------
# Minimum confidence required before an unrecognised organism string is
# snapped to a target pathogen by fuzzy matching.
FUZZY_MATCH_CUTOFF = 0.88
# Genus-only reports ("Klebsiella spp.") are attributed to the target species of
# that genus, but at this reduced confidence and flagged for review.
GENUS_LEVEL_CONFIDENCE = 0.65

# --------------------------------------------------------------------------
# Surveillance / trend engine
# --------------------------------------------------------------------------
RECENT_WINDOW_DAYS = 30          # primary "current signal" window
BASELINE_WINDOW_DAYS = 30        # comparison window immediately preceding RECENT
TREND_BUCKET_DAYS = 7            # weekly buckets for time-series charts
CLUSTER_WINDOW_DAYS = 14         # rolling window for transmission-cluster search
CLUSTER_MIN_PATIENTS = 2         # distinct patients in window => candidate cluster
# A ward-confined group needs three patients to be reported. Two isolates of the
# same species on a busy ward two weeks apart is normal background: with 12 wards
# x 12 organisms scanned, a floor of 2 produced ~99 "clusters" and drowned the
# handful that mattered.
WARD_CLUSTER_MIN_PATIENTS = 3
# A window must hold this multiple of the organism's OWN expected count for that
# window length before it is reported as a cluster. Without it a busy hospital
# yields hundreds of meaningless "clusters" of unrelated background isolates.
CLUSTER_EXCESS_FACTOR = 1.6
# Past this many distinct patients, spread alone counts as linkage even when no
# single ward holds two of them.
CLUSTER_SPREAD_MIN_PATIENTS = 6
MIN_ISOLATES_FOR_RESISTANCE_RATE = 5   # suppress noisy resistance percentages

# --------------------------------------------------------------------------
# Antimicrobial-resistance engine
# --------------------------------------------------------------------------
# EUCAST/WHO-style definitions used here:
#   MDR = non-susceptible to >= 1 agent in >= 3 antimicrobial categories
#   XDR = non-susceptible to >= 1 agent in all but <= 2 categories
#   PDR = non-susceptible to all agents tested
MDR_MIN_CATEGORIES_RESISTANT = 3
XDR_MAX_CATEGORIES_WITH_OPTION = 2

# Resistance rates at/above this level are surfaced as AMR alerts.
AMR_ALERT_RATE = 0.20
# A rise in resistance of this magnitude (percentage points) is a "worsening" signal.
AMR_RISE_ALERT_PP = 10

# --------------------------------------------------------------------------
# Outbreak risk scoring
# --------------------------------------------------------------------------
# Every signal is expressed relative to the organism's OWN endemic baseline,
# because raw frequency alone would keep a busy hospital permanently "red".
# Component weights are maximum attainable points and sum to 100.
# ``excess_frequency`` and ``pathogen_threat`` always count in full; the four
# "activity" components below are multiplied by the endemic gate (see
# RISK_ENDEMIC_GATE_MIN) so that broad-but-routine background volume cannot
# quietly accumulate a High score.
RISK_WEIGHTS = {
    "excess_frequency": 36,    # recent cases vs. the endemic expectation
    "case_volume": 12,         # absolute number of recent cases
    "patient_spread": 18,      # distinct patients in the recent window
    "ward_concentration": 10,  # most-affected single ward
    "resistance_burden": 14,   # MDR/XDR/PDR + critical-agent resistance
    "pathogen_threat": 10,     # intrinsic severity x transmissibility
}

# Score bands -> risk level.
RISK_HIGH_THRESHOLD = 70
RISK_MEDIUM_THRESHOLD = 40

# Normalisation ceilings: the value at which a component saturates.
RISK_SATURATION = {
    "cases_recent": 20,        # >= 20 recent episodes saturates case_volume
    "patients_recent": 10,     # >= 10 distinct recent patients saturates spread
    "excess_ratio": 3.0,       # 3x the endemic expectation saturates excess
    "ward_patients": 5,        # >= 5 patients in one ward saturates concentration
    "resistance_rate": 0.5,    # 50% MDR-or-worse saturates burden
    "gate_ratio": 2.0,         # ratio at which activity components are undamped
    "gate_floor": 0.8,         # ratio at/below which activity components are minimal
}

# Multiplier applied to the volume/spread/concentration components when the
# current activity is at or below the organism's endemic expectation.
RISK_ENDEMIC_GATE_MIN = 0.25

# A recent count below ``endemic_suppress_ratio`` x the historical expectation
# de-escalates the level to Low: nothing unusual is happening.
ENDEMIC_SUPPRESS_RATIO = 0.85
# Escalation rules are only considered once activity is this far above the
# endemic expectation - a busy unit's background noise must not trigger them.
ESCALATION_MIN_RATIO = 1.25
# ...and the weighted score must already be this high. A rule alone cannot turn
# a clinically quiet pathogen red.
ESCALATION_MIN_SCORE = 30
# Linked patients on one ward inside the surveillance window that escalate a band.
LARGE_WARD_CLUSTER_PATIENTS = 5       # ordinary ward
PROTECTED_WARD_CLUSTER_PATIENTS = 3   # ICU / NICU / oncology / transplant ...
# Distinct patients across >= 2 wards needed before a notifiable organism's
# geographic spread is itself treated as an escalation.
SPREAD_CLUSTER_MIN_PATIENTS = 6
# How many completed historical windows are used to learn the endemic baseline.
BASELINE_MIN_BINS = 2
# Maximum number of risk bands the escalation rules may add.
MAX_ESCALATION_STEPS = 2

# Ward types considered intrinsically high-consequence; clusters here escalate
# one risk band (the "protected cohort" rule).
HIGH_CONSEQUENCE_WARDS = {
    "icu", "nsicu", "picu", "nicu", "hdu", "ccu", "micu", "sicu",
    "transplant", "oncology", "haematology", "dialysis", "renal", "burns",
    "neonatal", "special", "baby", "scbu", "protective",
}

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
APP_NAME = "BioGuard AI"
APP_TAGLINE = "Hospital Infection Surveillance"
OFFICIAL_NAME = ("BioGuard AI - Hospital Infection Surveillance "
                 "and Outbreak Prediction System")
DEFAULT_PAGE_SIZE = 25
# Risk palette is presentation only - it never feeds a calculation.
RISK_COLORS = {"High": "#EA580C", "Medium": "#F59E0B", "Low": "#059669",
               "None": "#64748B"}
