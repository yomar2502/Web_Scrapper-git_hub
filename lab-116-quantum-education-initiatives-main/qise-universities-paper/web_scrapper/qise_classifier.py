import re
from urllib.parse import unquote

from keywords import (
    CORE_TERMS, ADJACENT_TERMS, GENERIC_TERMS, fold,
    find_quantum_matches, count_signals, detect_academic_level,
)
from utils import evidence_snippet, guess_course_title, get_logger

logger = get_logger("classifier")

_CATEGORY_TIER: dict[str, str] = {}
for _cat in CORE_TERMS:
    _CATEGORY_TIER[_cat] = "core"
for _cat in ADJACENT_TERMS:
    _CATEGORY_TIER[_cat] = "adjacent"
for _cat in GENERIC_TERMS:
    _CATEGORY_TIER[_cat] = "generic"

CLASSIFICATIONS = (
    "qise_core",
    "quantum_foundations_or_adjacent",
    "non_course_or_contextual",
    "unclear",
)

_CONTEXT_RADIUS = 300


def _context_window(text: str, start: int, end: int) -> str:
    return text[max(0, start - _CONTEXT_RADIUS): end + _CONTEXT_RADIUS]


_SENTENCE_BOUNDARY = re.compile(r"[.!?\n]")
_SENTENCE_CAP = 250

_PREREQ_MARKER = re.compile(
    r"(prerrequisitos?|pre-?requisitos?|prerequisites?)\s*:", re.IGNORECASE)


