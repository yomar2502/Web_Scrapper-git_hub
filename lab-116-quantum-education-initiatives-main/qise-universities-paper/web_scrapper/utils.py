"""
utils.py — Logging, rate-limiting, URL, robots and text helpers.
"""

import hashlib
import logging
import re
import time
import unicodedata
import urllib.robotparser
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from typing import Optional, Dict, List, Tuple

import requests


# ── LOGGING ───────────────────────────────────────────────────────────────────

def get_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    """
    Configure and return a logger with both file and console handlers.
    
    Args:
        name: Logger name
        log_dir: Directory to store log files
        
    Returns:
        Configured logger instance
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = Path(log_dir) / f"qise_{timestamp}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

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

    def wait(self) -> None:
        """Wait if necessary to maintain minimum delay between calls."""
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
    """
    Normalize unicode, collapse whitespace, strip control chars.
    
    Args:
        text: Input text to normalize
        
    Returns:
        Cleaned and normalized text
    """
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
    """Simple text normalization for comparison purposes."""
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def clean_course_text(text: str) -> str:
    """
    Deep cleaning for course descriptions before classification.
    
    Args:
        text: Raw course text
        
    Returns:
        Cleaned text ready for classification
    """
    text = normalize_text(text)
    
    # Remove HTML entities
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    
    # Remove URLs and emails
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\S+@\S+\.\S+", "", text)
    
    # Remove page numbers and similar noise
    text = re.sub(r"[Pp]ágina\s+\d+\s+de\s+\d+", "", text)
    text = re.sub(r"[Pp]age\s+\d+\s+of\s+\d+", "", text)
    
    # Final whitespace cleanup
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    
    return text.strip()


def extract_course_code(text: str) -> Optional[str]:
    """
    Extract a course code like FIS-3210, PHYS301, CS 4820.
    
    Args:
        text: Text containing potential course code
        
    Returns:
        Extracted course code or None if not found
    """
    pattern = r"\b([A-Z]{2,5}[-\s]?\d{3,5}[A-Z]?)\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "_", text)


def detect_language(text: str) -> str:
    """
    Lightweight heuristic language detection (es / pt / en).
    Designed for academic course texts in Latin America.
    
    Args:
        text: Text to analyze
        
    Returns:
        Detected language code: 'es', 'pt', 'en', or 'unknown'
    """
    if not text:
        return "unknown"

    text_lower = normalize(text)

    es_markers = [
        "curso", "asignatura", "creditos", "prerrequisito", "semestre",
        "computacion", "fisica", "informacion", "cuantico", "plan de estudios"
    ]

    pt_markers = [
        "curso", "disciplina", "creditos", "pre-requisito", "semestre",
        "computacao", "fisica", "informacao", "quantico", "ementa", "graduacao"
    ]

    en_markers = [
        "course", "credits", "prerequisite", "semester"
    ]

    technical_markers = [
        "quantum", "computing", "physics", "information"
    ]

    # Score each language
    scores = {
        "es": sum(marker in text_lower for marker in es_markers),
        "pt": sum(marker in text_lower for marker in pt_markers),
        "en": sum(marker in text_lower for marker in en_markers),
    }

    technical_score = sum(marker in text_lower for marker in technical_markers)

    # If it's clearly English
    if (scores["en"] >= 2 or technical_score >= 2) and scores["es"] == 0 and scores["pt"] == 0:
        return "en"

    # If no evidence
    if max(scores.values()) == 0:
        return "unknown"

    # Resolve Spanish-Portuguese ties
    if scores["es"] == scores["pt"]:
        pt_exclusive = ["disciplina", "computacao", "informacao", "quantico", "ementa", "graduacao"]
        es_exclusive = ["asignatura", "computacion", "informacion", "cuantico", "plan de estudios", "malla curricular"]

        if any(word in text_lower for word in pt_exclusive):
            return "pt"
        if any(word in text_lower for word in es_exclusive):
            return "es"
        if technical_score >= 2:
            return "es"

    # Return the language with highest score
    max_score = max(scores.values())
    if list(scores.values()).count(max_score) > 1:
        return "unknown"

    return max(scores, key=scores.get)


def truncate_text(text: str, max_chars: int = 5000) -> str:
    """Truncate text to max_chars, preserving whole words."""
    if len(text) <= max_chars:
        return text
    
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] + "…" if last_space > 0 else truncated + "…"


def is_likely_course_page(url: str, text: str) -> bool:
    """
    Heuristic: determine if URL/text looks like a course or syllabus page.
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

    snippet = text[:2000]
    text_hit = count_signals(snippet, "strong") >= 1 or count_signals(snippet, "weak") >= 2

    return url_hit or text_hit


