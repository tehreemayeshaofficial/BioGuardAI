"""
Antimicrobial agent knowledge base.

Contains (a) a canonical list of agents routinely reported by clinical
microbiology laboratories, (b) spelling/abbreviation aliases seen in lab
information system exports, (c) the WHO-derived antimicrobial *categories* used
for MDR/XDR/PDR counting, and (d) helper resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .textutil import normalise


# --------------------------------------------------------------------------
# Categories (used for MDR / XDR definitions)
# --------------------------------------------------------------------------
CATEGORIES = {
    "penicillin": "Penicillins",
    "betalactam_inhibitor": "Beta-lactam/beta-lactamase inhibitor combinations",
    "cephalosporin": "Cephalosporins",
    "monobactam": "Monobactams",
    "carbapenem": "Carbapenems",
    "cephalosporin_inhibitor": "Cephalosporin/beta-lactamase inhibitor combinations",
    "fluoroquinolone": "Fluoroquinolones",
    "aminoglycoside": "Aminoglycosides",
    "folate_antagonist": "Folate-pathway inhibitors",
    "tetracycline": "Tetracyclines",
    "glycylcycline": "Glycylcyclines",
    "fluorocycline": "Fluorocyclines",
    "macrolide": "Macrolides",
    "lincosamide": "Lincosamides",
    "streptogramin": "Streptogramins",
    "glycopeptide": "Glycopeptides",
    "lipoglycopeptide": "Lipoglycopeptides",
    "oxazolidinone": "Oxazolidinones",
    "lipopeptide": "Lipopeptides",
    "polymyxin": "Polymyxins",
    "nitroimidazole": "Nitroimidazoles",
    "nitrofuran": "Nitrofurans",
    "phosphonic_acid": "Phosphonic acids (fosfomycin)",
    "rifamycin": "Rifamycins",
    "phenicol": "Phenicols",
    "pleuromutilin": "Pleuromutilins",
    "siderophore_cephalosporin": "Siderophore cephalosporins",
    "other": "Other agents",
}

# Categories that are generally NOT available for Gram-negative rescue therapy,
# used when reasoning about XDR ("all but <= 2 categories have an option left").
GRAM_POSITIVE_ONLY = {
    "glycopeptide", "lipoglycopeptide", "oxazolidinone", "lipopeptide",
    "streptogramin", "nitroimidazole",
}


@dataclass(frozen=True)
class Antibiotic:
    key: str
    name: str
    category: str
    aliases: tuple[str, ...] = field(default=())
    # Oral/IV, informational only
    class_name: str = ""


def A(key, name, category, aliases=(), class_name=""):
    return Antibiotic(key, name, category, tuple(aliases), class_name)


ANTIBIOTICS: dict[str, Antibiotic] = {}

_DEFS: list[Antibiotic] = [
    # --- Penicillins ---
    A("penicillin_g", "Penicillin G", "penicillin", ["penicillin", "benzylpenicillin", "pen g", "pg", "penicillin g"], "Penicillin"),
    A("ampicillin", "Ampicillin", "penicillin", ["ampicillin", "amp", "pipercillin"], "Aminopenicillin"),
    A("amoxicillin", "Amoxicillin", "penicillin", ["amoxicillin", "amx", "amox", "amoxicilline"], "Aminopenicillin"),
    A("methicillin", "Methicillin", "penicillin", ["methicillin"], "Isoxazolyl"),
    A("oxacillin", "Oxacillin", "penicillin", ["oxacillin", "oxa"], "Isoxazolyl"),
    A("cloxacillin", "Cloxacillin", "penicillin", ["cloxacillin", "cx"], "Isoxazolyl"),
    A("flucloxacillin", "Flucloxacillin", "penicillin", ["flucloxacillin", "flx"], "Isoxazolyl"),
    A("nafcillin", "Nafcillin", "penicillin", ["nafcillin"], "Isoxazolyl"),
    A("piperacillin", "Piperacillin", "penicillin", ["piperacillin", "pip", "piperacilline"], "Ureidopenicillin"),
    A("ticarcillin", "Ticarcillin", "penicillin", ["ticarcillin", "tic"], "Carboxypenicillin"),
    A("mecillinam", "Mecillinam", "penicillin", ["mecillinam", "pivmecillinam"], "Aminopenicillin"),

    # --- Beta-lactam/inhibitor combinations ---
    A("amoxicillin_clav", "Amoxicillin-Clavulanate", "betalactam_inhibitor",
      ["amoxicillin clavulanate", "amoxicillin clav", "co amoxiclav", "augmentin", "amc", "amox clav", "clavulanate"], "BL/BLI"),
    A("ampicillin_sulbactam", "Ampicillin-Sulbactam", "betalactam_inhibitor",
      ["ampicillin sulbactam", "sulbactam", "unasyn", "ams"], "BL/BLI"),
    A("piperacillin_tazobactam", "Piperacillin-Tazobactam", "betalactam_inhibitor",
      ["piperacillin tazobactam", "pip tazobactam", "tazocin", "tazobactam", "ptz", "tzp", "pip tazo", "zosyn"], "BL/BLI"),
    A("ticarcillin_clav", "Ticarcillin-Clavulanate", "betalactam_inhibitor",
      ["ticarcillin clavulanate", "ticarcillin clav", "timentin"], "BL/BLI"),
    A("ceftolozane_tazobactam", "Ceftolozane-Tazobactam", "cephalosporin_inhibitor",
      ["ceftolozane tazobactam", "ceftolozane", "tzp ceftolozane"], "Ceph/BLI"),
    A("ceftazidime_avibactam", "Ceftazidime-Avibactam", "cephalosporin_inhibitor",
      ["ceftazidime avibactam", "avibactam", "ceftaz avb", "zavicefta"], "Ceph/BLI"),
    A("meropenem_vaborbactam", "Meropenem-Vaborbactam", "carbapenem",
      ["meropenem vaborbactam", "vaborbactam"], "Carbapenem/BLI"),
    A("imipenem_relebactam", "Imipenem-Relebactam", "carbapenem",
      ["imipenem relebactam", "relebactam"], "Carbapenem/BLI"),

    # --- Cephalosporins ---
    A("cefazolin", "Cefazolin", "cephalosporin", ["cefazolin", "cz", "cefazoline"], "1st gen"),
    A("cephalexin", "Cefalexin", "cephalosporin", ["cephalexin", "cefalexin", "cph"], "1st gen"),
    A("cefaclor", "Cefaclor", "cephalosporin", ["cefaclor"], "2nd gen"),
    A("cefuroxime", "Cefuroxime", "cephalosporin", ["cefuroxime", "cxm", "cefurox"], "2nd gen"),
    A("cefotetan", "Cefotetan", "cephalosporin", ["cefotetan", "otm", "cefotetame"], "2nd gen"),
    A("cefoxitin", "Cefoxitin", "cephalosporin", ["cefoxitin", "fox", "cefoxitine"], "Cephamycin"),
    A("ceftriaxone", "Ceftriaxone", "cephalosporin", ["ceftriaxone", "cro", "cefriaxone", "rocephin"], "3rd gen"),
    A("cefotaxime", "Cefotaxime", "cephalosporin", ["cefotaxime", "ctx", "cefotax"], "3rd gen"),
    A("ceftazidime", "Ceftazidime", "cephalosporin", ["ceftazidime", "caz", "fortum", "ceftaz"], "3rd gen"),
    A("ceftaroline", "Ceftaroline", "cephalosporin", ["ceftaroline", "ceftarosyl", "tseflin", "ceft"], "5th gen"),
    A("cefpodoxime", "Cefpodoxime", "cephalosporin", ["cefpodoxime", "cpd"], "3rd gen (oral)"),
    A("cefixime", "Cefixime", "cephalosporin", ["cefixime", "cfm"], "3rd gen (oral)"),
    A("cefepime", "Cefepime", "cephalosporin", ["cefepime", "fep", "maxipime", "cefipime"], "4th gen"),

    # --- Monobactams / carbapenems / siderophore ---
    A("aztreonam", "Aztreonam", "monobactam", ["aztreonam", "atz", "aztreonam heptadism"], "Monobactam"),
    A("meropenem", "Meropenem", "carbapenem", ["meropenem", "mem", "mropenem", "meronem", "meropenam"], "Carbapenem"),
    A("ertapenem", "Ertapenem", "carbapenem", ["ertapenem", "ert", "invanz"], "Carbapenem"),
    A("imipenem", "Imipenem", "carbapenem", ["imipenem", "ipm", "imipenem cilastatin", "cilastatin", "zolysin"], "Carbapenem"),
    A("doripenem", "Doripenem", "carbapenem", ["doripenem", "doripene", "finibax"], "Carbapenem"),
    A("cefiderocol", "Cefiderocol", "siderophore_cephalosporin", ["cefiderocol", "feti"], "Siderophore cephalosporin"),

    # --- Fluoroquinolones ---
    A("ciprofloxacin", "Ciprofloxacin", "fluoroquinolone", ["ciprofloxacin", "cip", "cipro", "cn"], "Fluoroquinolone"),
    A("levofloxacin", "Levofloxacin", "fluoroquinolone", ["levofloxacin", "lev", "levo", "tavanic"], "Fluoroquinolone"),
    A("moxifloxacin", "Moxifloxacin", "fluoroquinolone", ["moxifloxacin", "mfx", "vigaux"], "Fluoroquinolone"),
    A("ofloxacin", "Ofloxacin", "fluoroquinolone", ["ofloxacin", "ofl"], "Fluoroquinolone"),
    A("norfloxacin", "Norfloxacin", "fluoroquinolone", ["norfloxacin", "nfc"], "Fluoroquinolone"),
    A("nalidixic_acid", "Nalidixic Acid", "fluoroquinolone", ["nalidixic acid", "nalidixic", "nal"], "Quinolone"),

    # --- Aminoglycosides ---
    A("gentamicin", "Gentamicin", "aminoglycoside", ["gentamicin", "gen", "gm", "gentalline"], "Aminoglycoside"),
    A("tobramycin", "Tobramycin", "aminoglycoside", ["tobramycin", "tob", "nebracin"], "Aminoglycoside"),
    A("amikacin", "Amikacin", "aminoglycoside", ["amikacin", "amk", "akin", "amikacine"], "Aminoglycoside"),
    A("netilmicin", "Netilmicin", "aminoglycoside", ["netilmicin", "net"], "Aminoglycoside"),
    A("streptomycin", "Streptomycin", "aminoglycoside", ["streptomycin", "stra"], "Aminoglycoside"),
    A("spectinomycin", "Spectinomycin", "aminoglycoside", ["spectinomycin", "sp"], "Aminoglycoside"),
    A("plazomicin", "Plazomicin", "aminoglycoside", ["plazomicin", "pla"], "Aminoglycoside"),

    # --- Folate, tetracycline family ---
    A("trimethoprim", "Trimethoprim", "folate_antagonist", ["trimethoprim", "trim"], "Folate antagonist"),
    A("sulfamethoxazole", "Sulfamethoxazole", "folate_antagonist",
      ["sulfamethoxazole", "sulphamethoxazole", "sulfameth", "sulphameth"], "Sulfonamide"),
    A("cotrimoxazole", "Trimethoprim-Sulfamethoxazole", "folate_antagonist",
      ["cotrimoxazole", "co trimoxazole", "trimethoprim sulfamethoxazole", "tmp smx", "sxt", "bactrim", "septrin", "cotrim"], "Folate antagonist"),
    A("dapsone", "Dapsone", "folate_antagonist", ["dapsone"], "Sulfone"),
    A("tetracycline", "Tetracycline", "tetracycline", ["tetracycline", "tet", "tcy"], "Tetracycline"),
    A("doxycycline", "Doxycycline", "tetracycline", ["doxycycline", "dox", "monodox"], "Tetracycline"),
    A("minocycline", "Minocycline", "tetracycline", ["minocycline", "mini", "mnc"], "Tetracycline"),
    A("tigecycline", "Tigecycline", "glycylcycline", ["tigecycline", "cycl", "tygacil", "tgc"], "Glycylcycline"),
    A("eravacycline", "Eravacycline", "fluorocycline", ["eravacycline", "erv"], "Fluorocycline"),

    # --- Gram-positive agents ---
    A("vancomycin", "Vancomycin", "glycopeptide", ["vancomycin", "van", "va", "vanco", "vancomycine"], "Glycopeptide"),
    A("teicoplanin", "Teicoplanin", "glycopeptide", ["teicoplanin", "tec", "targocid", "teicoplanine"], "Glycopeptide"),
    A("telavancin", "Telavancin", "lipoglycopeptide", ["telavancin", "tlv"], "Lipoglycopeptide"),
    A("dalbavancin", "Dalbavancin", "lipoglycopeptide", ["dalbavancin", "dbv"], "Lipoglycopeptide"),
    A("oritavancin", "Oritavancin", "lipoglycopeptide", ["oritavancin", "orv"], "Lipoglycopeptide"),
    A("bacitracin", "Bacitracin", "other", ["bacitracin", "b"], "Polypeptide"),
    A("linezolid", "Linezolid", "oxazolidinone", ["linezolid", "lnz", "zyvoxid", "olid"], "Oxazolidinone"),
    A("tedizolid", "Tedizolid", "oxazolidinone", ["tedizolid", "tdz"], "Oxazolidinone"),
    A("daptomycin", "Daptomycin", "lipopeptide", ["daptomycin", "dap", "cubicin"], "Lipopeptide"),
    A("quinupristin_dalfopristin", "Quinupristin-Dalfopristin", "streptogramin",
      ["quinupristin dalfopristin", "synercid", "qa dp"], "Streptogramin"),
    A("pristinamycin", "Pristinamycin", "streptogramin", ["pristinamycin", "pr"], "Streptogramin"),
    A("rifampicin", "Rifampicin", "rifamycin", ["rifampicin", "rifampin", "rif", "rdi"], "Rifamycin"),
    A("fusidic_acid", "Fusidic Acid", "other", ["fusidic acid", "fus", "fucidin"], "Steroid antibacterial"),
    A("chloramphenicol", "Chloramphenicol", "phenicol", ["chloramphenicol", "c"], "Phenicol"),

    # --- Gram-negative / urinary / anaerobic rescue ---
    A("colistin", "Colistin", "polymyxin", ["colistin", "polymyxin", "polymyxin e", "col"], "Polymyxin"),
    A("polymyxin_b", "Polymyxin B", "polymyxin", ["polymyxin b", "polymyxin b sulfate", "pmb"], "Polymyxin"),
    A("nitrofurantoin", "Nitrofurantoin", "nitrofuran", ["nitrofurantoin", "nit", "ft", "furadantin", "macrodantin"], "Nitrofuran"),
    A("fosfomycin", "Fosfomycin", "phosphonic_acid", ["fosfomycin", "fos", "monurol"], "Phosphonic acid"),
    A("metronidazole", "Metronidazole", "nitroimidazole", ["metronidazole", "metro", "flagyl", "klion"], "Nitroimidazole"),
    A("fidaxomicin", "Fidaxomicin", "pleuromutilin", ["fidaxomicin", "fdn", "dificlir"], "Macrocycle"),
    A("clindamycin", "Clindamycin", "lincosamide", ["clindamycin", "clinda", "da", "clidac"], "Lincosamide"),
    A("erythromycin", "Erythromycin", "macrolide", ["erythromycin", "er", "erythro"], "Macrolide"),
    A("azithromycin", "Azithromycin", "macrolide", ["azithromycin", "azi", "azm"], "Macrolide"),
    A("clarithromycin", "Clarithromycin", "macrolide", ["clarithromycin", "cla"], "Macrolide"),
    A("spiramycin", "Spiramycin", "macrolide", ["spiramycin", "spi"], "Macrolide"),

    # --- Miscellaneous frequently-seen agents ---
    A("ceftazidime_avi", "Ceftazidime-Avibactam", "cephalosporin_inhibitor", ["caz avi", "ceftazidimeavibactam"], "Ceph/BLI"),
    A("imipenem_cilastatin", "Imipenem-Cilastatin", "carbapenem", ["imipenem cilastatin", "ipi"], "Carbapenem"),
    A("cefepime_enol", "Cefepime-Enol", "cephalosporin", ["cefepime enol", "wcke"], "4th gen"),
    A("aminopenicillic", "Aminopenicillic acid", "other", ["6 apa", "6 aminopenicillanic acid"], "Other"),
]

for _ab in _DEFS:
    ANTIBIOTICS.setdefault(_ab.key, _ab)


# Agents routinely used as "last-line" options, per Gram-stain group.
LAST_LINE_GRAM_NEGATIVE = {"colistin", "polymyxin_b", "tigecycline", "eravacycline",
                           "cefiderocol", "meropenem", "imipenem", "ertapenem",
                           "amikacin", "ceftazidime_avibactam", "ceftolozane_tazobactam",
                           "meropenem_vaborbactam", "imipenem_relebactam", "plazomicin"}
LAST_LINE_GRAM_POSITIVE = {"vancomycin", "teicoplanin", "linezolid", "tedizolid",
                           "daptomycin", "ceftaroline", "quinupristin_dalfopristin",
                           "tigecycline", "telavancin", "dalbavancin", "oritavancin",
                           "fidaxomicin", "pristinamycin"}

# --------------------------------------------------------------------------
# Alias index (built once, lazily)
# --------------------------------------------------------------------------
_ALIAS_INDEX: dict[str, str] | None = None


def _norm_key(text: str) -> str:
    return normalise(text)


def _alias_index() -> dict[str, str]:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        idx: dict[str, str] = {}
        for ab in ANTIBIOTICS.values():
            keys = {normalise(ab.name), normalise(ab.key.replace("_", " "))}
            keys.update(normalise(a) for a in ab.aliases)
            for k in keys:
                if len(k) >= 2:
                    # Longest alias wins; never overwrite a longer existing entry.
                    if k not in idx or len(k) > len(idx[k]):
                        idx[k] = ab.key
        # sorted descending by length for greedy phrase matching
        _ALIAS_INDEX = dict(sorted(idx.items(), key=lambda kv: -len(kv[0])))
    return _ALIAS_INDEX


def resolve_antibiotic(raw: str) -> str | None:
    """Map a free-text antibiotic label / disc abbreviation to a canonical key.

    Returns ``None`` when the token does not look like a known agent, so that
    callers can safely ignore unrelated CSV columns.
    """
    if not raw:
        return None
    text = normalise(raw)
    if not text or len(text) < 2:
        return None

    idx = _alias_index()
    exact = idx.get(text)
    if exact:
        return exact

    # Column headers frequently embed bracketed disc codes or dosages, e.g.
    # "Ceftazidime (CAZ 30)" or "VANCOMYCIN 30ug".
    for alias, key in idx.items():
        if len(alias) < 3:
            continue
        if _is_word_contained(text, alias):
            return key
    return None


def _is_word_contained(text: str, phrase: str) -> bool:
    import re
    pattern = r"(?:^|[\s_/\(\),;-])(?:" + re.escape(phrase) + r")(?:$|[\s_/\(\),;.-]|\d)"
    return re.search(pattern, text) is not None


def category_of(antibiotic_key: str) -> str:
    ab = ANTIBIOTICS.get(antibiotic_key)
    return ab.category if ab else "other"


def display_name(antibiotic_key: str) -> str:
    ab = ANTIBIOTICS.get(antibiotic_key)
    return ab.name if ab else (antibiotic_key or "").replace("_", " ").title()


def category_display(category: str) -> str:
    return CATEGORIES.get(category, category.replace("_", " ").title())


# Sensitivity interpretations that count as "non-susceptible" for MDR counting.
NON_SUSCEPTIBLE = {"R", "I"}
