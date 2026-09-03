"""
Target-pathogen knowledge base for Bioguard AI.

The twelve organisms below are the surveillance targets required by the
specification. Each carries the metadata the engines need:

* ``aliases``        - every spelling/abbreviation seen in lab exports
* ``sibling_species`` - same-genus organisms that must NOT be absorbed into the
                        target (e.g. *Klebsiella oxytoca* is not *K. pneumoniae*)
* ``intrinsic``      - agents the species is intrinsically non-susceptible to;
                        resistance to these is expected and is therefore
                        excluded from acquired-AMR trend statistics
* ``critical_drugs`` - loss of susceptibility here is a clinical red flag
* ``severity`` / ``transmissibility`` - drive the outbreak risk score
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pathogen:
    key: str
    name: str                      # full binomial, for display
    short_name: str                # e.g. "E. coli"
    group: str                     # epidemiological grouping
    gram: str
    aliases: tuple[str, ...] = field(default=())
    sibling_species: tuple[str, ...] = field(default=())
    intrinsic: frozenset[str] = frozenset()
    critical_drugs: frozenset[str] = frozenset()
    severity: float = 0.6          # 0..1  case-fatality / clinical impact
    transmissibility: float = 0.5  # 0..1  patient-to-patient / environmental spread
    contact_precautions: str = "Standard"
    reservoir: str = ""
    common_syndromes: tuple[str, ...] = ()
    alert_text: str = ""
    parent: str | None = None      # phenotype -> species link (MRSA -> S. aureus)
    phenotype: bool = False        # True for resistance phenotypes, not species
    # MDRO = requires contact isolation / cohort management when transmitted.
    mdro: bool = False
    # Notifiable: an epidemiological link between cases obliges public-health action.
    public_health: bool = False
    color: str = "#38bdf8"

    # ---- derived helpers -------------------------------------------------
    @property
    def threat(self) -> float:
        """Composite intrinsic outbreak potential (0..1)."""
        return round(min(1.0, 0.62 * self.severity + 0.38 * self.transmissibility), 3)

    @property
    def display(self) -> str:
        return self.short_name if self.short_name else self.name


def _P(*, intrinsic=(), critical=(), aliases=(), siblings=(), syndromes=(), **kw) -> Pathogen:
    return Pathogen(
        aliases=tuple(aliases),
        sibling_species=tuple(siblings),
        intrinsic=frozenset(intrinsic),
        critical_drugs=frozenset(critical),
        common_syndromes=tuple(syndromes),
        **kw,
    )


# ==========================================================================
# The twelve required surveillance targets
# ==========================================================================
PATHOGENS: dict[str, Pathogen] = {}


def _reg(p: Pathogen) -> Pathogen:
    PATHOGENS[p.key] = p
    return p


_reg(_P(
    key="e_coli",
    name="Escherichia coli",
    short_name="E. coli",
    group="Gram-negative Enterobacterales",
    gram="negative",
    color="#38bdf8",
    aliases=["escherichia coli", "escherichia", "e coli", "ecoli", "esch coli",
             "esco", "coli"],
    siblings=["escherichia fergusonii"],
    critical=["ceftriaxone", "cefotaxime", "ceftazidime", "meropenem", "ertapenem",
              "imipenem", "ciprofloxacin", "levofloxacin", "gentamicin", "amikacin",
              "piperacillin_tazobactam", "cotrimoxazole"],
    intrinsic=["ticarcillin"],
    severity=0.72, transmissibility=0.68,
    contact_precautions="Contact + enteric",
    reservoir="Endogenous gut flora; food/water contamination",
    syndromes=["Urinary tract infection", "Bloodstream infection",
               "Intra-abdominal sepsis", "Neonatal meningitis", "Diarrhoea"],
    alert_text="ESBL and fluoroquinolone resistance are the dominant drivers; "
               "watch for carbapenem non-susceptibility (CRE).",
))

_reg(_P(
    key="mrsa",
    name="Methicillin-resistant Staphylococcus aureus",
    short_name="MRSA",
    group="Gram-positive cocci",
    gram="positive",
    color="#ef4444",
    phenotype=True,
    parent="staph_aureus",
    aliases=["mrsa", "methicillin resistant staphylococcus aureus",
             "meticillin resistant staphylococcus aureus",
             "oxacillin resistant staphylococcus aureus",
             "methicillin resistant staph aureus", "mr sa", "heterogeneous mrsa",
             "staphylococcus aureus mrsa", "srsa", "ha mrsa", "ca mrsa"],
    siblings=[],
    critical=["vancomycin", "teicoplanin", "linezolid", "daptomycin", "ceftaroline",
              "clindamycin", "cotrimoxazole", "rifampicin"],
    intrinsic=["ampicillin", "amoxicillin", "amoxicillin_clav", "piperacillin",
               "piperacillin_tazobactam", "cefazolin", "cefuroxime", "ceftriaxone",
               "cefotaxime", "ceftazidime", "cefepime", "cephalexin", "aztreonam",
               "mecillinam", "ticarcillin", "ticarcillin_clav", "penicillin_g",
               "sulbactam", "ampicillin_sulbactam"],
    severity=0.92, transmissibility=0.9,
    mdro=True,
    contact_precautions="Contact isolation + decolonisation review",
    reservoir="Human skin/nasal carriage; healthcare environment, hands of staff",
    syndromes=["Wound & surgical-site infection", "Bloodstream infection",
               "Pneumonia", "Osteomyelitis", "Skin/soft-tissue abscess"],
    alert_text="Contact isolation and screening of close contacts required. "
               "Reduced vancomycin susceptibility (VISA/VRSA) is an emergency.",
))

_reg(_P(
    key="klebsiella_pneumoniae",
    name="Klebsiella pneumoniae",
    short_name="K. pneumoniae",
    group="Gram-negative Enterobacterales",
    gram="negative",
    color="#a78bfa",
    aliases=["klebsiella pneumoniae", "klebsiella", "k pneumoniae", "klebsiella spp",
             "kleb pneumoniae", "kp", "pneumoniae"],
    siblings=["klebsiella oxytoca", "klebsiella aerogenes", "klebsiella variicola",
              "klebsiella planticola", "klebsiella terrigena", "raouella"],
    critical=["meropenem", "ertapenem", "imipenem", "doripenem", "ceftazidime",
              "ceftriaxone", "piperacillin_tazobactam", "amikacin", "colistin",
              "tigecycline", "ceftazidime_avibactam", "cefiderocol"],
    intrinsic=["ampicillin", "amoxicillin", "ticarcillin", "cephalexin"],
    severity=0.9, transmissibility=0.8,
    mdro=True,
    contact_precautions="Contact isolation (MDRO)",
    reservoir="GI tract, hospital sinks, taps, moist surfaces, hands of staff",
    syndromes=["Ventilator-associated pneumonia", "Bloodstream infection",
               "Urinary tract infection", "Liver abscess", "Wound infection"],
    alert_text="Carbapenem-resistant K. pneumoniae (CRKP/KPC/NDM) is a critical-priority "
               "pathogen with mortality above 40% in bloodstream infection.",
))

_reg(_P(
    key="salmonella",
    name="Salmonella spp.",
    short_name="Salmonella",
    group="Gram-negative Enterobacterales",
    gram="negative",
    color="#fbbf24",
    aliases=["salmonella", "salmonella enterica", "salmonella spp", "salmonella typhi",
             "salmonella paratyphi", "salmonella typhimurium", "salmonella enteritidis",
             "non typhi salmonella", "enteric fever salmonella", "nontyphoidal salmonella"],
    siblings=[],
    critical=["ceftriaxone", "azithromycin", "ciprofloxacin", "cotrimoxazole",
              "ampicillin", "meropenem"],
    intrinsic=[],
    severity=0.75, transmissibility=0.85,
    public_health=True,
    contact_precautions="Enteric/contact precautions",
    reservoir="Food and water; poultry, reptiles; chronic biliary carriage",
    syndromes=["Gastroenteritis", "Enteric (typhoid) fever", "Bacteraemia",
               "Reactive arthritis", "Infected vascular graft"],
    alert_text="Common-source outbreak signature: genetically related cases cluster "
               "in time across unrelated wards - trace food service and ward kitchen.",
))

_reg(_P(
    key="pseudomonas_aeruginosa",
    name="Pseudomonas aeruginosa",
    short_name="P. aeruginosa",
    group="Non-fermenting Gram-negative bacilli",
    gram="negative",
    color="#22d3ee",
    aliases=["pseudomonas aeruginosa", "p aeruginosa", "pseudomonas", "aeruginosa",
             "pseudomonas spp", "pyocyanic bacillus"],
    siblings=["pseudomonas putida", "pseudomonas fluorescens", "pseudomonas stutzeri",
              "pseudomonas cepacia", "burkholderia cepacia", "stenotrophomonas",
              "alcigenes"],
    critical=["meropenem", "imipenem", "ceftazidime", "cefepime", "piperacillin_tazobactam",
              "ciprofloxacin", "levofloxacin", "amikacin", "gentamicin", "colistin",
              "ceftolozane_tazobactam", "ceftazidime_avibactam", "cefiderocol"],
    intrinsic=["amoxicillin", "amoxicillin_clav", "ceftriaxone", "cefotaxime",
               "ertapenem", "tetracycline", "doxycycline", "minocycline", "tigecycline",
               "eravacycline", "chloramphenicol", "nitrofurantoin", "trimethoprim",
               "sulfamethoxazole", "cotrimoxazole", "erythromycin", "azithromycin",
               "clarithromycin", "clindamycin", "ampicillin", "cephalexin",
               "cefuroxime", "dapsone"],
    severity=0.88, transmissibility=0.7,
    mdro=True,
    contact_precautions="Contact isolation; environmental source review",
    reservoir="Water and moist hospital environments: sinks, taps, aerators, "
              "respiratory equipment, disinfectant solutions",
    syndromes=["Ventilator-associated pneumonia", "Bloodstream infection",
               "Wound/burn infection", "Urinary tract infection", "Otitis externa"],
    alert_text="Environmental reservoir interrogation is mandatory: identical "
               "antibiograms from multiple patients implies a shared water source.",
))

_reg(_P(
    key="acinetobacter_baumannii",
    name="Acinetobacter baumannii",
    short_name="A. baumannii",
    group="Non-fermenting Gram-negative bacilli",
    gram="negative",
    color="#f472b6",
    aliases=["acinetobacter baumannii", "a baumannii", "acinetobacter", "baumannii",
             "acinetobacter spp", "acinetobacter calcoaceticus baumannii complex",
             "acinetobacter calcoaceticus", "aba"],
    siblings=["acinetobacter lwoffii", "acinetobacter haemolyticus", "acinetobacter junii",
              "acinetobacter radioresistens"],
    critical=["meropenem", "imipenem", "colistin", "polymyxin_b", "tigecycline",
              "ampicillin_sulbactam", "minocycline", "cefiderocol", "amikacin",
              "levofloxacin", "gentamicin", "piperacillin_tazobactam", "cefepime"],
    intrinsic=["ampicillin", "amoxicillin", "ceftriaxone", "aztreonam", "erythromycin"],
    severity=0.95, transmissibility=0.85,
    mdro=True,
    contact_precautions="Contact isolation + single-room/cohorting",
    reservoir="Dry hospital surfaces, bed rails, monitors, keyboards, linen, sinks",
    syndromes=["Ventilator-associated pneumonia", "Bloodstream infection",
               "Wound and burn infection", "Meningitis (post-neurosurgical)",
               "Urinary tract infection"],
    alert_text="CR-AB survives for weeks on dry surfaces and causes explosive ICU "
               "outbreaks; carbapenem resistance rates above 70% are common.",
))

_reg(_P(
    key="enterococcus",
    name="Enterococcus spp.",
    short_name="Enterococcus",
    group="Gram-positive cocci",
    gram="positive",
    color="#fb923c",
    aliases=["enterococcus", "enterococcus faecalis", "enterococcus faecium",
             "e faecalis", "e faecium", "enterococcus spp", "streptococcus faecalis",
             "streptococcus faecium", "group d streptococcus", "enterococcus durans",
             "enterococcus gallinarum", "enterococcus casseliflavus",
             "vancomycin resistant enterococcus", "vre", "enterococcus hirae",
             "faecium", "faecalis"],
    siblings=[],
    critical=["vancomycin", "teicoplanin", "linezolid", "daptomycin", "ampicillin",
              "amoxicillin", "gentamicin", "streptomycin", "quinupristin_dalfopristin",
              "tigecycline"],
    intrinsic=["cefazolin", "cefuroxime", "ceftriaxone", "cefotaxime", "ceftazidime",
               "cefepime", "cephalexin", "cefotetan", "cefoxitin", "cefpodoxime",
               "cefixime", "ceftaroline", "clindamycin", "trimethoprim",
               "sulfamethoxazole", "cotrimoxazole", "ertapenem"],
    severity=0.78, transmissibility=0.72,
    mdro=True,
    contact_precautions="Contact isolation (VRE)",
    reservoir="Normal gut flora; broad-spectrum antibiotic pressure selects VRE",
    syndromes=["Urinary tract infection", "Bloodstream infection", "Intra-abdominal abscess",
               "Endocarditis", "Wound infection"],
    alert_text="VRE persists on fabrics and floors; screen contacts and consider "
               "ward closure where a transmission cluster is confirmed.",
))

_reg(_P(
    key="staph_aureus",
    name="Staphylococcus aureus",
    short_name="S. aureus",
    group="Gram-positive cocci",
    gram="positive",
    color="#f87171",
    aliases=["staphylococcus aureus", "s aureus", "staph aureus", "aureus", "mssa",
             "methicillin sensitive staphylococcus aureus",
             "meticillin sensitive staphylococcus aureus", "staphylococcus sp aureus",
             "coagulase positive staphylococcus"],
    siblings=["staphylococcus epidermidis", "staphylococcus haemolyticus",
              "staphylococcus hominis", "staphylococcus saprophyticus",
              "staphylococcus lugdunensis", "staphylococcus warwicki",
              "coagulase negative staphylococcus", "cons", "staphylococcus capitis",
              "staphylococcus caprae", "staphylococcus simulans",
              "staphylococcus schleiferi"],
    critical=["oxacillin", "cefoxitin", "vancomycin", "teicoplanin", "linezolid",
              "daptomycin", "ceftaroline", "clindamycin"],
    intrinsic=[],
    severity=0.8, transmissibility=0.78,
    contact_precautions="Standard + contact if MSSA wound/blood isolate",
    reservoir="Anterior nares and skin of 20-30% of the population; staff hands",
    syndromes=["Skin and soft-tissue infection", "Bloodstream infection",
               "Endocarditis", "Pneumonia", "Osteomyelitis", "Toxic shock"],
    alert_text="Oxacillin/cefoxitin resistance reclassifies the isolate as MRSA, which "
               "Bioguard tracks as a separate high-priority target.",
))

_reg(_P(
    key="streptococcus",
    name="Streptococcus spp.",
    short_name="Streptococcus",
    group="Gram-positive cocci",
    gram="positive",
    color="#c084fc",
    aliases=["streptococcus", "streptococcus pneumoniae", "streptococcus pyogenes",
             "streptococcus agalactiae", "pneumococcus", "s pneumoniae", "s pyogenes",
             "s agalactiae", "group a streptococcus", "group b streptococcus",
             "group a beta haemolytic streptococcus", "beta haemolytic streptococcus",
             "viridans group streptococcus", "streptococcus milleri",
             "streptococcus anginosus", "streptococcus mitis", "streptococcus suis",
             "pneumococcus pneumoniae", " GAS", "GBS", "streptococcus spp",
             "streptococcus salivarius", "streptococcus constellatus"],
    # "streptococcus faecalis/faecium" are legacy enterococci names and are
    # captured by the enterococcus alias list (longest-match wins).
    siblings=[],
    critical=["penicillin_g", "amoxicillin", "ceftriaxone", "vancomycin",
              "meropenem", "clindamycin", "erythromycin"],
    intrinsic=["gentamicin", "tobramycin", "amikacin", "streptomycin", "netilmicin",
               "plazomicin", "colistin", "polymyxin_b", "nitrofurantoin",
               "trimethoprim", "sulfamethoxazole", "cotrimoxazole"],
    severity=0.7, transmissibility=0.88,
    public_health=True,
    contact_precautions="Droplet (invasive GAS: contact + droplet)",
    reservoir="Human respiratory tract carriage; droplet transmission",
    syndromes=["Pneumonia", "Meningitis", "Pharyngitis", "Cellulitis",
               "Puerperal sepsis", "Invasive GAS soft-tissue infection",
               "Neonatal sepsis"],
    alert_text="Invasive GAS in linked patients implies staff-mediated transmission: "
               "screen theatre and ward staff for pharyngeal/skin carriage.",
))

_reg(_P(
    key="clostridioides_difficile",
    name="Clostridioides (Clostridium) difficile",
    short_name="C. difficile",
    group="Anaerobic Gram-positive bacilli",
    gram="positive",
    color="#4ade80",
    aliases=["clostridium difficile", "clostridioides difficile", "c difficile",
             "clostridium diff", "cdiff", "c diff", "difficile", "cx difficile",
             "cdi", "toxin positive clostridium difficile", "pseudomembranous colitis"],
    siblings=["clostridium perfringens", "clostridium septicum", "clostridium novyi",
              "clostridium sordellii"],
    critical=["vancomycin", "fidaxomicin", "metronidazole"],
    intrinsic=["vancomycin_oral_only_marker"],
    severity=0.8, transmissibility=0.95,
    mdro=True, public_health=True,
    contact_precautions="Contact + soap-and-water hand hygiene, sporicidal cleaning",
    reservoir="Spores on bed rails, commodes, floors, toilets; persists months",
    syndromes=["Antibiotic-associated diarrhoea", "Pseudomembranous colitis",
               "Toxic megacolon", "Recurrent colitis"],
    alert_text="A rise in toxin-positive cases in one ward is an outbreak by "
               "definition - review fluoroquinolone/cephalosporin usage and cleaning.",
))

_reg(_P(
    key="proteus_mirabilis",
    name="Proteus mirabilis",
    short_name="P. mirabilis",
    group="Gram-negative Enterobacterales",
    gram="negative",
    color="#2dd4bf",
    aliases=["proteus mirabilis", "p mirabilis", "proteus", "mirabilis", "proteus spp"],
    siblings=["proteus vulgaris", "proteus penneri", "proteus rettgeri",
              "morganella morganii", "Providencia rettgeri", "providencia stuartii"],
    critical=["meropenem", "ertapenem", "ceftriaxone", "piperacillin_tazobactam",
              "amikacin", "gentamicin", "ciprofloxacin", "cotrimoxazole"],
    intrinsic=["nitrofurantoin", "tetracycline", "doxycycline", "minocycline",
               "tigecycline", "eravacycline", "colistin", "polymyxin_b",
               "erythromycin", "azithromycin", "clarithromycin", "clindamycin",
               "chloramphenicol"],
    severity=0.6, transmissibility=0.5,
    contact_precautions="Standard",
    reservoir="Perineal carriage; catheter-associated biofilm",
    syndromes=["Catheter-associated urinary tract infection", "Wound infection",
               "Bloodstream infection", "Struvite stones", "Otitis media"],
    alert_text="Strong urease producer - alkaline urine and encrusted catheters; "
               "cluster of CAUTI cases should trigger a catheter-care review.",
))

_reg(_P(
    key="serratia_marcescens",
    name="Serratia marcescens",
    short_name="S. marcescens",
    group="Gram-negative Enterobacterales",
    gram="negative",
    color="#818cf8",
    aliases=["serratia marcescens", "serratia", "s marcescens", "marcescens",
             "serratia spp"],
    siblings=["serratia liquefaciens", "serratia fonticola", "serratia plymuthica"],
    critical=["meropenem", "imipenem", "ceftazidime", "cefepime", "ciprofloxacin",
              "amikacin", "gentamicin", "piperacillin_tazobactam", "cotrimoxazole",
              "tigecycline", "colistin"],
    intrinsic=["ampicillin", "amoxicillin", "amoxicillin_clav", "cefuroxime",
               "cephalexin", "cefazolin", "nitrofurantoin", "tetracycline",
               "doxycycline", "colistin", "polymyxin_b", "erythromycin",
               "clindamycin", "ticarcillin"],
    severity=0.68, transmissibility=0.7,
    mdro=True,
    contact_precautions="Contact isolation (MDRO)",
    reservoir="Moist environments, saline flushes, disinfectants, catheters",
    syndromes=["Bloodstream infection", "Ventilator-associated pneumonia",
               "Urinary tract infection", "Endophthalmitis", "Joint sepsis"],
    alert_text="Classic vehicle-borne outbreak organism (contaminated multipvial "
               "saline/chlorhexidine); investigate product history in every cluster.",
))


# --------------------------------------------------------------------------
# Explicitly non-target organisms. Matching one of these phrases (when it is
# more specific than any target match) suppresses an alert, so that commensals
# such as coagulase-negative staphylococci do not pollute the dashboard.
# --------------------------------------------------------------------------
NON_TARGET_ORGANISMS: dict[str, str] = {
    "staphylococcus epidermidis": "Coagulase-negative staphylococcus",
    "staphylococcus haemolyticus": "Coagulase-negative staphylococcus",
    "staphylococcus hominis": "Coagulase-negative staphylococcus",
    "staphylococcus saprophyticus": "Coagulase-negative staphylococcus",
    "staphylococcus lugdunensis": "Coagulase-negative staphylococcus",
    "coagulase negative staphylococcus": "Coagulase-negative staphylococcus",
    "cons": "Coagulase-negative staphylococcus",
    "staphylococcus": "Staphylococcus spp. (species not stated)",
    "micrococcus": "Micrococcus spp.",
    "candida": "Yeasts - not a bacterial target",
    "aspergillus": "Mould - not a bacterial target",
    "cryptococcus": "Yeasts - not a bacterial target",
    "malassezia": "Yeasts - not a bacterial target",
    "lactobacillus": "Commensal flora",
    "lactococcus": "Commensal flora",
    "leuconostoc": "Commensal flora",
    "corynebacterium": "Diphtheroids / skin flora",
    "cutibacterium": "Skin flora",
    "propionibacterium": "Skin flora",
    "bacillus": "Bacillus spp. (likely contaminant)",
    "pseudomonas putida": "Non-aeruginosa Pseudomonas",
    "burkholderia": "Non-target non-fermenter",
    "stenotrophomonas": "Non-target non-fermenter",
    "achromobacter": "Non-target non-fermenter",
    "moraxella": "Respiratory commensal",
    "haemophilus influenzae": "Non-target respiratory pathogen",
    "haemophilus": "Non-target respiratory pathogen",
    "neisseria": "Non-target Gram-negative cocci",
    "acinetobacter lwoffii": "Non-baumannii Acinetobacter",
    "acinetobacter haemolyticus": "Non-baumannii Acinetobacter",
    "citrobacter": "Non-target Enterobacterales",
    "enterobacter": "Non-target Enterobacterales",
    "cronobacter": "Non-target Enterobacterales",
    "edwardsiella": "Non-target Enterobacterales",
    "morganella": "Non-target Proteeae",
    "providencia": "Non-target Proteeae",
    "yersinia": "Non-target Enterobacterales",
    "shigella": "Non-target Enterobacterales",
    "campylobacter": "Non-target curved rod",
    "helibacter": "Non-target curved rod",
    "vibrio": "Non-target curved rod",
    "aeromonas": "Non-target curved rod",
    "listeria": "Non-target Gram-positive rod",
    "nocardia": "Non-target actinomycete",
    "actinomyces": "Non-target actinomycete",
    "treponema": "Non-target spirochaete",
    "mycobacterium": "Mycobacteria - separate programme",
    "mycoplasma": "Cell-wall-deficient organism",
    "ureaplasma": "Cell-wall-deficient organism",
    "klebsiella oxytoca": "Non-pneumoniae Klebsiella",
    "klebsiella aerogenes": "Non-pneumoniae Klebsiella",
    "klebsiella variicola": "Non-pneumoniae Klebsiella",
    "proteus vulgaris": "Non-mirabilis Proteus",
    "proteus penneri": "Non-mirabilis Proteus",
    "serratia liquefaciens": "Non-marcescens Serratia",
    "escherichia fergusonii": "Non-coli Escherichia",
    "mixed growth": "Mixed flora - no single pathogen",
    "mixed flora": "Mixed flora - no single pathogen",
    "normal flora": "Normal flora",
    "no pathogen isolated": "No pathogen isolated",
    "no growth": "No growth",
    "no organism isolated": "No organism isolated",
    "contaminant": "Specimen contamination",
    "skin flora": "Skin flora contaminant",
    "delayed growth only": "Contaminant / low significance",
}

# --------------------------------------------------------------------------
# Resistance phenotype markers that can appear anywhere in a result string.
# They do not identify an organism on their own but enrich the record.
# --------------------------------------------------------------------------
RESISTANCE_MARKERS: dict[str, str] = {
    "mrsa": "Methicillin-resistant S. aureus",
    "mrca": "Methicillin-resistant CoNS",
    "vrsa": "Vancomycin-resistant S. aureus",
    "visa": "Vancomycin-intermediate S. aureus",
    "vre": "Vancomycin-resistant enterococcus",
    "esbl": "Extended-spectrum beta-lactamase producer",
    "cre": "Carbapenem-resistant Enterobacterales",
    "crkp": "Carbapenem-resistant K. pneumoniae",
    "crab": "Carbapenem-resistant A. baumannii",
    "mdr": "Multi-drug resistant",
    "xdr": "Extensively drug-resistant",
    "pdr": "Pan drug-resistant",
    "carbapenemase": "Carbapenemase producer",
    "kpc": "KPC carbapenemase",
    "ndm": "NDM carbapenemase",
    "oxa 48": "OXA-48 carbapenemase",
    "hlar": "High-level aminoglycoside resistance",
    "hlgn": "High-level gentamicin resistance",
}

# Longer spellings that collapse onto a canonical marker tag.
MARKER_SYNONYMS: dict[str, str] = {
    "vancomycin resistant enterococcus": "VRE",
    "vancomycin resistant enterococci": "VRE",
    "glycopeptide resistant enterococcus": "VRE",
    "methicillin resistant staphylococcus aureus": "MRSA",
    "meticillin resistant staphylococcus aureus": "MRSA",
    "oxacillin resistant staphylococcus aureus": "MRSA",
    "extended spectrum beta lactamase": "ESBL",
    "extended spectrum beta-lactamase": "ESBL",
    "third generation cephalosporin resistant": "ESBL",
    "carbapenem resistant enterobacterales": "CRE",
    "carbapenem resistant enterobacteriaceae": "CRE",
    "carbapenem resistant klebsiella pneumoniae": "CRKP",
    "carbapenem resistant acinetobacter baumannii": "CRAB",
    "carbapenem resistant pseudomonas": "CRPA",
    "multi drug resistant": "MDR",
    "multidrug resistant": "MDR",
    "extensively drug resistant": "XDR",
    "pan drug resistant": "PDR",
    "vancomycin intermediate staphylococcus aureus": "VISA",
}


def key_for(name_or_alias: str) -> str | None:
    """Convenience reverse lookup from a display name to a catalog key."""
    from .textutil import normalise
    target = normalise(name_or_alias)
    for p in PATHOGENS.values():
        if target in {normalise(p.name), normalise(p.short_name), normalise(p.key)}:
            return p.key
        if any(target == normalise(a) for a in p.aliases):
            return p.key
    return None


def display_name(key: str) -> str:
    p = PATHOGENS.get(key)
    return p.short_name if p else (key or "Unknown")


def full_name(key: str) -> str:
    p = PATHOGENS.get(key)
    return p.name if p else (key or "Unknown")


def color_for(key: str) -> str:
    p = PATHOGENS.get(key)
    return p.color if p else "#64748b"


TARGET_KEYS: list[str] = list(PATHOGENS.keys())

# Order used by the dashboard pathogen grid: highest intrinsic threat first.
PRIORITY_ORDER: list[str] = sorted(PATHOGENS, key=lambda k: -PATHOGENS[k].threat)
