"""
utils.py — Logging, rate-limiting, URL, robots and text helpers.
"""

import hashlib
import logging
import re
import time
import unicodedata
import urllib.robotparser

import requests

from pathlib import Path
from functools import wraps
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode


# ── LOGGING ───────────────────────────────────────────────────────────────────

def get_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = Path(log_dir) / f"qise_{timestamp}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — full debug log
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler — info and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── RATE LIMITER ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple fixed-delay rate limiter for sequential crawling."""

    def __init__(self, min_delay: float = 2.0):
        self.min_delay = min_delay
        self._last_call: float = 0.0

    def wait(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self._last_call = time.time()

    def __call__(self, fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            self.wait()
            return fn(*args, **kwargs)
        return wrapper


# ── TEXT CLEANING ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Normalize unicode, collapse whitespace, strip control chars."""
    if not text:
        return ""
    # NFKC normalization (handles ligatures, half-width chars, etc.)
    text = unicodedata.normalize("NFKC", text)
    # Remove control characters except newline and tab
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse multiple spaces / tabs
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse 3+ consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    return text.lower()


def clean_course_text(text: str) -> str:
    """Deeper cleaning for course descriptions before classification."""
    text = normalize_text(text)
    # Remove HTML entities that slipped through
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove emails
    text = re.sub(r"\S+@\S+\.\S+", "", text)
    # Remove page numbers / noise patterns like "Página 3 de 12"
    text = re.sub(r"[Pp]ágina\s+\d+\s+de\s+\d+", "", text)
    text = re.sub(r"[Pp]age\s+\d+\s+of\s+\d+", "", text)
    # Collapse again after removals
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_course_code(text: str) -> str | None:
    """Try to extract a course code like FIS-3210, PHYS301, CS 4820."""
    pattern = r"\b([A-Z]{2,5}[-\s]?\d{3,5}[A-Z]?)\b"
    match = re.search(
    pattern,
    text,
    flags=re.IGNORECASE
    )
    return match.group(1) if match else None


def slugify(text: str) -> str:
    """Convert university name to filesystem-safe slug."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "_", text)


def detect_language(text: str) -> str:
    """
    Lightweight heuristic language detection (es / pt / en).
    Designed for academic course texts in Latin America.
    """

    if not text:
        return "unknown"

    text_lower = normalize(text)

    es_markers = [
        "curso",
        "asignatura",
        "creditos",
        "prerrequisito",
        "semestre",
        "computacion",
        "fisica",
        "informacion",
        "cuantico",
        "plan de estudios",
    ]

    pt_markers = [
        "curso",
        "disciplina",
        "creditos",
        "pre-requisito",
        "semestre",
        "computacao",
        "fisica",
        "informacao",
        "quantico",
        "ementa",
        "graduacao",
    ]

    en_markers = [
        "course",
        "credits",
        "prerequisite",
        "semester",
    ]

    technical_markers = [
        "quantum",
        "computing",
        "physics",
        "information",
    ]

    english_academic_markers = [
        "introduction to",
        "advanced",
        "theory",
        "algorithms",
        "mechanics",
    ]

    scores = {
        "es": sum(marker in text_lower for marker in es_markers),
        "pt": sum(marker in text_lower for marker in pt_markers),
        "en": sum(marker in text_lower for marker in en_markers),
    }

    technical_score = sum(
        marker in text_lower
        for marker in technical_markers
    )

    if (
            (scores["en"] >= 2 or technical_score >= 2)
            and scores["es"] == 0
            and scores["pt"] == 0
        ):
            return "en"

    # Sin evidencia
    if max(scores.values()) == 0:
        return "unknown"

    
    # Resolver empate español-portugués
    if scores["es"] == scores["pt"]:

        pt_exclusive = [
            "disciplina",
            "computacao",
            "informacao",
            "quantico",
            "ementa",
            "graduacao",
        ]

        es_exclusive = [
            "asignatura",
            "computacion",
            "informacion",
            "cuantico",
            "plan de estudios",
            "malla curricular",
        ]

        if any(word in text_lower for word in pt_exclusive):
            return "pt"

        if any(word in text_lower for word in es_exclusive):
            return "es"

        if technical_score >= 2:
            return "es"

    max_score = max(scores.values())

    if list(scores.values()).count(max_score) > 1:
        return "unknown"

    # Si no hay evidencia suficiente, usar el mayor puntaje
    return max(scores, key=scores.get)


def truncate_text(text: str, max_chars: int = 5000) -> str:
    """Truncate to max_chars, preserving whole words."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] + "…" if last_space > 0 else truncated


