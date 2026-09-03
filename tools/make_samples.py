"""
Generate the bundled example reports served at /samples/<name>.

These exist so that a first-time visitor can exercise the whole intake path -
upload, parse, store, analyse - without needing a real laboratory export, which
is the one thing no demo can supply on a hospital network.

Seven fixtures, each aimed at a different branch of the readers:

  icu-klebsiella-long.csv      long panel, one row per organism/agent pair
  ward12-mrsa-wide.tsv         wide panel + merged cells + cefoxitin-driven MRSA
  cdiff-pcr-screen.csv         detection-only rapid screen, positives + negatives
  esbl-ecoli-profile.csv       compact "Amp:R; Mer:S" profile string column
  mixed-background-noise.csv   junk banners, non-target flora, genus-only rows
  single-report.pdf            free-text report, dot-leader susceptibility list
  tabular-antibiogram.pdf      ruled-grid antibiogram table

Dates are written relative to the day of generation so the samples never look
stale, and every identifier is synthetic. The PDFs are emitted by the small
writer at the bottom of this file rather than by a PDF library: Bioguard's only
runtime dependency for reading is pdfplumber, and it would be silly to make
people install a second package just to create two test documents.

Every file is parsed back through the real readers before this script exits.
A "sample" that silently parses to zero rows is worse than no sample at all,
because the upload page actively invites the visitor to click it.

    python tools/make_samples.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config                                             # noqa: E402
from bioguard.parsing import parse_upload                 # noqa: E402

SAMPLE_DIR = Path(config.BASE_DIR) / "samples"
TODAY = date.today()


def d(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def iso(days_ago: int) -> str:
    return d(days_ago).isoformat()


def dmy(days_ago: int) -> str:
    """DD/MM/YYYY - only ever used with a day above 12 so it stays unambiguous."""
    x = d(days_ago)
    return f"{x.day:02d}/{x.month:02d}/{x.year}"


def words(days_ago: int) -> str:
    """'05 Aug 2026' - the format most LIS PDFs print."""
    return d(days_ago).strftime("%d %b %Y")


# Excel stores a date as days since its 1900 epoch; the reader recognises these.
EXCEL_EPOCH = date(1899, 12, 30)


def serial(days_ago: int) -> str:
    return str((d(days_ago) - EXCEL_EPOCH).days)


# ==========================================================================
# 1. long panel: a genuine ICU cluster of carbapenem-resistant Klebsiella
# ==========================================================================
LONG_HEADER = ["Patient MRN", "Ward", "Date Collected", "Specimen", "Organism",
               "Antibiotic", "Interpretation", "MIC (ug/mL)", "Method"]

# (patient, day offset, organism, [(agent, call, mic), ...])
LONG_ISOLATES = [
    ("88214", 3,  "Klebsiella pneumoniae",
     [("Meropenem", "R", ">32"), ("Imipenem", "R", ">32"),
      ("Ceftazidime", "R", ">32"), ("Ciprofloxacin", "R", ">4"),
      ("Amikacin", "S", "2"), ("Tigecycline", "I", "4")]),
    ("88219", 5,  "Klebsiella pneumoniae",
     [("Meropenem", "R", ">32"), ("Imipenem", "R", "32"),
      ("Ceftazidime", "R", ">32"), ("Ciprofloxacin", "R", ">4"),
      ("Amikacin", "R", "32"), ("Tigecycline", "S", "1")]),
    ("88231", 7,  "Klebsiella pneumoniae",
     [("Meropenem", "R", "16"), ("Imipenem", "R", "16"),
      ("Ceftazidime", "S", "<=1"), ("Gentamicin", "R", "16"),
      ("Amikacin", "S", "4")]),
    ("88240", 9,  "Klebsiella pneumoniae",
     [("Meropenem", "R", ">32"), ("Ceftazidime", "R", ">32"),
      ("Piperacillin/Tazobactam", "R", ">128"), ("Amikacin", "S", "4")]),
    ("88247", 12, "Klebsiella pneumoniae",
     [("Meropenem", "I", "8"), ("Ceftazidime", "R", "16"),
      ("Cotrimoxazole", "S", "<=2")]),
    # This one is fully susceptible: the control that keeps the ward cluster
    # from reading as an automatic high-risk event.
    ("88255", 15, "Klebsiella pneumoniae",
     [("Meropenem", "S", "<=1"), ("Ceftazidime", "S", "<=1"),
      ("Piperacillin/Tazobactam", "S", "<=4")]),
    # A second organism from a patient already in the file, to prove that one
    # upload can carry several isolates and that they are not merged away.
    ("88214", 4,  "Enterococcus faecium",
     [("Ampicillin", "R", "32"), ("Vancomycin", "R", ">32"),
      ("Teicoplanin", "R", "16"), ("Linezolid", "S", "2")]),
]


def build_long_panel() -> tuple[str, str]:
    rows = [LONG_HEADER]
    for pid, ago, organism, panel in LONG_ISOLATES:
        for agent, call, mic in panel:
            rows.append([f"M-{pid}", "ICU", words(ago), "Bronchoalveolar lavage",
                         organism, agent, call, mic, "Vitek 2"])
    return "icu-klebsiella-long.csv", _csv(rows)


def _csv(rows, delim=",") -> str:
    out = []
    for row in rows:
        cells = []
        for cell in row:
            cell = str(cell)
            if any(ch in cell for ch in (delim, '"', "\n")):
                cell = '"' + cell.replace('"', '""') + '"'
            cells.append(cell)
        out.append(delim.join(cells))
    return "\r\n".join(out) + "\r\n"


# ==========================================================================
# 2. wide panel with merged cells: MSSA that the antibiogram reclassifies
# ==========================================================================
WIDE_HEADER = ["Patient ID", "Patient Name", "Ward", "Room", "Sample Date",
               "Specimen Type", "Organism Identified", "Penicillin", "Cefoxitin",
               "Clindamycin", "Erythromycin", "Gentamicin", "Rifampicin",
               "Vancomycin", "Linezolid", "Cotrimoxazole"]

# None means "cell left blank because the LIS merged it with the row above".
WIDE_ROWS = [
    ("S-4001", "Halloran, P", "Ward 12", "12", words(2), "Wound swab",
     "Staphylococcus aureus", "R", "R", "S", "R", "S", "S", "S", "S", "S"),
    ("S-4001", None, "Ward 12", None, words(2), None,
     "Staphylococcus aureus", "R", "R", "R", "R", "S", "S", "S", "S", None),
    ("S-4007", "Okafor, R", "", "", words(4), "Blood culture",
     "Staphylococcus aureus", "R", "R", "S", "S", "S", "R", "S", "S", "S"),
    ("S-4012", "Berg, U", None, "09", words(6), "Sputum",
     "Staphylococcus aureus", "S", "S", "S", "S", "S", "S", "S", None, "S"),
    ("S-4019", "Nasser, F", "", "", words(8), "Urine",
     "Staphylococcus aureus", "R", "R", "S", "R", "R", "S", "S", "S", "S"),
    ("S-4024", "Doyle, M", None, "21", words(11), "Wound swab",
     "Staphylococcus aureus", "S", "S", "S", "S", "S", "S", "S", None, "S"),
    ("S-4030", "Kovač, A", "", "", words(14), "Blood culture",
     "Coagulase-negative Staphylococcus", "R", "R", "R", "S", "S", "R", "S", "S", None),
]


def build_wide_panel() -> tuple[str, str]:
    rows = [WIDE_HEADER]
    for row in WIDE_ROWS:
        rows.append(["" if c is None else c for c in row])
    # Blank trailing line, as most spreadsheet exports produce.
    return "ward12-mrsa-wide.tsv", _csv(rows, "\t") + "\r\n"


# ==========================================================================
# 3. detection-only: rapid PCR panel, positives and negatives together
# ==========================================================================
DETECT_HEADER = ["Patient Number", "Care Unit", "Collection Date", "Test Method",
                 "Target", "Detected", "Reported"]

DETECT_ROWS = [
    ("S-5102", "Ward 8",   words(1),  "Verigene PCR", "Clostridioides difficile toxin B", "Positive"),
    ("S-5108", "Ward 8",   words(3),  "Verigene PCR", "Clostridioides difficile toxin B", "Positive"),
    ("S-5115", "Geriatrics", words(4), "Verigene PCR", "Clostridioides difficile GDH",     "Positive"),
    ("S-5121", "Ward 8",   words(6),  "Verigene PCR", "Clostridioides difficile toxin B", "Negative"),
    ("S-5126", "ICU",      words(7),  "Verigene PCR", "Clostridioides difficile toxin B", "Positive"),
    ("S-5133", "Geriatrics", words(9), "EIA toxin assay", "Clostridium difficile",         "Positive"),
    ("S-5140", "Ward 8",   words(10), "EIA toxin assay", "Clostridium difficile",          "Negative"),
    ("S-5144", "Oncology", words(12), "Verigene PCR", "Staphylococcus aureus (MecA)",      "Positive"),
    ("S-5150", "NICU",     words(13), "Verigene PCR", "Klebsiella pneumoniae",             "Positive"),
    ("S-5155", "Ward 3",   words(15), "Verigene PCR", "Clostridioides difficile toxin B",  "Negative"),
]


def build_detection() -> tuple[str, str]:
    rows = [DETECT_HEADER]
    for pid, ward, when, method, target, call in DETECT_ROWS:
        rows.append([pid, ward, when, method, target, call, words(0)])
    return "cdiff-pcr-screen.csv", _csv(rows)


# ==========================================================================
# 4. profile-string column
# ==========================================================================
PROFILE_HEADER = ["Accession Number", "Placed In", "Date Sampled", "Isolate Source",
                  "Bacterial Species", "Resistance Pattern", "Comment"]

PROFILE_ROWS = [
    ("A-7701", "Renal Dialysis", dmy(2),  "Urine",      "Escherichia coli",
     "Amp:R; Cefotaxime:R; Ceftriaxone:R; Aztreonam:R; Mer:S; Gent:S; Cipro:R",
     "ESBL phenotype - report to IPC"),
    ("A-7708", "Renal Dialysis", dmy(4),  "Urine",      "E. coli",
     "Amp:R; Ceftriaxone:R; Mer:S; Amikacin:S; Nitrofurantoin:S", ""),
    ("A-7715", "Ward 5",         dmy(6),  "Blood",      "Escherichia coli",
     "Amp:R; Ceftriaxone:R; Cipro:R; Gent:R; Mer:S; Colistin:I",
     "MDR organism"),
    ("A-7722", "Ward 8",         dmy(9),  "Urine",      "Escherichia coli",
     "Amp:S; Ceftriaxone:S; Mer:S; Cipro:S", ""),
    ("A-7730", "Renal Dialysis", dmy(12), "Catheter tip", "Escherichia coli",
     "Amp:R; Cefoxitin:R; Ceftriaxone:R; Mer:I; Tigecycline:S", ""),
    ("A-7736", "Geriatrics",     dmy(16), "Stool",      "Salmonella enteritidis",
     "Amp:S; Ceftriaxone:S; Cipro:S", "Notifiable - health protection informed"),
]


def build_profile() -> tuple[str, str]:
    rows = [PROFILE_HEADER]
    for row in PROFILE_ROWS:
        rows.append(list(row))
    return "esbl-ecoli-profile.csv", _csv(rows)


# ==========================================================================
# 5. banner junk, non-target flora, genus-only calls, unreadable rows
# ==========================================================================
NOISE_TOP = [
    ["BIOGUARD REFERRAL LABORATORY"],
    ["Page 1 of 3"],
    ["Confidential - clinical record"],
    [],
    ["Pat ID", "Ward", "Collected", "Specimen", "Organism", "Result", "Notes"],
]

NOISE_ROWS = [
    # Real target, spelled the way a busy biomedical scientist types it.
    ("N-9001", "Surgical Ward", dmy(2), "Blood", "Klebsiella pn.", "Positive", ""),
    ("N-9002", "NICU", dmy(3), "CSF", "acinetobacter calcoaceticus/baumannii complex", "Positive",
     "complex name, must still match"),
    ("N-9003", "Ward 3", dmy(4), "Urine", "E. coli", "Positive", ""),
    # Genus only: kept, and flagged as a genus-level match.
    ("N-9004", "Geriatrics", dmy(5), "Throat swab", "Streptococcus sp.", "Positive",
     "species not determined"),
    ("N-9005", "Geriatrics", dmy(6), "Sputum", "Streptococcus pneumoniae", "Positive", ""),
    # Not one of the twelve.
    ("N-9006", "Ward 5", dmy(7), "Urine", "Enterococcus faecium", "Positive", "VRE screen"),
    ("N-9007", "Ward 5", dmy(8), "Swab", "Coagulase-negative Staphylococcus", "Positive",
     "considered skin flora"),
    ("N-9008", "Ward 5", dmy(9), "Stool", "Candida albicans", "Positive", "yeast, not tracked"),
    ("N-9009", "Ward 12", dmy(10), "Sputum", "Lactobacillus spp.", "Positive", ""),
    # A row with no organism at all: must be skipped, not invented.
    ("N-9010", "Ward 12", dmy(11), "Urine", "", "Negative", "no growth after 48h"),
    # Dates as Excel writes them: serial numbers.
    ("N-9011", "Emergency Department", serial(8), "Blood", "Proteus mirabilis", "Positive", ""),
    ("N-9012", "Emergency Department", serial(11), "Wound", "Serratia marcescens", "Positive", ""),
    # A typo, to show fuzzy matching earning its keep.
    ("N-9013", "Oncology", dmy(14), "Blood", "Psedomonas aeruginosa", "Positive",
     "misspelled in the source system"),
]

NOISE_BOTTOM = [
    [],
    ["Note: all results relate to the specimens listed above."],
    ["www.bioguard.invalid - synthetic data"],
    ["*R = resistant, S = susceptible, I = intermediate"],
]


def build_noise() -> tuple[str, str]:
    rows = [r for r in NOISE_TOP]
    for row in NOISE_ROWS:
        rows.append(list(row))
    rows += [r for r in NOISE_BOTTOM]
    return "mixed-background-noise.csv", _csv(rows)


# ==========================================================================
# 6. free-text PDF: the conventional single-report layout
# ==========================================================================
PDF_TEXT_REPORT = [
    ("h", "NORTHGATE GENERAL HOSPITAL        MICROBIOLOGY DEPARTMENT"),
    ("s", "ANTIMICROBIAL SUSCEPTIBILITY REPORT          Generated automatically"),
    ("b", ""),
    ("b", "PATIENT AND SPECIMEN"),
    ("kv", f"Patient ID: S-6001"),
    ("kv", "Patient Name: Marchetti, L"),
    ("kv", "Ward: ICU"),
    ("kv", "Room: 07"),
    ("kv", f"Specimen: Endotracheal aspirate"),
    ("kv", f"Collected: {words(3)}"),
    ("kv", f"Reported: {words(1)}"),
    ("b", ""),
    ("b", "DIRECT ANTIGEN DETECTION / CULTURE"),
    ("kv", "Organism: Pseudomonas aeruginosa"),
    ("b", "Sensitivity"),
    ("ab", "Piperacillin/Tazobactam ..... S  <=4/4"),
    ("ab", "Ceftazidime ................ R  >32"),
    ("ab", "Meropenem .................. R  16"),
    ("ab", "Imipenem ................... R  >=8"),
    ("ab", "Ciprofloxacin .............. R  >4"),
    ("ab", "Amikacin ................... S  8"),
    ("ab", "Colistin ................... S  0.5"),
    ("ab", "Ceftolozane/Tazobactam ..... S  <=1"),
    ("b", ""),
    ("s", "S = susceptible, I = intermediate, R = resistant"),
    ("b", ""),
    ("b", "SECOND ISOLATE FROM THE SAME PATIENT"),
    ("kv", "Organism: Stenotrophomonas maltophilia"),
    ("b", "Sensitivity"),
    ("ab", "Co-trimoxazole ............. S  <=2"),
    ("ab", "Levofloxacin ............... R  8"),
    ("b", ""),
    ("b", "NEW PATIENT BLOCK"),
    ("kv", "Patient ID: S-6008"),
    ("kv", "Ward: NICU"),
    ("kv", f"Date of Collection: {words(5)}"),
    ("kv", "Specimen Type:faecal swab"),
    ("kv", "Organism Identified: Salmonella enteritidis"),
    ("b", "Sensitivity"),
    ("ab", "Ceftriaxone ....... S"),
    ("ab", "Azithromycin ...... S"),
    ("ab", "Ciprofloxacin ..... R  >4"),
    ("b", ""),
    ("kv", "Comment: Notified to the health protection team on the {day}."),
]


def pdf_text_lines() -> list[tuple[str, str]]:
    out = []
    for kind, text in PDF_TEXT_REPORT:
        out.append((kind, text.replace("{day}", words(1))))
    return out


# ==========================================================================
# 7. ruled-grid PDF: an LIS table export
# ==========================================================================
TABLE_COLUMNS = [("Patient", 60), ("Ward", 66), ("Sampled", 62), ("Organism", 116),
                 ("Meropenem", 52), ("Ceftazidime", 52), ("Amikacin", 46),
                 ("Cipro", 44), ("Colistin", 48)]

TABLE_BODY = [
    ("T-3001", "ICU",  words(2),  "Klebsiella pneumoniae", "R >32", "R >32", "S 4",  "R >4", "I 2"),
    ("T-3002", "ICU",  words(3),  "Klebsiella pneumoniae", "R 16",  "R 16",  "S 2",  "R 4",  "S 0.25"),
    ("T-3005", "ICU",  words(5),  "Klebsiella pneumoniae", "R >32", "S <=1", "S 4",  "S <=0.25", "S 0.25"),
    ("T-3009", "Surgical Ward", words(7), "Klebsiella pneumoniae", "R 32", "R 32", "R 32", "R 4", "R 4"),
    ("T-3014", "Ward 3", words(9), "Proteus mirabilis", "S <=1", "S <=1", "S 8", "S <=0.25", "n/a"),
    ("T-3018", "Ward 3", words(11), "Morganella morganii", "R 16", "R 16", "S 8", "R 2", "n/a"),
]


# ==========================================================================
# A minimal PDF writer (base-14 fonts, no dependencies)
# ==========================================================================
PAGE_W, PAGE_H = 595.28, 841.89     # A4 portrait, points
LEFT, TOP, BOTTOM = 48.0, PAGE_H - 56.0, 56.0


def esc(text: str) -> str:
    """Escape a PDF literal string and drop anything WinAnsi cannot show."""
    out = []
    for ch in text:
        if ord(ch) > 126:
            ch = {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
                  "\u201c": '"', "\u201d": '"', "\u2022": "*", "\u2026": "...",
                  "\u010d": "c", "\u0107": "c", "\u00e1": "a", "\u00ed": "i",
                  "\u00b5": "u"}.get(ch, "?")
        if ch in "\\()":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class Pdf:
    """One page per flush(); enough for a two-page laboratory report."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = TOP

    @property
    def page(self) -> list[str]:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append([])
        self.y = TOP

    def room(self, needed: float) -> bool:
        return self.y - needed >= BOTTOM

    def at(self, text: str, x: float, y: float, font: str = "F1",
           size: int = 9) -> None:
        self.page.append(
            f"BT /{font} {size} Tf 1 0 0 1 {x:.2f} {y + size * 0.32:.2f} Tm "
            f"({esc(text)}) Tj ET")

    def line(self, text: str, font: str = "F1", size: int = 9,
             indent: float = 0.0, dy: float = 13.0) -> None:
        if not self.room(dy):
            self.new_page()
        self.at(text, LEFT + indent, self.y, font, size)
        self.y -= dy

    def rule(self, x0: float, y0: float, x1: float, y1: float,
             width: float = 0.7) -> None:
        self.page.append(
            f"0.45 0.45 0.45 RG {width} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")

    def save(self, path: Path) -> None:
        path.write_bytes(build_pdf(self.pages))


