"""
keywords.py — Central taxonomy for quantum-related coursework detection.

Design goals (see project brief):
  * BROAD during discovery: match any quantum-related term in ES / PT / EN.
  * CONSERVATIVE during classification: keep three tiers so that
    "quantum computing" is never silently equated with "mecánica cuántica".

Everything here is plain data + a couple of tiny helpers so the taxonomy is
auditable in one place. The classifier and extractor both import from here.

Tiers
-----
  core      → Quantum Information Science & Engineering proper
              (computing, information, algorithms, cryptography, communication,
               sensing, technologies, engineering, software, hardware, circuits,
               programming, machine learning, error correction).
  adjacent  → Foundational / adjacent quantum fields
              (mechanics, physics, optics, chemistry, condensed matter,
               solid state, semiconductors, photonics, atomic/molecular,
               modern physics).
  generic   → Bare quantum stems ("cuántica", "quantum", "quântico"). These are
              the broad discovery net; on their own they cannot decide
              core-vs-adjacent, so they push a candidate toward `unclear`.

Matching is accent-folded (NFKD → ASCII, lowercased) so accented and
un-accented spellings collapse to the same form, which also neatly unifies the
heavy Spanish/Portuguese overlap.
"""

import re
import unicodedata


# ── ACCENT FOLDING ────────────────────────────────────────────────────────────

