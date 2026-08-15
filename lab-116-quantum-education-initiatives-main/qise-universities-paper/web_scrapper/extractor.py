"""
extractor.py — Convert raw HTML / PDF / RSS items into clean *evidence fragments*.

An evidence fragment is the atomic unit the classifier scores. Every fragment
carries enough provenance to make a result auditable:

  {
    "media_type": "html" | "pdf",
    "source_url": str,          # canonical URL of the document
    "found_on_page": str,       # HTML page a PDF link was discovered on ("" otherwise)
    "pdf_url": str,             # == source_url for PDFs, "" for HTML
    "pdf_page": int | None,     # 1-based page number (PDF only)
    "source_type": str,         # syllabus / curriculum_grid / catalog / ...
    "title": str,               # document / block title
    "raw_text": str,            # cleaned text ready for the classifier
    "university": str, "country": str, "country_code": str,
    "language": str,            # es / pt / en
    "extraction_status": "extracted" | "failed_pdf_extraction" | "needs_manual_review",
  }

PDF philosophy (the core fix): a PDF that we downloaded is NEVER silently
dropped. If text extraction fails or returns almost nothing (scanned / image
PDF), we still emit a fragment flagged for manual review.

CORREGIDO — resumen de cambios (buscar "CAMBIO" para el detalle):
  1. [BUG, confirmado con impacto real] extract_from_html(): el fragmento de
     "página completa como un solo bloque" que se devuelve cuando NO se
     detecta ningún bloque de curso (sin tablas, sin accordions/cards) no
     llevaba coarse=True — a diferencia del catch_all, que sí lo lleva y por
     la MISMA razón (mezcla muchos contextos). Sin la bandera, una mención
     de paso en una página genérica se podía clasificar qise_core en vez de
     unclear. Confirmado con un caso de prueba real (ver commit message /
     conversación) antes y después del fix.
  2. [LIMPIEZA, consistencia de esquema] extract_from_rss_entry(),
     extract_from_tweet() y extract_from_reddit_post() construyen su dict de
     salida a mano (no vía _make_fragment) y no incluían
     "academic_level_hint" — presente en todo lo demás que produce el
     extractor. extract_from_reddit_post() además no incluía "seed_origin"
     (los otros dos sí). No es una causa de crash (el resto del pipeline usa
     fragment.get(...) con default en todos los sitios que revisamos), pero
     si algo en pipeline.py arma el CSV con una lista fija de columnas, una
     fila con menos claves que las demás puede quedar con celdas vacías por
     una razón distinta a la real (falta de dato) — así que se agregan por
     consistencia.
"""

import io
import re
from typing import Any

from keywords import find_quantum_matches, detect_academic_level
from utils import (
    clean_course_text,
    detect_language,
    guess_source_type,
    is_likely_course_page,  # re-exported for dispatcher backwards-compat
    get_logger,
)

logger = get_logger("extractor")

# A page/doc with fewer than this many extractable chars is treated as
# scanned / image-based and flagged for manual review instead of discarded.
MIN_PDF_CHARS = 120


# ── FRAGMENT FACTORY ──────────────────────────────────────────────────────────

def _make_fragment(
    *,
    text: str,
    url: str,
    university: dict,
    media_type: str,
    source_type: str | None = None,
    title: str = "",
    pdf_page: int | None = None,
    found_on_page: str = "",
    extraction_status: str = "extracted",
    academic_level_hint: str = "",
) -> dict:
    is_pdf = media_type == "pdf"
    return {
        "media_type": media_type,
        "source_url": url,
        "found_on_page": found_on_page,
        "pdf_url": url if is_pdf else "",
        "pdf_page": pdf_page,
        # doc-level undergrad/grad hint (front matter / page title); the
        # classifier prefers URL and local-context signals over this.
        "academic_level_hint": academic_level_hint,
        "source_type": source_type or guess_source_type(url, text, is_pdf),
        "title": (title or "").strip()[:200],
        "raw_text": text,
        "university": (university or {}).get("name", ""),
        "country": (university or {}).get("country", ""),
        "country_code": (university or {}).get("country_code", ""),
        "language": detect_language(text) if text else (university or {}).get("language", ""),
        "extraction_status": extraction_status,
        # manual | auto_discovered | homepage_crawl — how this institution's
        # seed URLs were obtained (for evaluating auto-discovery quality).
        "seed_origin": (university or {}).get("seed_origin", "manual"),
    }