def build_pdf(pages: list[list[str]]) -> bytes:
    """Serialise pages of content ops into a valid PDF 1.4 byte stream."""
    n = len(pages)
    # 1 catalog, 2 pages tree, 3+4 fonts, then page/content pairs.
    first_page_obj = 5
    objects: list[bytes] = []

    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                   b" /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier"
                   b" /Encoding /WinAnsiEncoding >>")

    for i, ops in enumerate(pages):
        page_obj, content_obj = first_page_obj + 2 * i, first_page_obj + 2 * i + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_obj} 0 R >>".encode())
        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream
                       + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def build_text_pdf() -> tuple[str, Pdf]:
    pdf = Pdf()
    for kind, text in pdf_text_lines():
        if kind == "h":
            pdf.line(text, font="F1", size=13)
        elif kind == "b":
            pdf.line(text if text else " ", font="F1", size=9)
            pdf.y -= 1
        elif kind == "s":
            pdf.line(text, font="F1", size=7)
        elif kind == "kv":
            label, _, value = text.partition(":")
            if not pdf.room(13):
                pdf.new_page()
            # Label and value must share one baseline, exactly as a printed form
            # does; drawing the value from the already-decremented cursor would
            # split every field across two lines.
            pdf.at(f"{label}:", LEFT, pdf.y, "F1", 8)
            pdf.at(value.strip(), LEFT + 118, pdf.y, "F1", 9)
            pdf.y -= 13
        elif kind == "ab":
            pdf.line(text, font="F1", size=9, indent=14)
    return "single-report.pdf", pdf