def fold(text: str) -> str:
    """Lowercase + strip diacritics so 'cuántica'/'quantica'/'quântica' unify."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """
    Like fold(), but also return index_map where index_map[i] is the index in
    the ORIGINAL string of the i-th folded character. Lets us report evidence
    snippets in the original (accented) text from matches found on folded text.
    """
    folded_chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            folded_chars.append(c.lower())
            index_map.append(i)
    return "".join(folded_chars), index_map


# ── QUANTUM TERM TAXONOMY ─────────────────────────────────────────────────────
# category -> list of surface phrases (ES / PT / EN mixed; folding handles accents)

CORE_TERMS: dict[str, list[str]] = {
    "quantum_computing": [
        "quantum computing", "quantum computation", "quantum computer",
        "computacion cuantica", "computação quantica", "computacao quantica",
        "computadora cuantica", "computador quantico",
    ],
    "quantum_information": [
        "quantum information", "informacion cuantica", "informacao quantica",
        "quantum information science", "qubit", "qubits", "cubit", "q-bit",
    ],
    "quantum_algorithms": [
        "quantum algorithm", "quantum algorithms", "algoritmo cuantico",
        "algoritmos cuanticos", "algoritmo quantico", "algoritmos quanticos",
        "grover algorithm", "shor algorithm", "quantum fourier transform",
        "quantum walk", "variational quantum eigensolver", "qaoa",
    ],
    "quantum_cryptography": [
        "quantum cryptography", "criptografia cuantica", "criptografia quantica",
        "quantum key distribution", "quantum-safe", "post-quantum cryptography",
        "criptografia post-cuantica", "bb84",
    ],
    "quantum_communication": [
        "quantum communication", "comunicacion cuantica", "comunicacao quantica",
        "quantum network", "quantum internet", "quantum teleportation",
        "teleportacion cuantica", "teletransporte cuantico",
    ],
    "quantum_sensing": [
        "quantum sensing", "quantum metrology", "quantum sensor",
        "sensores cuanticos", "sensores quanticos", "sensado cuantico",
        "metrologia cuantica",
    ],
    "quantum_technologies": [
        "quantum technologies", "quantum technology", "tecnologias cuanticas",
        "tecnologia cuantica", "tecnologias quanticas", "segunda revolucion cuantica",
        "second quantum revolution",
    ],
    "quantum_engineering": [
        "quantum engineering", "ingenieria cuantica", "engenharia quantica",
    ],
    "quantum_software": [
        "quantum software", "quantum programming", "programacion cuantica",
        "programacao quantica", "qiskit", "cirq", "pennylane", "ocean sdk",
    ],
    "quantum_hardware": [
        "quantum hardware", "superconducting qubit", "trapped ion", "spin qubit",
        "topological qubit", "quantum circuit", "quantum gate", "quantum processor",
        "circuito cuantico", "circuito quantico", "puerta cuantica", "porta quantica",
        "nisq",
    ],
    "quantum_machine_learning": [
        "quantum machine learning", "quantum neural network", "aprendizaje cuantico",
        "aprendizado quantico", "qml",
    ],
    "quantum_error_correction": [
        "quantum error correction", "correccion de errores cuanticos",
        "correcao de erros quanticos", "fault tolerant quantum", "surface code",
        "stabilizer code", "codigos estabilizadores",
    ],
}

ADJACENT_TERMS: dict[str, list[str]] = {
    "quantum_mechanics": [
        "quantum mechanics", "mecanica cuantica", "mecanica quantica",
    ],
    "quantum_physics": [
        "quantum physics", "fisica cuantica", "fisica quantica",
    ],
    "quantum_optics": [
        "quantum optics", "optica cuantica", "optica quantica",
    ],
    "quantum_chemistry": [
        "quantum chemistry", "quimica cuantica", "quimica quantica",
    ],
    "quantum_field_theory": [
        "quantum field theory", "teoria cuantica de campos", "teoria quantica de campos",
    ],
    "condensed_matter": [
        "condensed matter", "materia condensada", "materia condensada",
    ],
    "solid_state": [
        "solid state", "solid-state physics", "estado solido", "fisica del estado solido",
        "fisica do estado solido",
    ],
    "semiconductors": [
        "semiconductor", "semiconductors", "semiconductores", "semicondutores",
    ],
    "photonics": [
        "photonics", "photonic", "fotonica", "fotonica",
    ],
    "atomic_molecular": [
        "atomic physics", "molecular physics", "fisica atomica", "fisica molecular",
        "fisica atomica y molecular",
    ],
    "modern_physics": [
        "modern physics", "fisica moderna", "fisica contemporanea",
    ],
    "statistical_physics": [
        "statistical mechanics", "mecanica estadistica", "mecanica estatistica",
    ],
}

# Bare stems — the broad discovery net. Matched only if nothing more specific hit.
GENERIC_TERMS: dict[str, list[str]] = {
    "quantum_general": [
        "cuantica", "cuantico", "quantum", "quantica", "quantico",
    ],
}


# ── COURSE-CONTEXT SIGNALS ────────────────────────────────────────────────────
# STRONG signals almost always mean a formal course / syllabus / curriculum.
COURSE_SIGNALS_STRONG = [
    "silabo", "syllabus", "malla curricular", "plan de estudios", "plan de estudio",
    "grade curricular", "matriz curricular", "plano de ensino", "projeto pedagogico",
    "ementa", "creditos", "credito", "prerrequisito", "prerequisito", "prerequisite",
    "pre-requisito", "pre requisito", "codigo del curso", "codigo de la asignatura",
    "carga horaria", "horas teoricas", "horas practicas", "unidades de credito",
    "pensum", "cuadro de asignaturas",
]

# WEAK signals suggest an academic context but also appear on non-course pages.
COURSE_SIGNALS_WEAK = [
    "curso", "cursos", "asignatura", "asignaturas", "materia", "materias", "catedra",
    "disciplina", "disciplinas", "curriculo", "curriculum", "catalogo", "catalogue",
    "catalog", "programa", "pregrado", "posgrado", "postgrado", "licenciatura",
    "maestria", "doctorado", "electivo", "electiva", "optativa", "obligatorio",
    "obligatoria", "obrigatoria", "graduacao", "pos-graduacao", "mestrado", "doutorado",
    "course", "courses", "undergraduate", "graduate", "master", "doctoral", "elective",
    "required", "credits", "semestre", "semester", "trimestre", "objetivos",
    "bibliografia", "contenido", "temario", "competencias", "modulo", "unidad",
    "departamento", "facultad", "escuela", "faculty", "department",
]

# Signals that a page is contextual (NOT a formal course).
NONCOURSE_SIGNALS = [
    "seminario", "seminar", "webinar", "workshop", "taller", "conferencia",
    "conference", "congreso", "simposio", "symposium", "coloquio", "colloquium",
    "noticia", "noticias", "news", "blog", "boletin", "newsletter", "evento",
    "eventos", "event", "agenda", "convocatoria", "tesis", "thesis", "dissertacao",
    "disertacion", "grupo de investigacion", "research group", "linea de investigacion",
    "laboratorio de investigacion", "research laboratory", "divulgacion", "outreach",
    "escuela de verano", "summer school", "winter school", "charla", "talk",
    "comunicado", "press release", "prensa", "premio", "award", "publicacion",
    "paper", "articulo", "preprint", "revista", "journal", "proceedings",
    # Research-activity verb phrases ("what we research", not "what we teach").
    # Bare "investigacion" would be wrong here: course names like "Seminario de
    # Investigación" and "Trabajo de Investigación" are real coursework.
    "se investiga", "investigamos", "temas de investigacion",
    "areas de investigacion", "linhas de pesquisa", "pesquisamos",
]


# ── SEED-DISCOVERY / CRAWL-PRIORITY TERMS ─────────────────────────────────────
# Used by seed_discovery.py to score candidate seed URLs and by crawler.py to
# prioritize the crawl queue. Written unaccented — fold() unifies accents, and
# callers matching against URLs should first turn separators (-_/.) into spaces.

# Academic/course navigation terms (ES + PT + EN, merged & deduplicated).
ACADEMIC_SEED_TERMS = [
    # Spanish
    "cursos", "asignaturas", "materias", "plan de estudios", "malla curricular",
    "curriculo", "catalogo", "silabo", "syllabus", "pregrado", "posgrado",
    "licenciatura", "maestria", "doctorado", "facultad", "escuela",
    "departamento", "carrera", "programa", "oferta academica", "pensum",
    # Portuguese
    "disciplinas", "ementa", "grade curricular", "matriz curricular",
    "plano de ensino", "graduacao", "pos-graduacao", "mestrado", "doutorado",
    "faculdade", "escola", "carreira",
    # English
    "courses", "course catalog", "catalogue", "curriculum", "degree plan",
    "undergraduate", "graduate", "master", "doctoral", "faculty", "school",
    "department", "program", "academics",
]

# STEM areas where QISE coursework lives.
STEM_TERMS = [
    "fisica", "physics",
    "computacion", "computacao", "computing",
    "informatica", "computer science",
    "ingenieria", "engenharia", "engineering",
    "electronica", "eletronica", "electronics",
    "electrica", "eletrica", "electrical",
    "telecomunicaciones", "telecomunicacoes", "telecommunications",
    "matematica", "mathematics",
    "fotonica", "photonics",
    "ciencias", "sciences", "nanotecnologia", "nanotechnology",
]

# Academic level signals (ES / PT / EN) — used to label evidence rows as
# undergraduate vs graduate. Deliberately conservative: ambiguous terms that
# name BOTH levels ("grado" alone, "estudios") are excluded.
UNDERGRAD_SIGNALS = [
    "pregrado", "licenciatura", "bachillerato", "carrera profesional",
    "undergraduate", "bachelor", "graduacao", "bacharelado",
    "estudios generales",
]

POSTGRAD_SIGNALS = [
    "posgrado", "postgrado", "escuela de posgrado", "maestria", "magister",
    "doctorado", "graduate school", "master of science", "masters", "msc",
    "doctoral", "phd", "pos-graduacao", "mestrado", "doutorado",
    # NOT "especializacion"/"diplomado": undergrad plans routinely say
    # "electivo de especialización", so those would mislabel pregrado rows.
]


def detect_academic_level(text: str) -> str:
    """
    "undergraduate" / "graduate" / "" from level terms in `text` (folded,
    plural-tolerant). Conservative: both kinds present, or neither → "".
    """
    under = match_terms(text, UNDERGRAD_SIGNALS)
    post = match_terms(text, POSTGRAD_SIGNALS)
    if under and not post:
        return "undergraduate"
    if post and not under:
        return "graduate"
    return ""


# Pages to crawl LAST (never discarded — news can hold contextual quantum
# evidence — but they must not eat the page budget before curricula).
LOW_PRIORITY_TERMS = [
    "noticia", "noticias", "news", "blog", "boletin",
    "evento", "eventos", "events", "agenda", "calendario",
    "prensa", "press", "comunicado", "sala de prensa",
    "admision", "admisiones", "admission", "admissions", "vestibular",
    "alumni", "egresados", "exalumnos",
    "deporte", "deportes", "sports", "atletismo",
    "transparencia", "administrativo", "administrativa", "licitaciones",
    "donaciones", "donations", "tienda", "store",
]


# ── COMPILED MATCHERS ─────────────────────────────────────────────────────────
_PLURAL_SUFFIX = r"(?:es|s)?"

def _compile_phrase(phrase: str) -> re.Pattern:
    """Word-boundary regex on the folded phrase; tolerant of internal whitespace."""
    folded = fold(phrase)
    tokens = folded.split()
    body = r"\s+".join(re.escape(t) for t in tokens)
    return re.compile(r"(?<![\w])" + body + r"(?![\w])")


def _build_term_matchers() -> list[tuple[re.Pattern, str, str, str]]:
    """Return [(regex, phrase, category, tier)] ordered core → adjacent → generic."""
    matchers: list[tuple[re.Pattern, str, str, str]] = []
    for tier, table in (("core", CORE_TERMS),
                        ("adjacent", ADJACENT_TERMS),
                        ("generic", GENERIC_TERMS)):
        for category, phrases in table.items():
            for phrase in phrases:
                matchers.append((_compile_phrase(phrase), phrase, category, tier))
    return matchers


_TERM_MATCHERS = _build_term_matchers()
_STRONG_MATCHERS = [( _compile_phrase(s), s) for s in COURSE_SIGNALS_STRONG]
_WEAK_MATCHERS = [(_compile_phrase(s), s) for s in COURSE_SIGNALS_WEAK]
_NONCOURSE_MATCHERS = [(_compile_phrase(s), s) for s in NONCOURSE_SIGNALS]


# ── PUBLIC API ────────────────────────────────────────────────────────────────

# Cap occurrences per phrase per fragment: enough for any real curriculum
# while bounding pathological pages that repeat a term hundreds of times.
MAX_OCCURRENCES_PER_PHRASE = 20


def find_quantum_matches(text: str) -> list[dict]:
    """
    Find every quantum term in `text` — ALL occurrences of each phrase, not
    just the first: a curriculum can list several courses of the same category
    ("Mecánica Cuántica 1", "Mecánica Cuántica 2", "Mecánica Cuántica
    Relativista") and each mention must surface as its own match.

    Returns a list of dicts: {phrase, category, tier, start, end} where
    start/end index into the ORIGINAL `text` (mapped back through accent
    folding), so callers can slice accurate, accented evidence snippets.
    """
    folded, index_map = fold_with_map(text)
    n = len(folded)
    out: list[dict] = []
    for regex, phrase, category, tier in _TERM_MATCHERS:
        for count, m in enumerate(regex.finditer(folded)):
            if count >= MAX_OCCURRENCES_PER_PHRASE:
                break
            fs, fe = m.start(), m.end()
            start = index_map[fs] if fs < n else (index_map[-1] + 1 if index_map else 0)
            end = (index_map[fe - 1] + 1) if 0 < fe <= n else start
            out.append({
                "phrase": phrase, "category": category, "tier": tier,
                "start": start, "end": end,
            })
    return out


def has_quantum_term(text: str) -> bool:
    folded = fold(text)
    return any(regex.search(folded) for regex, *_ in _TERM_MATCHERS)


_PHRASE_CACHE: dict[str, re.Pattern] = {}

def _compile_phrase_plural(phrase: str) -> re.Pattern:
    """Alias de _compile_phrase — mantenido por compatibilidad de nombre."""
    return _compile_phrase(phrase)

def match_terms(text: str, terms: list[str]) -> list[str]:
    """
    Return the subset of `terms` present in `text` (accent-folded, word-bounded,
    plural-tolerant). Compiled patterns are cached, so passing the module-level
    term lists is cheap. Callers matching URLs should first replace separators
    (-_/.) with spaces.
    """
    folded = fold(text)
    hits: list[str] = []
    for term in terms:
        pat = _PHRASE_CACHE.get(term)
        if pat is None:
            pat = _PHRASE_CACHE[term] = _compile_phrase_plural(term)
        if pat.search(folded):
            hits.append(term)
    return hits


def count_signals(text: str, which: str) -> int:
    folded = fold(text)
    matchers = {
        "strong": _STRONG_MATCHERS,
        "weak": _WEAK_MATCHERS,
        "noncourse": _NONCOURSE_MATCHERS,
    }[which]
    return sum(1 for regex, _ in matchers if regex.search(folded))


def list_signals(text: str, which: str) -> list[str]:
    folded = fold(text)
    matchers = {
        "strong": _STRONG_MATCHERS,
        "weak": _WEAK_MATCHERS,
        "noncourse": _NONCOURSE_MATCHERS,
    }[which]
    return [phrase for regex, phrase in matchers if regex.search(folded)]