# ── URL HELPERS ───────────────────────────────────────────────────────────────

_TRACKING_PARAMS = re.compile(
    r"^(utm_|_gl$|gclid$|fbclid$|_ga$|mc_|tmpl$|print$)", 
    re.IGNORECASE
)


def normalize_url(url: str, base: Optional[str] = None) -> str:
    """
    Resolve URL against base and canonicalize it to deduplicate trivially
    different spellings of the same page.
    
    Args:
        url: URL to normalize
        base: Base URL for resolution
        
    Returns:
        Normalized URL or empty string if invalid
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
    
    if not netloc:
        return ""

    # Drop default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Filter tracking params
    query_pairs = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k)
    ]
    query = urlencode(query_pairs)

    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, "", query, ""))


# Second-level public suffixes common in LatAm (and beyond)
_SLD_SUFFIXES = re.compile(r"^(edu|com|org|net|gob|gov|mil|ac)\.[a-z]{2}$")


def registered_domain(netloc: str) -> str:
    """
    Extract the registrable domain from a host string.
    
    Examples:
        portal.uni.edu.pe → uni.edu.pe
        www5.usp.br → usp.br
        fc.uni.edu.pe → uni.edu.pe
        
    Args:
        netloc: Network location (hostname)
        
    Returns:
        Registrable domain
    """
    host = (netloc or "").lower().split(":")[0].strip(".")
    labels = host.split(".")
    
    if len(labels) <= 2:
        return host
    
    if _SLD_SUFFIXES.match(".".join(labels[-2:])):
        return ".".join(labels[-3:])
    
    return ".".join(labels[-2:])


def same_registered_domain(url: str, allowed_netloc: str) -> bool:
    """
    Check if URL's host equals or is a subdomain of allowed_netloc.
    
    Args:
        url: URL to check
        allowed_netloc: Allowed network location
        
    Returns:
        True if URL belongs to the same domain
    """
    host = urlparse(url).netloc.lower().split(":")[0]
    allowed = allowed_netloc.lower().split(":")[0]
    
    if not host or not allowed:
        return False
    
    return host == allowed or host.endswith("." + allowed)


def url_hash(url: str) -> str:
    """Generate a stable short hash of a URL for cache filenames."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def looks_like_pdf_url(url: str) -> bool:
    """Determine if URL likely points to a PDF file."""
    low = url.lower().split("#")[0]
    path = urlparse(low).path
    
    if path.endswith(".pdf"):
        return True
    
    # Common non-.pdf giveaways in LatAm CMS
    return bool(re.search(r"(format=pdf|type=pdf|\.pdf[?&]|/pdf/|filetype=pdf)", low))


# ── EXTERNAL DOCUMENT HOSTS ──────────────────────────────────────────────────

_DRIVE_FILE_RE = re.compile(r"https?://drive\.google\.com/file/d/([\w-]+)")
_GDOCS_SHEET_RE = re.compile(r"https?://docs\.google\.com/spreadsheets/d/([\w-]+)")
_SHAREPOINT_SHARE_RE = re.compile(
    r"https?://[\w.-]+\.sharepoint\.com/:[xwbp]:/", 
    re.IGNORECASE
)