# ── HTML EXTRACTOR ────────────────────────────────────────────────────────────

def extract_from_html(html: str, url: str, university: dict,
                      found_on_page: str = "") -> list[dict]:
    """
    Parse HTML into evidence fragments. Broad by design: we return structured
    course blocks when we can find them, otherwise the whole page as one
    fragment. Deciding whether the evidence is a *course* is the classifier's
    job, not the extractor's, so we do NOT gate on `is_likely_course_page` here.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4 not installed. Run: pip install beautifulsoup4")
        return []

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "form"]):
        tag.decompose()

    page_title = _get_page_title(soup)
    full_text = clean_course_text(soup.get_text(separator="\n"))
    # Page-level undergrad/grad hint from the title + opening text.
    level_hint = detect_academic_level(page_title + " " + full_text[:800])

    blocks = _extract_course_blocks(soup, url, university, found_on_page,
                                    level_hint)
    if blocks:
        # Safety net: block heuristics can miss courses when a site uses an
        # unfamiliar DOM (accordions, tabs, custom widgets). Never let a quantum
        # mention vanish just because we did not recognise its container — if any
        # quantum phrase in the whole page is not represented in the emitted
        # blocks, append the full page text as one more fragment.
        covered = "\n".join(b["raw_text"] for b in blocks)
        if _uncovered_quantum(full_text, covered) and len(full_text) >= 30:
            catch_all = _make_fragment(
                text=full_text, url=url, university=university, media_type="html",
                title=page_title, found_on_page=found_on_page,
                source_type="html_page", academic_level_hint=level_hint,
            )
            # Flag as coarse: a whole-page fragment mixes many contexts, so the
            # classifier must judge each match by its LOCAL surroundings and not
            # promote to qise_core on distant course signals.
            catch_all["coarse"] = True
            blocks.append(catch_all)
        return blocks

    if len(full_text) < 30:
        return []
    # CAMBIO: este fragmento TAMBIÉN es la página completa como un
    # solo bloque — la misma situación que catch_all arriba, y por la misma
    # razón necesita coarse=True 
    whole_page = _make_fragment(
        text=full_text, url=url, university=university, media_type="html",
        title=page_title, found_on_page=found_on_page,
        academic_level_hint=level_hint,
    )
    whole_page["coarse"] = True
    return [whole_page]


# Classes that commonly wrap one course/subject entry, across the many CMS and
# hand-built curriculum pages LatAm universities use. Matching is substring +
# case-insensitive, so "js-accordion-item" and "plan-de-estudios" both hit.
_COURSE_BLOCK_CLASS = re.compile(
    r"curso|course|subject|asignatura|disciplina|materia|"
    r"accordion-item|acordeon|collapse-item|"
    r"malla|curricul|plan-?de-?estudio|program-item|syllabus|silabo",
    re.I,
)


def _extract_course_blocks(soup, url, university, found_on_page,
                           level_hint="") -> list[dict]:
    """Pull individual course-like entries from tables / cards / accordions."""
    records: list[dict] = []

    # Strategy 1: catalog table rows (one course per row)
    for table in soup.find_all("table"):
        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            text = clean_course_text(" | ".join(c.get_text(strip=True) for c in cells))
            if len(text) < 20:
                continue
            records.append(_make_fragment(
                text=text, url=url, university=university, media_type="html",
                title=text[:120], found_on_page=found_on_page,
                academic_level_hint=level_hint,
            ))
    if records:
        logger.debug(f"Table extraction: {len(records)} rows from {url}")
        return records

    # Strategy 2: course-like cards / accordion items / list entries.
    candidates = soup.find_all(["div", "article", "section", "li"],
                               attrs={"class": _COURSE_BLOCK_CLASS})
    # Keep only the innermost matches. Curriculum accordions nest a cycle/semester
    # container (also matching) around the individual courses; without this a
    # cycle wrapper would be emitted as one giant fragment AND swallow its
    # courses' provenance. If a candidate contains another candidate, skip it.
    candidate_ids = {id(c) for c in candidates}
    seen_texts: set[str] = set()
    for block in candidates:
        if any(id(d) in candidate_ids
               for d in block.find_all(["div", "article", "section", "li"])):
            continue  # wrapper around finer-grained course blocks — skip
        text = clean_course_text(block.get_text(separator="\n"))
        if len(text) < 20:
            continue
        key = re.sub(r"\s+", " ", text.lower()).strip()
        if key in seen_texts:  # same course listed twice (e.g. highlight + malla)
            continue
        seen_texts.add(key)
        records.append(_make_fragment(
            text=text, url=url, university=university, media_type="html",
            title=text[:120], found_on_page=found_on_page,
            academic_level_hint=level_hint,
        ))
    if records:
        logger.debug(f"Block extraction: {len(records)} from {url}")
    return records


def _uncovered_quantum(full_text: str, covered_text: str) -> set[str]:
    """Quantum phrases present in the whole page but not in the emitted blocks."""
    page = {m["phrase"] for m in find_quantum_matches(full_text)}
    if not page:
        return set()
    covered = {m["phrase"] for m in find_quantum_matches(covered_text)}
    return page - covered


def _get_page_title(soup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


# ── PDF EXTRACTOR ─────────────────────────────────────────────────────────────

def extract_from_pdf(pdf_bytes: bytes, url: str, university: dict,
                     found_on_page: str = "") -> list[dict]:
    """
    Extract text from a PDF, one fragment per page.

    Returns AT LEAST one fragment for every PDF we were handed:
      * healthy PDF        → one 'extracted' fragment per non-empty page
      * scanned/image PDF  → one 'needs_manual_review' fragment (little/no text)
      * unreadable/corrupt → one 'failed_pdf_extraction' fragment
    """
    pages, engine = _pdf_to_pages(pdf_bytes)

    # Could not open the file with any engine.
    if pages is None:
        logger.warning(f"PDF unreadable (all engines failed): {url}")
        return [_make_fragment(
            text="", url=url, university=university, media_type="pdf",
            source_type="pdf", found_on_page=found_on_page,
            extraction_status="failed_pdf_extraction",
        )]

    total_chars = sum(len(p) for p in pages)
    doc_title = _first_meaningful_line(pages[0]) if pages else ""
    # Doc-level undergrad/grad hint: catalogs announce "ESCUELA DE POSGRADO",
    # "MAESTRÍA EN…" (or "pregrado") in their front matter.
    level_hint = detect_academic_level("\n".join(pages[:2])[:4000])

    # Opened, but essentially no text → almost certainly scanned/image-only.
    if total_chars < MIN_PDF_CHARS:
        logger.warning(
            f"PDF has {total_chars} chars over {len(pages)} page(s) "
            f"(engine={engine}) — flagging for manual review: {url}"
        )
        return [_make_fragment(
            text="", url=url, university=university, media_type="pdf",
            source_type="pdf", title=doc_title, found_on_page=found_on_page,
            extraction_status="needs_manual_review",
        )]

    fragments: list[dict] = []
    for i, page_text in enumerate(pages, start=1):
        cleaned = clean_course_text(page_text)
        if len(cleaned) < 30:
            continue
        # Page-level signal first: catalogs can MIX pregrado and posgrado
        # sections in one document, so the front-matter hint must not paint
        # every page (UNI's combined catalog does exactly this).
        page_hint = detect_academic_level(cleaned[:3000]) or level_hint
        fragments.append(_make_fragment(
            text=cleaned, url=url, university=university, media_type="pdf",
            title=doc_title, pdf_page=i, found_on_page=found_on_page,
            academic_level_hint=page_hint,
        ))

    if not fragments:  # every page individually tiny but total passed — keep whole doc
        whole = clean_course_text("\n".join(pages))
        fragments.append(_make_fragment(
            text=whole, url=url, university=university, media_type="pdf",
            title=doc_title, found_on_page=found_on_page,
            academic_level_hint=level_hint,
        ))

    logger.debug(f"PDF extracted: {len(fragments)} page fragment(s) via {engine}: {url}")
    return fragments


def _pdf_to_pages(pdf_bytes: bytes) -> tuple[list[str] | None, str]:
    """
    Return (list_of_page_texts, engine_name). Tries the most robust engines
    first. Returns (None, "") only if NO engine could open the document at all.
    """
    opened = False

    # 1) PyMuPDF (fitz) — fast, robust, per-page.
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = [page.get_text("text") or "" for page in doc]
        opened = True
        if sum(len(p) for p in pages) >= MIN_PDF_CHARS:
            return pages, "pymupdf"
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"PyMuPDF failed: {e}")

    # 2) pdfplumber — great on tables / curriculum grids, per-page.
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [(page.extract_text() or "") for page in pdf.pages]
        opened = True
        if sum(len(p) for p in pages) >= MIN_PDF_CHARS:
            return pages, "pdfplumber"
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pdfplumber failed: {e}")

    # 3) pdfminer.six — whole document (no cheap per-page split).
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        text = pdfminer_extract(io.BytesIO(pdf_bytes)) or ""
        opened = True
        if len(text) >= MIN_PDF_CHARS:
            return _split_form_feed(text), "pdfminer"
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pdfminer failed: {e}")

    # 4) pypdf — last resort, per-page.
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [(page.extract_text() or "") for page in reader.pages]
        opened = True
        return pages, "pypdf"  # even if tiny — caller flags needs_manual_review
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pypdf failed: {e}")

    # If an earlier engine opened it but all were below threshold, return the
    # (small) text so the caller can flag needs_manual_review rather than fail.
    if opened:
        return [""], "low_text"
    return None, ""


def _split_form_feed(text: str) -> list[str]:
    """pdfminer separates pages with the form-feed char (\\x0c)."""
    parts = text.split("\x0c")
    return parts if len(parts) > 1 else [text]


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) >= 4:
            return line[:200]
    return ""


# ── SPREADSHEET EXTRACTOR (planes de estudio published as Excel) ──────────────

MAX_SHEET_ROWS = 2000  # per sheet — planes never come close; bounds junk files


def extract_from_xlsx(content: bytes, url: str, university: dict,
                      found_on_page: str = "") -> list[dict]:
    """
    Extract text from an Excel workbook (xlsx via openpyxl; legacy xls via
    optional xlrd), one fragment per sheet, rows joined cell-by-cell with
    " | " — the same shape as an HTML catalog row, so the classifier scores
    it identically. Same never-drop philosophy as PDFs: every workbook we
    fetched yields at least one fragment ('extracted' or 'needs_manual_review').
    """
    sheets = _xlsx_to_sheets(content)
    if sheets is None:
        sheets = _xls_to_sheets(content)
    if sheets is None or sum(len(t) for _, t in sheets) < 40:
        logger.warning(f"Spreadsheet unreadable or empty — manual review: {url}")
        return [_make_fragment(
            text="", url=url, university=university, media_type="xlsx",
            source_type="curriculum_grid", found_on_page=found_on_page,
            extraction_status="needs_manual_review",
        )]

    level_hint = detect_academic_level(
        " ".join(name for name, _ in sheets) + " " + sheets[0][1][:2000])

    fragments: list[dict] = []
    for name, text in sheets:
        cleaned = clean_course_text(text)
        if len(cleaned) < 30:
            continue
        source_type = guess_source_type(url, cleaned, False)
        if source_type == "html_page":  # generic fallback is wrong for a sheet
            source_type = "curriculum_grid"
        fragments.append(_make_fragment(
            text=cleaned, url=url, university=university, media_type="xlsx",
            source_type=source_type, title=name, found_on_page=found_on_page,
            academic_level_hint=level_hint,
        ))
    if not fragments:
        return [_make_fragment(
            text="", url=url, university=university, media_type="xlsx",
            source_type="curriculum_grid", found_on_page=found_on_page,
            extraction_status="needs_manual_review",
        )]
    logger.debug(f"XLSX extracted: {len(fragments)} sheet fragment(s): {url}")
    return fragments


def _xlsx_to_sheets(content: bytes) -> list[tuple[str, str]] | None:
    """[(sheet_name, text)] via openpyxl, or None if unreadable."""
    try:
        import openpyxl
    except ImportError:
        logger.error("openpyxl not installed. Run: pip install openpyxl")
        return None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True,
                                    data_only=True)
    except Exception as e:
        logger.debug(f"openpyxl failed: {e}")
        return None
    out: list[tuple[str, str]] = []
    try:
        for ws in wb.worksheets:
            lines: list[str] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    break
                cells = [str(c).strip() for c in row
                         if c is not None and str(c).strip()]
                if cells:
                    lines.append(" | ".join(cells))
            out.append((str(ws.title), "\n".join(lines)))
    finally:
        wb.close()
    return out


def _xls_to_sheets(content: bytes) -> list[tuple[str, str]] | None:
    """Legacy .xls via xlrd, if installed. None → caller flags manual review."""
    try:
        import xlrd
    except ImportError:
        return None
    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as e:
        logger.debug(f"xlrd failed: {e}")
        return None
    out: list[tuple[str, str]] = []
    for sheet in book.sheets():
        lines: list[str] = []
        for r in range(min(sheet.nrows, MAX_SHEET_ROWS)):
            cells = [str(sheet.cell_value(r, c)).strip()
                     for c in range(sheet.ncols)
                     if str(sheet.cell_value(r, c)).strip()]
            if cells:
                lines.append(" | ".join(cells))
        out.append((sheet.name, "\n".join(lines)))
    return out


# ── RSS / SOCIAL EXTRACTORS (optional sources) ────────────────────────────────

def extract_from_rss_entry(entry: Any, source_name: str) -> dict:
    title = getattr(entry, "title", "") or ""
    summary = getattr(entry, "summary", "") or ""
    content = ""
    if hasattr(entry, "content") and entry.content:
        content = entry.content[0].get("value", "")
    full_text = clean_course_text(f"{title}\n{summary}\n{content}")
    return {
        "media_type": "html",
        "source_url": getattr(entry, "link", "") or "",
        "found_on_page": "",
        "pdf_url": "",
        "pdf_page": None,
        # CAMBIO (limpieza #2): faltaba, presente en todo lo demás que
        # produce el extractor (vía _make_fragment).
        "academic_level_hint": "",
        "source_type": "news",
        "title": title[:200],
        "raw_text": full_text,
        "university": "",
        "country": "",
        "country_code": "",
        "language": detect_language(full_text),
        "extraction_status": "extracted",
        "seed_origin": "",
        "rss_source": source_name,
    }


def extract_from_tweet(tweet: dict, query: str) -> dict:
    text = clean_course_text(tweet.get("text", ""))
    return {
        "media_type": "html", "source_url": f"https://twitter.com/i/web/status/{tweet.get('id', '')}",
        "found_on_page": "", "pdf_url": "", "pdf_page": None,
        "academic_level_hint": "",  # CAMBIO (limpieza #2)
        "source_type": "social",
        "title": text[:120], "raw_text": text, "university": "", "country": "",
        "country_code": "", "language": detect_language(text),
        "extraction_status": "extracted",
        "seed_origin": "", "search_query": query,
    }


def extract_from_reddit_post(post: dict, subreddit: str) -> dict:
    title = post.get("title", "")
    text = clean_course_text(f"{title}\n{post.get('selftext', '') or post.get('body', '')}")
    return {
        "media_type": "html", "source_url": post.get("url", ""), "found_on_page": "",
        "pdf_url": "", "pdf_page": None,
        "academic_level_hint": "",  # CAMBIO (limpieza #2)
        "source_type": "social", "title": title[:200],
        "raw_text": text, "university": "", "country": "", "country_code": "",
        "language": detect_language(text), "extraction_status": "extracted",
        # CAMBIO (limpieza #2): faltaba por completo en esta función (las
        # otras dos sí lo traían) — se agrega vacío por consistencia.
        "seed_origin": "",
        "subreddit": subreddit,
    }


__all__ = [
    "extract_from_html", "extract_from_pdf", "extract_from_xlsx",
    "extract_from_rss_entry", "extract_from_tweet", "extract_from_reddit_post",
    "is_likely_course_page",
]