def is_likely_course_page(url: str, text: str) -> bool:
    """
    Heuristic: does this URL / text look like a course or syllabus page?
    Used to filter irrelevant crawled pages early.
    """
    from keywords import count_signals
 
    url_signals = [
        "curso", "course", "syllabus", "silabo", "sílabo",
        "asignatura", "disciplina", "programa", "materia",
        "catalogo", "catalog", "oferta", "pensum",
    ]
    url_lower = url.lower()
    url_hit = any(s in url_lower for s in url_signals)
 
    # keywords.count_signals ya hace folding de tildes y tolera plural;
    # umbral (strong>=1 o weak>=2) elegido para mantener la sensibilidad
    # similar a la versión original (que pedía >=2 coincidencias de texto).
    snippet = text[:2000]
    text_hit = count_signals(snippet, "strong") >= 1 or count_signals(snippet, "weak") >= 2
 
    return url_hit or text_hit

# ── URL HELPERS ───────────────────────────────────────────────────────────────

_TRACKING_PARAMS = re.compile(
    # tracking params + print-view params (Joomla's ?tmpl=component&print=1
    # serves the same page again and duplicates every row scraped from it)
    r"^(utm_|_gl$|gclid$|fbclid$|_ga$|mc_|tmpl$|print$)", re.IGNORECASE)


def normalize_url(url: str, base: str | None = None) -> str:
    """
    Resolve `url` against `base` (if given) and canonicalize it so that trivially
    different spellings of the same page dedupe to one string:
      * scheme + host lowercased, default ports dropped
      * fragment removed
      * tracking query params (utm_*, _gl, gclid, ...) removed
      * trailing slash stripped (except for the bare root path)
    Returns "" if the URL is empty or not http(s).
    """
    if not url:
        return ""
    url = url.strip()
    if base:
        url = urljoin(base, url)
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if p.scheme and p.scheme not in ("http", "https"):
        return ""
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    # Drop default ports
    if not netloc:
        return ""
 
    # Drop default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    # Filter tracking params, preserve everything else (PDF ids etc. matter)
    query_pairs = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k)
    ]
    query = urlencode(query_pairs)
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", query, ""))


# Second-level public suffixes common in LatAm (and beyond): under these, the
# registrable domain has THREE labels (uni.edu.pe), not two (edu.pe).
_SLD_SUFFIXES = re.compile(
    r"^(edu|com|org|net|gob|gov|mil|ac)\.[a-z]{2}$")


def registered_domain(netloc: str) -> str:
    """
    The registrable domain of a host — the unit a university "owns":
      portal.uni.edu.pe → uni.edu.pe     www5.usp.br → usp.br
      fc.uni.edu.pe     → uni.edu.pe     utec.edu.pe → utec.edu.pe
    Used as the crawl scope so faculty/portal sibling subdomains stay in
    bounds while other institutions under edu.pe do not.
    """
    host = (netloc or "").lower().split(":")[0].strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if _SLD_SUFFIXES.match(".".join(labels[-2:])):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_registered_domain(url: str, allowed_netloc: str) -> bool:
    """True if `url`'s host equals or is a subdomain of `allowed_netloc`."""
    host = urlparse(url).netloc.lower().split(":")[0]
    allowed = allowed_netloc.lower().split(":")[0]
    if not host or not allowed:
        return False
    return host == allowed or host.endswith("." + allowed)


def url_hash(url: str) -> str:
    """Stable short hash of a URL — used for cache filenames."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def looks_like_pdf_url(url: str) -> bool:
    """Cheap URL-only guess. Real detection also uses Content-Type + %PDF header."""
    low = url.lower().split("#")[0]
    path = urlparse(low).path
    if path.endswith(".pdf"):
        return True
    # Common non-.pdf giveaways in LatAm CMS download endpoints
    return bool(re.search(r"(format=pdf|type=pdf|\.pdf[?&]|/pdf/|filetype=pdf)", low))


# ── EXTERNAL DOCUMENT HOSTS (Google Drive / SharePoint embeds) ────────────────
# LatAm universities often embed their planes de estudio as Google Drive
# iframes or SharePoint/OneDrive spreadsheets. These helpers turn share/preview
# links into direct-download URLs. Documents only — never crawl surface.

_DRIVE_FILE_RE = re.compile(r"https?://drive\.google\.com/file/d/([\w-]+)")
_GDOCS_SHEET_RE = re.compile(r"https?://docs\.google\.com/spreadsheets/d/([\w-]+)")
_SHAREPOINT_SHARE_RE = re.compile(
    r"https?://[\w.-]+\.sharepoint\.com/:[xwbp]:/", re.IGNORECASE)


def external_doc_download_url(url: str) -> str:
    """
    If `url` is a Google Drive file preview, Google Sheets, or SharePoint/
    OneDrive share link, return the direct-download form. "" otherwise.
    """
    if not url:
        return ""
    m = _DRIVE_FILE_RE.match(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = _GDOCS_SHEET_RE.match(url)
    if m:
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
                f"/export?format=xlsx")
    if _SHAREPOINT_SHARE_RE.match(url) or url.startswith(
            ("https://onedrive.live.com/", "http://onedrive.live.com/")):
        if "download=1" in url:
            return url
        return url + ("&" if "?" in url else "?") + "download=1"
    return ""


def now_iso() -> str:
    """UTC timestamp, timezone-aware, ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