def external_doc_download_url(url: str) -> str:
    """
    Convert Google Drive, Google Sheets, or SharePoint share links to
    direct-download form. Returns empty string if not a recognized format.
    """
    if not url:
        return ""
    
    # Google Drive file
    m = _DRIVE_FILE_RE.match(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    
    # Google Sheets
    m = _GDOCS_SHEET_RE.match(url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    
    # SharePoint / OneDrive
    if _SHAREPOINT_SHARE_RE.match(url) or url.startswith(
            ("https://onedrive.live.com/", "http://onedrive.live.com/")):
        if "download=1" not in url:
            separator = "&" if "?" in url else "?"
            return url + separator + "download=1"
        return url
    
    return ""


def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ── PAGE-KIND / SOURCE-TYPE HEURISTIC ──────────────────────────────────────

def guess_source_type(url: str, text: str, is_pdf: bool) -> str:
    """
    Best-effort label for the research 'source type' column.
    
    Returns one of:
        syllabus | curriculum_grid | catalog | department_page | course_list | pdf | html_page
    """
    hay = (url + " " + text[:1500]).lower()
    hay = unicodedata.normalize("NFKD", hay)
    hay = "".join(c for c in hay if not unicodedata.combining(c))

    def has(*words: str) -> bool:
        return any(w in hay for w in words)

    if has("silabo", "syllabus", "plano de ensino", "ementa", "programa del curso", "programa de la asignatura"):
        return "syllabus"
    
    if has("malla curricular", "matriz curricular", "grade curricular", "pensum", "plan de estudios"):
        return "curriculum_grid"
    
    if has("catalogo", "catalog", "oferta academica", "cursos de posgrado", "cursos de pregrado", 
           "lista de cursos", "relacion de asignaturas"):
        return "catalog"
    
    if has("/departamento", "department", "departamento de", "instituto de fisica", 
           "facultad de", "faculty of"):
        return "department_page"
    
    if has("curso", "asignatura", "disciplina", "course", "materia"):
        return "course_list"
    
    return "pdf" if is_pdf else "html_page"


# ── EVIDENCE SNIPPET / COURSE TITLE ─────────────────────────────────────────

def evidence_snippet(text: str, start: int, end: int, radius: int = 140) -> str:
    """
    Extract a one-line window of text around [start, end) for auditability.
    
    Args:
        text: Source text
        start: Start position
        end: End position
        radius: Number of characters before/after the match
        
    Returns:
        Snippet with ellipsis indicators
    """
    if not text:
        return ""
    
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    
    frag = text[lo:hi].replace("\n", " ")
    frag = re.sub(r"\s+", " ", frag).strip()
    
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    
    return f"{prefix}{frag}{suffix}"


_ABBREV_RE = re.compile(r"\b(Ing|Lic|Dr|Dra|Mg|M\.Sc|MSc|Prof|Univ|Av|Jr|Sr|Sra)\.\s*$")


def _looks_like_heading(line: str) -> bool:
    """Check if a line looks like a course title/heading."""
    if not (3 <= len(line) <= 90):
        return False
    
    # Check for sentence endings (not abbreviations)
    for m in re.finditer(r"\.\s", line):
        before = line[:m.start() + 1]
        if not _ABBREV_RE.search(before):
            return False
    
    return line[0].isupper() or line[0].isdigit()


def guess_course_title(text: str, start: int, end: int) -> str:
    """
    Best-effort course title extraction.
    
    Returns the line containing the matched keyword, or if that line is prose,
    the nearest heading-like line above it. Returns empty string if nothing
    title-shaped is found.
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
    
    # Scan upward for a heading
    above = text[:line_start].splitlines()
    for raw in reversed(above[-8:]):
        prev = re.sub(r"\s+", " ", raw).strip(" |\t·-—")
        if prev and _looks_like_heading(prev):
            return prev
    
    # Fallback: prose line if it's reasonably short
    if 3 <= len(line) <= 160:
        return line
    
    return ""


# ── ROBOTS.TXT ────────────────────────────────────────────────────────────────

class RobotsCache:
    """
    Per-host robots.txt cache. Fails open if robots.txt can't be fetched or parsed.
    Many LatAm university servers return odd status codes for /robots.txt.
    """

    def __init__(self, user_agent: str, enabled: bool = True, logger=None):
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._logger = logger

    def allowed(self, url: str) -> bool:
        """Check if crawling a URL is allowed by robots.txt."""
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
        """Load and parse robots.txt for a host."""
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(host + "/robots.txt")
        
        try:
            resp = requests.get(
                host + "/robots.txt",
                timeout=(5, 8),
                headers={"User-Agent": self.user_agent},
            )
            
            if resp.status_code >= 400:
                return None
            
            rp.parse(resp.text.splitlines())
            return rp
            
        except Exception as e:
            if self._logger:
                self._logger.debug(f"robots.txt unreadable for {host}: {e} (allowing)")
            return None