def build_table_pdf() -> tuple[str, Pdf]:
    pdf = Pdf()
    pdf.line("NORTHGATE GENERAL HOSPITAL  -  MICROBIOLOGY  -  WEEKLY SURVEILLANCE EXTRACT",
             font="F1", size=11)
    pdf.line(f"Extract generated {words(0)}   (synthetic demonstration data)",
             font="F1", size=7)
    pdf.y -= 8

    widths = [w for _, w in TABLE_COLUMNS]
    xs, acc = [], LEFT
    for w in widths:
        xs.append(acc)
        acc += w
    right = acc
    row_h, top = 18.0, pdf.y

    def hline(y: float) -> None:
        pdf.rule(LEFT, y, right, y)

    def vline(x: float, y0: float, y1: float) -> None:
        pdf.rule(x, y0, x, y1)

    total_rows = len(TABLE_BODY) + 1
    bottom = top - total_rows * row_h
    hline(top)
    hline(bottom)
    for x in xs + [right]:
        vline(x, bottom, top)
    for r in range(1, total_rows):
        hline(top - r * row_h)

    y = top
    for c, (label, _) in enumerate(TABLE_COLUMNS):
        pdf.at(label, xs[c] + 4, y - row_h, "F1", 8)
    for row in TABLE_BODY:
        y -= row_h
        for c, value in enumerate(row):
            pdf.at(str(value), xs[c] + 4, y - row_h, "F1", 8)
    pdf.y = bottom - 18
    pdf.line("Key: S susceptible, I intermediate, R resistant, n/a not tested.",
             font="F1", size=7)
    pdf.line("Report 2 of 2 - synthetic data, no real patient information.",
             font="F1", size=7)
    return "tabular-antibiogram.pdf", pdf