# ── PAGE-KIND / SOURCE-TYPE HEURISTIC ─────────────────────────────────────────

def guess_source_type(url: str, text: str, is_pdf: bool) -> str:
    """
    Best-effort label for the research 'source type' column. Returns one of:
      syllabus | curriculum_grid | catalog | department_page | course_list |
      pdf | html_page
    """
    hay = (url + " " + text[:1500]).lower()
    hay = unicodedata.normalize("NFKD", hay)
    hay = "".join(c for c in hay if not unicodedata.combining(c))

    def has(*words):
        return any(w in hay for w in words)

    if has("silabo", "syllabus", "plano de ensino", "ementa", "programa del curso",
           "programa de la asignatura"):
        return "syllabus"
    if has("malla curricular", "matriz curricular", "grade curricular", "pensum",
           "plan de estudios", "plan de estudio"):
        return "curriculum_grid"
    if has("catalogo", "catalog", "oferta academica", "oferta-academica",
           "cursos de posgrado", "cursos de pregrado", "lista de cursos",
           "relacion de asignaturas"):
        return "catalog"
    if has("/departamento", "department", "departamento de", "instituto de fisica",
           "facultad de", "faculty of"):
        return "department_page"
    if has("curso", "asignatura", "disciplina", "course", "materia"):
        return "course_list"
    return "pdf" if is_pdf else "html_page"


# ── EVIDENCE SNIPPET / COURSE TITLE ───────────────────────────────────────────

def evidence_snippet(text: str, start: int, end: int, radius: int = 140) -> str:
    """A one-line window of `text` around [start, end), for auditability."""
    if not text:
        return ""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    frag = text[lo:hi].replace("\n", " ")
    frag = re.sub(r"\s+", " ", frag).strip()
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{frag}{suffix}"

_ABBREV_RE = re.compile(
    r"\b(Ing|Lic|Dr|Dra|Mg|M\.Sc|MSc|Prof|Univ|Av|Jr|Sr|Sra)\.\s*$"
)

def _looks_like_heading(line: str) -> bool:
    """Course-title shaped: short, no sentence break, starts upper/digit."""
    if not (3 <= len(line) <= 90):
        return False
    for m in re.finditer(r"\.\s", line):
        before = line[:m.start() + 1]  # incluye el punto, no el espacio
        if not _ABBREV_RE.search(before):
            return False  # punto de fin de oración real → es prosa, no título
    return line[0].isupper() or line[0].isdigit()



def guess_course_title(text: str, start: int, end: int) -> str:
    """
    Best-effort course title: the line containing the matched keyword — or,
    when that line is prose (a syllabus content sentence like "Fundamentos de
    la mecánica estadística cuántica. Matriz, ..."), the nearest heading-like
    line above it (usually "MF604 Mecánica Estadística Cuántica" in catalogs).
    Empty string if nothing title-shaped is found.
    """
    if not text:
        return ""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    line = re.sub(r"\s+", " ", line).strip(" |\t·-—")
    if _looks_like_heading(line):
        return line
    # Match sits inside prose: scan a few lines upward for the course heading
    # this content belongs to.
    above = text[:line_start].splitlines()
    for raw in reversed(above[-8:]):
        prev = re.sub(r"\s+", " ", raw).strip(" |\t·-—")
        if prev and _looks_like_heading(prev):
            return prev
    # Fall back to the prose line itself when it is at least title-sized.
    if 3 <= len(line) <= 160:
        return line
    return ""


# ── ROBOTS.TXT ────────────────────────────────────────────────────────────────

class RobotsCache:
    """
    Per-host robots.txt cache. Fails OPEN: if robots.txt can't be fetched or
    parsed we allow the URL (and log once), because many LatAm university servers
    return odd status codes for /robots.txt.
    """

    def __init__(self, user_agent: str, enabled: bool = True, logger=None):
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._logger = logger

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._parsers:
            self._parsers[host] = self._load(host)
        parser = self._parsers[host]
        if parser is None:
            return True  # fail open
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _load(self, host: str):
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(host + "/robots.txt")
        try:
            resp = requests.get(
                host + "/robots.txt",
                timeout=(5, 8),  # (connect, read) — igual de estricto que crawler.py
                headers={"User-Agent": self.user_agent},
            )
            if resp.status_code >= 400:
                # Sin robots.txt (404) o inaccesible → fail open, como antes.
                return None
            rp.parse(resp.text.splitlines())
            return rp
        except Exception as e:
            if self._logger:
                self._logger.debug(f"robots.txt unreadable for {host}: {e} (allowing)")
            return None