def _is_prereq_reference(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    return bool(_PREREQ_MARKER.search(fold(text[line_start:start])))


def _sentence_window(text: str, start: int, end: int) -> str:
    lo = max(0, start - _SENTENCE_CAP)
    hi = min(len(text), end + _SENTENCE_CAP)
    before, after = text[lo:start], text[end:hi]
    last = None
    for last in _SENTENCE_BOUNDARY.finditer(before):
        pass
    s = lo + last.end() if last else lo
    nxt = _SENTENCE_BOUNDARY.search(after)
    e = end + nxt.start() if nxt else hi
    return text[s:e]


class QISEClassifier:

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        logger.info(f"Classifier ready | categories={len(_CATEGORY_TIER)} "
                    f"(core={len(CORE_TERMS)} adjacent={len(ADJACENT_TERMS)})")

    def classify(self, fragment: dict) -> list[dict]:
        status = fragment.get("extraction_status", "extracted")
        text = fragment.get("raw_text", "") or ""

        if status in ("failed_pdf_extraction", "needs_manual_review"):
            return [self._manual_review_row(fragment)]

        matches = find_quantum_matches(text)
        if not matches:
            return []

        specific = [m for m in matches if m["tier"] in ("core", "adjacent")]
        chosen = specific if specific else matches

        source_type = fragment.get("source_type", "html_page")
        media = fragment.get("media_type", "html")
        coarse = bool(fragment.get("coarse"))

        # CAMBIO (bug): unquote() estaba DESPUÉS de la sustitución de
        # separadores. Si un separador venía percent-encoded en la URL
        # (ej. "%2D" en vez de "-", poco común pero ocurre con URLs
        # generadas por algunos CMS), el regex ya había consumido el "%"
        # antes de que unquote() pudiera decodificar nada, dejando basura
        # pegada al token siguiente sin espacio de separación (p. ej.
        # "posgrado" quedaba como "2Dposgrado", y el matcher de límite de
        # palabra ya no lo reconocía). unquote() debe decodificar PRIMERO,
        # y el regex tokenizar DESPUÉS sobre el texto ya decodificado.
        url_level = detect_academic_level(re.sub(
            r"[-_/.:+%?=&]+", " ",
            unquote(" ".join(fragment.get(k) or ""
                     for k in ("source_url", "pdf_url", "found_on_page")))))
        doc_level_hint = fragment.get("academic_level_hint", "")

        groups: dict[tuple[str, str], dict] = {}
        for m in chosen:
            if _is_prereq_reference(text, m["start"]):
                continue
            line_key = fold(guess_course_title(text, m["start"], m["end"]))
            # CAMBIO cuando guess_course_title() no encuentra un
            # encabezado (frecuente en fragmentos "coarse"/PDFs sin
            # estructura clara), devolvía "" — y TODAS las menciones de la
            # misma categoría sin título detectable colapsaban en un solo
            # grupo/fila, aunque fueran menciones genuinamente distintas en
            # partes distintas del texto (se perdía la evidencia de todas
            # menos una). Ahora, solo en ese caso, se usa la oración local
            # como parte de la clave — menciones en oraciones distintas
            # quedan en filas distintas; repeticiones LITERALES de la misma
            # oración (la intención original) siguen colapsando igual.
            if not line_key:
                line_key = fold(_sentence_window(text, m["start"], m["end"]))
            key = (m["category"], line_key)
            cur = groups.get(key)
            if cur is None or len(m["phrase"]) > len(cur["phrase"]):
                groups[key] = m

        all_phrases = sorted({m["phrase"] for m in chosen})

        rows: list[dict] = []
        for (cat, _line_key), m in groups.items():
            tier = _CATEGORY_TIER.get(cat, "generic")
            window = _context_window(text, m["start"], m["end"])
            strong = count_signals(window, "strong")
            weak = count_signals(window, "weak")
            noncourse = count_signals(window, "noncourse")
            sentence = _sentence_window(text, m["start"], m["end"])
            sent_strong = count_signals(sentence, "strong")
            sent_noncourse = count_signals(sentence, "noncourse")
            classification = self._decide(
                tier, strong, weak, noncourse, source_type, coarse,
                sent_strong=sent_strong, sent_noncourse=sent_noncourse,
            )
            confidence = self._confidence(
                classification, strong, noncourse, source_type
            )
            snippet = evidence_snippet(text, m["start"], m["end"])
            course_title = guess_course_title(text, m["start"], m["end"])
            if not course_title and media != "pdf":
                course_title = fragment.get("title", "")
            academic_level = (url_level
                              or detect_academic_level(window)
                              or doc_level_hint)
            rows.append({
                **fragment,
                "matched_keyword": m["phrase"],
                "matched_keywords": all_phrases,
                "semantic_category": cat,
                "keyword_tier": tier,
                "classification": classification,
                "confidence": confidence,
                "academic_level": academic_level,
                "course_title": course_title,
                "evidence_snippet": snippet,
                "explanation": (
                    f"tier={tier} cat={cat} strong={strong} weak={weak} "
                    f"noncourse={noncourse} sent_strong={sent_strong} "
                    f"sent_noncourse={sent_noncourse} src={source_type}/{media} "
                    f"level={academic_level or 'unknown'} "
                    f"→ {classification}/{confidence}"
                ),
            })
        return rows

    @staticmethod
    def _decide(tier, strong, weak, noncourse, source_type, coarse=False,
                sent_strong=0, sent_noncourse=0) -> str:
        formal_doc = source_type in ("syllabus", "curriculum_grid")
        course_page = source_type in ("syllabus", "curriculum_grid", "catalog",
                                      "course_list")
        contextual_src = source_type in ("news", "social")

        if contextual_src:
            return "non_course_or_contextual"
        if noncourse >= 1 and strong == 0 and not formal_doc:
            return "non_course_or_contextual"
        if sent_noncourse >= 1 and sent_strong == 0:
            return "non_course_or_contextual"

        has_course_ctx = (
            strong >= 1 or formal_doc or course_page or weak >= 2
        )

        if tier == "core":
            if coarse:
                return "qise_core" if strong >= 1 else "unclear"
            return "qise_core" if (has_course_ctx or weak >= 1) else "unclear"
        if tier == "adjacent":
            return "quantum_foundations_or_adjacent" if has_course_ctx else "unclear"
        return "unclear"

    @staticmethod
    def _confidence(classification, strong, noncourse, source_type) -> str:
        formal_doc = source_type in ("syllabus", "curriculum_grid")
        if classification in ("qise_core", "quantum_foundations_or_adjacent"):
            return "high" if (strong >= 1 or formal_doc) else "medium"
        if classification == "non_course_or_contextual":
            return "medium" if noncourse >= 1 else "low"
        return "low"

    @staticmethod
    def _manual_review_row(fragment: dict) -> dict:
        status = fragment.get("extraction_status", "needs_manual_review")
        note = ("PDF opened but little/no extractable text (likely scanned or "
                "image-based) — manual review needed."
                if status == "needs_manual_review"
                else "PDF could not be opened by any extraction engine.")
        return {
            **fragment,
            "matched_keyword": "",
            "matched_keywords": [],
            "semantic_category": "",
            "keyword_tier": "",
            "classification": "unclear",
            "confidence": "low",
            "academic_level": fragment.get("academic_level_hint", ""),
            "course_title": fragment.get("title", ""),
            "evidence_snippet": note,
            "explanation": f"extraction_status={status}",
        }