# ==========================================================================
# Driver
# ==========================================================================
def expected(name: str) -> str:
    """What each fixture is there to prove, in one line for the log."""
    return {
        "icu-klebsiella-long.csv": "long layout; 6 ICU Klebsiella patients",
        "ward12-mrsa-wide.tsv": "wide + merged cells; cefoxitin R upgrades to MRSA",
        "cdiff-pcr-screen.csv": "detection layout; negatives must be suppressed",
        "esbl-ecoli-profile.csv": "profile string; ESBL marker from ceftriaxone R",
        "mixed-background-noise.csv": "banners skipped; non-target organisms kept visible",
        "single-report.pdf": "free text; two patients + a non-target isolate",
        "tabular-antibiogram.pdf": "ruled grid; four ICU/Ward Klebsiella rows",
    }.get(name, "")


def main() -> int:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in SAMPLE_DIR.glob("*"):
        if stale.suffix.lower() in config.ALLOWED_EXTENSIONS:
            stale.unlink()

    makers = [build_long_panel, build_wide_panel, build_detection,
              build_profile, build_noise]
    produced: list[tuple[str, bytes]] = []
    for make in makers:
        name, text = make()
        produced.append((name, text.encode("utf-8")))
    for build in (build_text_pdf, build_table_pdf):
        name, pdf = build()
        produced.append((name, build_pdf(pdf.pages)))

    problems: list[str] = []
    print(f"writing to {SAMPLE_DIR}\n")
    print(f"{'file':30} {'rows':>5} {'recs':>5} {'skip':>5} {'layout':>10}  organisms")
    for name, blob in produced:
        path = SAMPLE_DIR / name
        path.write_bytes(blob)
        data = path.read_bytes()
        outcome = parse_upload(name, data)
        found = {}
        for r in outcome.records:
            key = r.pathogen or ("nontarget:" + (r.other_label or "?")
                                 if not r.pathogen else "?")
            found[key] = found.get(key, 0) + 1
        marks = sorted({m for r in outcome.records for m in r.markers})
        print(f"{name:30} {outcome.rows_read:>5} {len(outcome.records):>5} "
              f"{outcome.rows_skipped:>5} {outcome.layout:>10}  "
              f"{_fmt(found)}")
        if marks:
            print(f"{'':30} {'':5} {'':5} {'':5} {'':10}  markers: {', '.join(marks)}")
        if outcome.errors:
            print(f"{'':30} ERRORS: {outcome.errors}")
        if not outcome.records:
            problems.append(f"{name}: parsed to zero records")
        print(f"{'':30} {expected(name)}")
    return _finish(problems)


def _fmt(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def _finish(problems: list[str]) -> int:
    print()
    if problems:
        for p in problems:
            print("PROBLEM " + p)
        return 1
    print("all fixtures parsed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
