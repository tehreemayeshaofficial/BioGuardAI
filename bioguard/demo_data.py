"""
Synthetic but epidemiologically realistic surveillance data.

Bioguard ships with this generator so the dashboard, trend, AMR and outbreak
engines can be demonstrated end-to-end without needing patient-identifiable
hospital data. It seeds twelve scenarios - one per tracked pathogen - each with:

* an endemic background rate,
* an embedded outbreak with the signature the risk engine should catch, and
* time-varying resistance probabilities so AMR *trends* are visible too.

Generation is deterministic (fixed seed) so every install shows the same
demonstration dataset.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .antibiotics import ANTIBIOTICS
from .detection import normalise_patient
from .parsing.common import LabRecord, Sensitivity
from .pathogens import PATHOGENS

SEED = 20260829
HISTORY_DAYS = 210          # 7 completed 30-day windows: enough to learn an endemic rate

WARDS = [
    ("ICU", 22), ("Ward 3", 14), ("Ward 5", 12), ("Ward 8", 12), ("Ward 12", 12),
    ("NICU", 8), ("Emergency Department", 10), ("Surgical Ward", 9),
    ("Renal Dialysis", 6), ("Oncology", 6), ("Geriatrics", 6), ("Outpatient", 8),
]

SPECIMENS_BY_PATHOGEN = {
    "e_coli": ["Urine", "Blood", "Blood", "Urine", "Ascitic fluid"],
    "mrsa": ["Blood", "Wound", "Nasal swab", "Abscess", "Sputum"],
    "klebsiella_pneumoniae": ["Blood", "Sputum", "Urine", "Wound", "BAL"],
    "salmonella": ["Stool", "Blood", "Stool", "Stool"],
    "pseudomonas_aeruginosa": ["Sputum", "Blood", "Urine", "Wound", "Catheter tip"],
    "acinetobacter_baumannii": ["Sputum", "Blood", "Wound", "BAL", "CSF"],
    "enterococcus": ["Urine", "Blood", "Wound", "Ascitic fluid"],
    "staph_aureus": ["Blood", "Wound", "Sputum", "Abscess", "Joint fluid"],
    "streptococcus": ["Blood", "Sputum", "CSF", "Throat swab", "Pleural fluid"],
    "clostridioides_difficile": ["Stool", "Stool", "Rectal swab"],
    "proteus_mirabilis": ["Urine", "Urine", "Wound", "Blood"],
    "serratia_marcescens": ["Blood", "Sputum", "Urine", "Catheter tip", "Eye"],
}

# Agents a laboratory would normally report for each organism.
DEFAULT_PANELS = {
    "e_coli": ["ampicillin", "amoxicillin_clav", "cefuroxime", "ceftriaxone",
               "ceftazidime", "piperacillin_tazobactam", "ciprofloxacin", "gentamicin",
               "amikacin", "cotrimoxazole", "nitrofurantoin", "fosfomycin", "meropenem"],
    "klebsiella_pneumoniae": ["ampicillin", "cefuroxime", "ceftriaxone", "ceftazidime",
                              "cefepime", "piperacillin_tazobactam", "ciprofloxacin",
                              "gentamicin", "amikacin", "cotrimoxazole", "ertapenem",
                              "meropenem", "tigecycline", "colistin"],
    "pseudomonas_aeruginosa": ["piperacillin_tazobactam", "ceftazidime", "cefepime",
                               "meropenem", "imipenem", "ciprofloxacin", "gentamicin",
                               "tobramycin", "amikacin", "colistin"],
    "acinetobacter_baumannii": ["ampicillin_sulbactam", "piperacillin_tazobactam",
                                "ceftazidime", "cefepime", "meropenem", "imipenem",
                                "ciprofloxacin", "gentamicin", "amikacin", "minocycline",
                                "tigecycline", "colistin"],
    "mrsa": ["oxacillin", "vancomycin", "teicoplanin", "linezolid", "daptomycin",
             "clindamycin", "cotrimoxazole", "rifampicin", "erythromycin",
             "ciprofloxacin", "gentamicin", "tetracycline"],
    "staph_aureus": ["oxacillin", "penicillin_g", "clindamycin", "erythromycin",
                     "cotrimoxazole", "tetracycline", "gentamicin", "ciprofloxacin",
                     "vancomycin", "teicoplanin", "linezolid", "rifampicin"],
    "enterococcus": ["ampicillin", "vancomycin", "teicoplanin", "linezolid",
                     "daptomycin", "gentamicin", "streptomycin", "tetracycline"],
    "salmonella": ["ampicillin", "cotrimoxazole", "nalidixic_acid", "ciprofloxacin",
                   "ceftriaxone", "azithromycin"],
    "streptococcus": ["penicillin_g", "amoxicillin", "erythromycin", "clindamycin",
                      "ceftriaxone", "cotrimoxazole", "tetracycline", "vancomycin"],
    "clostridioides_difficile": ["vancomycin", "metronidazole", "fidaxomicin"],
    "proteus_mirabilis": ["ampicillin", "cefuroxime", "ceftriaxone", "cefepime",
                          "piperacillin_tazobactam", "meropenem", "ciprofloxacin",
                          "gentamicin", "amikacin", "cotrimoxazole", "nitrofurantoin"],
    "serratia_marcescens": ["cefuroxime", "ceftriaxone", "ceftazidime", "cefepime",
                            "piperacillin_tazobactam", "meropenem", "ciprofloxacin",
                            "gentamicin", "amikacin", "cotrimoxazole", "tigecycline",
                            "colistin"],
}

# Baseline probability that an isolate is resistant to each agent.
BASE_RESISTANCE = {
    ("e_coli", "ampicillin"): .45, ("e_coli", "amoxicillin_clav"): .28,
    ("e_coli", "cefuroxime"): .22, ("e_coli", "ceftriaxone"): .14,
    ("e_coli", "ceftazidime"): .10, ("e_coli", "piperacillin_tazobactam"): .12,
    ("e_coli", "ciprofloxacin"): .22, ("e_coli", "gentamicin"): .14,
    ("e_coli", "amikacin"): .05, ("e_coli", "cotrimoxazole"): .25,
    ("e_coli", "nitrofurantoin"): .04, ("e_coli", "fosfomycin"): .05,
    ("e_coli", "meropenem"): .01,
    ("klebsiella_pneumoniae", "ampicillin"): 1.0,
    ("klebsiella_pneumoniae", "cefuroxime"): .35,
    ("klebsiella_pneumoniae", "ceftriaxone"): .28,
    ("klebsiella_pneumoniae", "ceftazidime"): .25,
    ("klebsiella_pneumoniae", "cefepime"): .22,
    ("klebsiella_pneumoniae", "piperacillin_tazobactam"): .24,
    ("klebsiella_pneumoniae", "ciprofloxacin"): .26,
    ("klebsiella_pneumoniae", "gentamicin"): .22,
    ("klebsiella_pneumoniae", "amikacin"): .10,
    ("klebsiella_pneumoniae", "cotrimoxazole"): .22,
    ("klebsiella_pneumoniae", "ertapenem"): .10,
    ("klebsiella_pneumoniae", "meropenem"): .09,
    ("klebsiella_pneumoniae", "tigecycline"): .04,
    ("klebsiella_pneumoniae", "colistin"): .05,
    ("pseudomonas_aeruginosa", "piperacillin_tazobactam"): .22,
    ("pseudomonas_aeruginosa", "ceftazidime"): .20,
    ("pseudomonas_aeruginosa", "cefepime"): .18,
    ("pseudomonas_aeruginosa", "meropenem"): .21,
    ("pseudomonas_aeruginosa", "imipenem"): .24,
    ("pseudomonas_aeruginosa", "ciprofloxacin"): .22,
    ("pseudomonas_aeruginosa", "gentamicin"): .18,
    ("pseudomonas_aeruginosa", "tobramycin"): .16,
    ("pseudomonas_aeruginosa", "amikacin"): .08,
    ("pseudomonas_aeruginosa", "colistin"): .02,
    ("acinetobacter_baumannii", "ampicillin_sulbactam"): .55,
    ("acinetobacter_baumannii", "piperacillin_tazobactam"): .60,
    ("acinetobacter_baumannii", "ceftazidime"): .68,
    ("acinetobacter_baumannii", "cefepime"): .65,
    ("acinetobacter_baumannii", "meropenem"): .72,
    ("acinetobacter_baumannii", "imipenem"): .74,
    ("acinetobacter_baumannii", "ciprofloxacin"): .70,
    ("acinetobacter_baumannii", "gentamicin"): .62,
    ("acinetobacter_baumannii", "amikacin"): .38,
    ("acinetobacter_baumannii", "minocycline"): .40,
    ("acinetobacter_baumannii", "tigecycline"): .22,
    ("acinetobacter_baumannii", "colistin"): .06,
    ("mrsa", "oxacillin"): 1.0, ("mrsa", "vancomycin"): .01,
    ("mrsa", "teicoplanin"): .01, ("mrsa", "linezolid"): .01,
    ("mrsa", "daptomycin"): .02, ("mrsa", "clindamycin"): .38,
    ("mrsa", "cotrimoxazole"): .12, ("mrsa", "rifampicin"): .10,
    ("mrsa", "erythromycin"): .62, ("mrsa", "ciprofloxacin"): .58,
    ("mrsa", "gentamicin"): .34, ("mrsa", "tetracycline"): .28,
    ("staph_aureus", "oxacillin"): .02, ("staph_aureus", "penicillin_g"): .85,
    ("staph_aureus", "clindamycin"): .18, ("staph_aureus", "erythromycin"): .26,
    ("staph_aureus", "cotrimoxazole"): .06, ("staph_aureus", "tetracycline"): .10,
    ("staph_aureus", "gentamicin"): .14, ("staph_aureus", "ciprofloxacin"): .22,
    ("staph_aureus", "vancomycin"): .005, ("staph_aureus", "teicoplanin"): .005,
    ("staph_aureus", "linezolid"): .005, ("staph_aureus", "rifampicin"): .05,
    ("enterococcus", "ampicillin"): .35, ("enterococcus", "vancomycin"): .14,
    ("enterococcus", "teicoplanin"): .16, ("enterococcus", "linezolid"): .04,
    ("enterococcus", "daptomycin"): .05, ("enterococcus", "gentamicin"): .30,
    ("enterococcus", "streptomycin"): .34, ("enterococcus", "tetracycline"): .48,
    ("salmonella", "ampicillin"): .18, ("salmonella", "cotrimoxazole"): .16,
    ("salmonella", "nalidixic_acid"): .22, ("salmonella", "ciprofloxacin"): .14,
    ("salmonella", "ceftriaxone"): .05, ("salmonella", "azithromycin"): .03,
    ("streptococcus", "penicillin_g"): .06, ("streptococcus", "amoxicillin"): .06,
    ("streptococcus", "erythromycin"): .22, ("streptococcus", "clindamycin"): .18,
    ("streptococcus", "ceftriaxone"): .07, ("streptococcus", "cotrimoxazole"): .30,
    ("streptococcus", "tetracycline"): .26, ("streptococcus", "vancomycin"): .002,
    ("clostridioides_difficile", "vancomycin"): .01,
    ("clostridioides_difficile", "metronidazole"): .06,
    ("clostridioides_difficile", "fidaxomicin"): .01,
    ("proteus_mirabilis", "ampicillin"): .38, ("proteus_mirabilis", "cefuroxime"): .22,
    ("proteus_mirabilis", "ceftriaxone"): .16, ("proteus_mirabilis", "cefepime"): .12,
    ("proteus_mirabilis", "piperacillin_tazobactam"): .14,
    ("proteus_mirabilis", "meropenem"): .04, ("proteus_mirabilis", "ciprofloxacin"): .20,
    ("proteus_mirabilis", "gentamicin"): .16, ("proteus_mirabilis", "amikacin"): .05,
    ("proteus_mirabilis", "cotrimoxazole"): .22,
    ("proteus_mirabilis", "nitrofurantoin"): 1.0,
    ("serratia_marcescens", "cefuroxime"): .55,
    ("serratia_marcescens", "ceftriaxone"): .22,
    ("serratia_marcescens", "ceftazidime"): .18,
    ("serratia_marcescens", "cefepime"): .15,
    ("serratia_marcescens", "piperacillin_tazobactam"): .16,
    ("serratia_marcescens", "meropenem"): .07,
    ("serratia_marcescens", "ciprofloxacin"): .14,
    ("serratia_marcescens", "gentamicin"): .18,
    ("serratia_marcescens", "amikacin"): .07,
    ("serratia_marcescens", "cotrimoxazole"): .12,
    ("serratia_marcescens", "tigecycline"): .05,
    ("serratia_marcescens", "colistin"): 1.0,
}

# Agents whose resistance is *worsening* over the demonstration period.
# value = additional absolute resistance probability acquired by day HISTORY_DAYS.
AMR_TRENDS = {
    ("klebsiella_pneumoniae", "meropenem"): 0.22,
    ("klebsiella_pneumoniae", "ertapenem"): 0.26,
    ("klebsiella_pneumoniae", "ceftriaxone"): 0.14,
    ("klebsiella_pneumoniae", "piperacillin_tazobactam"): 0.12,
    ("e_coli", "ceftriaxone"): 0.10,
    ("e_coli", "ciprofloxacin"): 0.09,
    ("acinetobacter_baumannii", "meropenem"): 0.12,
    ("acinetobacter_baumannii", "colistin"): 0.07,
    ("pseudomonas_aeruginosa", "meropenem"): 0.10,
    ("enterococcus", "vancomycin"): 0.16,
    ("enterococcus", "teicoplanin"): 0.14,
    ("serratia_marcescens", "meropenem"): 0.08,
    ("mrsa", "clindamycin"): -0.08,
    ("e_coli", "meropenem"): 0.01,
}

# Endemic incidence, expressed as expected isolates per 30 days.
ENDEMIC_RATE = {
    "e_coli": 16, "klebsiella_pneumoniae": 8, "mrsa": 5, "staph_aureus": 6,
    "enterococcus": 6, "pseudomonas_aeruginosa": 6, "proteus_mirabilis": 5,
    "clostridioides_difficile": 5, "streptococcus": 5, "salmonella": 3,
    "serratia_marcescens": 2, "acinetobacter_baumannii": 3,
}

ORGANISM_LABEL_STYLE = {
    "e_coli": ["Escherichia coli", "E. coli", "ESCHERICHIA COLI", "E. coli (ESBL)"],
    "klebsiella_pneumoniae": ["Klebsiella pneumoniae", "KLEBSIELLA PNEUMONIAE",
                              "K. pneumoniae", "Klebsiella spp."],
    "mrsa": ["MRSA", "Staphylococcus aureus (MRSA)",
             "Methicillin-resistant Staphylococcus aureus"],
    "staph_aureus": ["Staphylococcus aureus", "S. aureus", "STAPHYLOCOCCUS AUREUS"],
    "enterococcus": ["Enterococcus faecium", "Enterococcus faecalis", "Enterococcus spp.",
                     "E. faecium"],
    "pseudomonas_aeruginosa": ["Pseudomonas aeruginosa", "P. aeruginosa",
                               "PSEUDOMONAS AERUGINOSA"],
    "acinetobacter_baumannii": ["Acinetobacter baumannii", "A. baumannii",
                                "Acinetobacter calcoaceticus-baumannii complex"],
    "salmonella": ["Salmonella Enteritidis", "Salmonella spp.", "Salmonella Typhimurium"],
    "streptococcus": ["Streptococcus pneumoniae", "Streptococcus pyogenes",
                      "Group B Streptococcus", "Streptococcus agalactiae"],
    "clostridioides_difficile": ["Clostridioides difficile (toxin positive)",
                                 "Clostridium difficile", "C. difficile"],
    "proteus_mirabilis": ["Proteus mirabilis", "P. mirabilis", "PROTEUS MIRABILIS"],
    "serratia_marcescens": ["Serratia marcescens", "S. marcescens", "SER RATIA"],
}


@dataclass
class Cluster:
    """An intentionally seeded transmission event."""
    pathogen: str
    start_day: int          # days from the end of the window (0 == today)
    duration: int
    patients: int
    wards: list[str]
    resistance_boost: float = 0.0
    label: str = ""


# Days are counted backwards from "today" so the demo always looks current.
# The scenario is deliberately mixed: a handful of genuine high-priority
# transmissions, several moderate signals, and a few events that stay at the
# organism's endemic level so the risk engine is shown discriminating, not
# simply calling everything red.
CLUSTERS = [
    Cluster("mrsa", 0, 18, 9, ["ICU", "Ward 12"], 0.05,
            "MRSA transmission, ICU cohort"),
    Cluster("klebsiella_pneumoniae", 1, 25, 7, ["ICU", "Surgical Ward"], 0.25,
            "Carbapenem-resistant K. pneumoniae"),
    Cluster("pseudomonas_aeruginosa", 5, 20, 6, ["NICU"], 0.15,
            "P. aeruginosa sink/environmental cluster"),
    Cluster("acinetobacter_baumannii", 3, 22, 5, ["ICU", "Renal Dialysis"], 0.10,
            "XDR A. baumannii"),
    Cluster("serratia_marcescens", 9, 18, 3, ["NICU", "Ward 12"], 0.10,
            "Serratia contaminated flushes"),
    Cluster("salmonella", 6, 10, 5, ["Outpatient", "Emergency Department", "Ward 3"],
            0.0, "Common-source foodborne event"),
    Cluster("clostridioides_difficile", 2, 16, 3, ["Ward 8", "Geriatrics"], 0.0,
            "C. difficile toxin-positive cluster"),
    Cluster("enterococcus", 8, 26, 3, ["Oncology", "Ward 5"], 0.05, "VRE emergence"),
    Cluster("staph_aureus", 15, 22, 3, ["Ward 3", "Surgical Ward"], 0.0, "MSSA surgical-site"),
    Cluster("streptococcus", 20, 24, 2, ["Geriatrics"], 0.0,
            "Two linked invasive GAS cases in one nursing wing"),
    Cluster("e_coli", 25, 40, 4, ["Renal Dialysis", "Ward 8"], 0.10,
            "ESBL E. coli endemic rise"),
    Cluster("proteus_mirabilis", 12, 20, 2, ["Ward 5", "Geriatrics"], 0.0,
            "Catheter-associated UTI, background level"),
]


def _ward_weights() -> tuple[list[str], list[int]]:
    names = [w for w, _ in WARDS]
    weights = [n for _, n in WARDS]
    return names, weights


def build_records(as_of: date | None = None) -> list[LabRecord]:
    """Generate the full demonstration dataset, newest scenarios included."""
    today = as_of or date.today()
    rnd = random.Random(SEED)
    ward_names, ward_weights = _ward_weights()
    patient_counter = {"n": 1200}
    used_by_patient: dict[str, set] = {}

    def new_patient() -> str:
        patient_counter["n"] += rnd.randint(1, 4)
        return normalise_patient(str(patient_counter["n"]))

    def resistance_prob(pathogen: str, drug: str, t: float, boost: float) -> float:
        base = BASE_RESISTANCE.get((pathogen, drug))
        if base is None:
            p = PATHOGENS[pathogen]
            base = 0.25 if drug in p.intrinsic else 0.10
        base += AMR_TRENDS.get((pathogen, drug), 0.0) * t
        intrinsic = drug in PATHOGENS[pathogen].intrinsic
        prob = base + boost * (0.0 if intrinsic else 1.0)
        return max(0.0, min(0.99, prob))

    def make_antibiogram(pathogen: str, day_index: int, boost: float,
                         clone_from: Sensitivity | None) -> list[Sensitivity]:
        t = day_index / max(1, HISTORY_DAYS - 1)
        out: list[Sensitivity] = []
        for drug in DEFAULT_PANELS.get(pathogen, []):
            if rnd.random() < 0.06 and drug not in ("oxacillin", "ampicillin"):
                continue                      # labs skip some agents occasionally
            if clone_from is not None and clone_from.antibiotic == drug:
                result = clone_from.result    # outbreaks shed identical antibiograms
            else:
                p = resistance_prob(pathogen, drug, t, boost)
                roll = rnd.random()
                if roll < p:
                    result = "R"
                elif roll < p + 0.05:
                    result = "I"
                else:
                    result = "S"
            out.append(Sensitivity(
                antibiotic_raw=ANTIBIOTICS[drug].name, antibiotic=drug,
                category=ANTIBIOTICS[drug].category, result=result,
                mic=_fake_mic(drug, result, rnd)))
        return out

    records: list[LabRecord] = []

    # ---- endemic background -------------------------------------------
    # Placement is stratified rather than uniformly random: a steady baseline is
    # what a hospital actually reports month to month, and it means the seeded
    # clusters - not sampling noise - decide which pathogen reads as an outbreak.
    for pathogen, rate in ENDEMIC_RATE.items():
        total = int(round(rate * HISTORY_DAYS / 30.0))
        stride = (HISTORY_DAYS - 1) / max(1, total)
        for n in range(total):
            jitter = rnd.uniform(-0.4, 0.4) * stride
            day_index = int(round(n * stride + jitter))
            day_index = min(HISTORY_DAYS - 1, max(0, day_index))
            when = today - timedelta(days=HISTORY_DAYS - 1 - day_index)
            pid = new_patient()
            rec = LabRecord(
                patient_id=pid,
                patient_name=f"{rnd.choice('ABCDEFGHJKLMNPRSTVWY')}{rnd.choice('ABCDEFGHJKLMNPRSTVWY')}•••",
                ward=rnd.choices(ward_names, weights=ward_weights)[0],
                specimen_type=rnd.choice(SPECIMENS_BY_PATHOGEN[pathogen]),
                sample_date=when.isoformat(),
                organism_raw=rnd.choice(ORGANISM_LABEL_STYLE[pathogen]),
                pathogen=pathogen, confidence=0.99, match_method="alias",
                result_flag="positive",
            )
            rec.sensitivities = make_antibiogram(pathogen, day_index, 0.0, None)
            records.append(rec)

    # ---- seeded outbreaks ----------------------------------------------
    for cluster in CLUSTERS:
        pathogen = cluster.pathogen
        # Clone a representative antibiogram so clusters look clonal.
        prototype = make_antibiogram(pathogen,
                                     HISTORY_DAYS - cluster.start_day,
                                     cluster.resistance_boost, None)
        proto_by_drug = {s.antibiotic: s for s in prototype}
        for n in range(cluster.patients):
            day_index = (HISTORY_DAYS - 1 - cluster.start_day) + rnd.randint(
                0, max(0, cluster.duration - 1))
            day_index = min(HISTORY_DAYS - 1, max(0, day_index))
            when = today - timedelta(days=HISTORY_DAYS - 1 - day_index)
            ward = cluster.wards[n % len(cluster.wards)]
            pid = new_patient()
            # a proportion of the cluster is also screened/replicated on a
            # second specimen, mirroring real repeat testing
            specimens = SPECIMENS_BY_PATHOGEN[pathogen]
            sens: list[Sensitivity] = []
            for s in prototype:
                if rnd.random() < 0.85:
                    clone = Sensitivity(antibiotic_raw=s.antibiotic_raw,
                                        antibiotic=s.antibiotic, category=s.category,
                                        result=s.result, mic=s.mic)
                    if rnd.random() < 0.12:
                        clone.result = rnd.choice(["S", "I", "R"])
                    sens.append(clone)
            rec = LabRecord(
                patient_id=pid,
                patient_name=f"{rnd.choice('ABCDEFGHJKLMNPRSTVWY')}{rnd.choice('ABCDEFGHJKLMNPRSTVWY')}•••",
                ward=ward,
                specimen_type=rnd.choice(specimens),
                sample_date=when.isoformat(),
                organism_raw=rnd.choice(ORGANISM_LABEL_STYLE[pathogen]),
                pathogen=pathogen, confidence=0.99, match_method="alias",
                result_flag="positive",
            )
            rec.sensitivities = sens
            if "mrsa" == pathogen:
                rec.markers = ["MRSA"]
            records.append(rec)
        _ = proto_by_drug

    # ---- a few negatives and non-target organisms, for realism ---------
    for _ in range(26):
        day_index = rnd.randint(0, HISTORY_DAYS - 1)
        when = today - timedelta(days=HISTORY_DAYS - 1 - day_index)
        rec = LabRecord(
            patient_id=new_patient(), ward=rnd.choices(ward_names, weights=ward_weights)[0],
            specimen_type=rnd.choice(["Blood", "Urine", "Wound", "Sputum"]),
            sample_date=when.isoformat(),
            organism_raw=rnd.choice(["Coagulase-negative Staphylococcus",
                                     "Staphylococcus epidermidis", "Candida albicans",
                                     "Mixed growth", "No growth", "Enterococcus spp. "
                                     "(screen negative)"]),
            pathogen="", match_method="nontarget", suppressed=True,
            result_flag="negative", confidence=0.95,
            other_label="Non-target organism - excluded from surveillance",
        )
        records.append(rec)

    records.sort(key=lambda r: (r.sample_date, r.patient_id))
    return records


_MIC_LADDER = ["<=0.06", "0.12", "0.25", "0.5", "1", "2", "4", "8", "16", "32",
               ">=64", ">=128", ">=256"]


def _fake_mic(drug: str, result: str, rnd: random.Random) -> str:
    if result == "R":
        return rnd.choice(_MIC_LADDER[7:])
    if result == "I":
        return rnd.choice(_MIC_LADDER[5:9])
    return rnd.choice(_MIC_LADDER[:6])


def scenario_summary() -> list[dict]:
    """Human-readable description of what the demo dataset contains."""
    return [{
        "pathogen": PATHOGENS[c.pathogen].short_name,
        "label": c.label,
        "patients": c.patients,
        "wards": ", ".join(c.wards),
        "window_days": f"T-{c.start_day} to T-{max(0, c.start_day - c.duration)}",
    } for c in CLUSTERS]
