"""
seed_discovery.py — Automatic seed URL discovery from sitemaps and homepage links.
"""

import gzip
import re
from urllib.parse import urlparse, unquote
from typing import List, Tuple, Dict, Optional, Set, Any
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from crawler import ACADEMIC_UA, WebCrawler
from keywords import (
    ACADEMIC_SEED_TERMS, STEM_TERMS, LOW_PRIORITY_TERMS, match_terms,
)
from utils import (
    RateLimiter, RobotsCache, get_logger,
    normalize_url, same_registered_domain, registered_domain, looks_like_pdf_url,
)

logger = get_logger("seed_discovery")

# ── CONSTANTS ──────────────────────────────────────────────────────────────────

MAX_FETCH_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_SITEMAP_FETCHES = 6
MAX_CHILD_SITEMAPS = 5
MAX_URLS_PER_SITEMAP = 3000
MAX_CANDIDATES = 8000
MIN_SEED_SCORE = 3
DEFAULT_MAX_SEEDS = 20

# ── REGULAR EXPRESSIONS ──────────────────────────────────────────────────────

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_DECL_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_URL_SEPARATORS = re.compile(r"[-_/.+%?=&#]+")
_GZIP_MAGIC = b"\x1f\x8b"


# ── DATA CLASSES ─────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """Represents a discovered seed candidate."""
    url: str
    source: str
    anchor: str = ""
    in_sitemap: bool = False


@dataclass
class ScoredCandidate:
    """Represents a scored seed candidate."""
    institution: str
    seed_url: str
    source: str
    score: int
    matched_terms: str
    reason: str


# ── SITEMAP PARSING ──────────────────────────────────────────────────────────

def parse_robots_sitemaps(robots_txt: str) -> List[str]:
    """
    Extract sitemap URLs from robots.txt.
    
    Args:
        robots_txt: Content of robots.txt
        
    Returns:
        List of sitemap URLs
    """
    if not robots_txt:
        return []
    return [m.group(1).strip() for m in _SITEMAP_DECL_RE.finditer(robots_txt)]


def parse_sitemap(xml_text: str) -> Tuple[List[str], List[str]]:
    """
    Parse sitemap XML and extract URLs and child sitemaps.
    
    Args:
        xml_text: Sitemap XML content
        
    Returns:
        Tuple of (urls, child_sitemaps)
    """
    urls: List[str] = []
    children: List[str] = []
    
    if not xml_text:
        return urls, children
    
    for loc in _LOC_RE.findall(xml_text):
        loc = loc.strip()
        if not loc.startswith("http"):
            continue
            
        low = loc.lower().split("?")[0]
        
        # Check if it's a sitemap (child) or regular URL
        if low.endswith(".xml") or low.endswith(".xml.gz") or "sitemap" in low.rsplit("/", 1)[-1]:
            children.append(loc)
        else:
            urls.append(loc)
            
        if len(urls) >= MAX_URLS_PER_SITEMAP:
            break
            
    return urls, children


# ── SCORING ──────────────────────────────────────────────────────────────────

def score_candidate(url: str, anchor: str = "", in_sitemap: bool = False) -> Tuple[int, List[str], str]:
    """
    Score a candidate URL based on academic relevance.
    
    Args:
        url: Candidate URL
        anchor: Anchor text from the link
        in_sitemap: Whether the URL came from a sitemap
        
    Returns:
        Tuple of (score, matched_terms, reason)
    """
    parsed = urlparse(url)
    url_text = _URL_SEPARATORS.sub(" ", unquote(parsed.path + " " + parsed.query))
    anchor = (anchor or "").strip()
    combined_text = url_text + " " + anchor

    # Match against term lists
    academic_url = match_terms(url_text, ACADEMIC_SEED_TERMS)
    academic_anchor = match_terms(anchor, ACADEMIC_SEED_TERMS) if anchor else []
    stem = match_terms(combined_text, STEM_TERMS)
    low = match_terms(combined_text, LOW_PRIORITY_TERMS)
    
    is_pdf = looks_like_pdf_url(url)
    curriculum = bool(WebCrawler.CURRICULUM_PRIORITY_PATTERNS.search(url))

    # Calculate score
    score = 0
    score += 3 * min(len(academic_url), 3)          # Academic terms in URL
    score += 2 * min(len(academic_anchor), 3)       # Academic terms in anchor
    score += 2 * min(len(stem), 3)                  # STEM terms
    score += 4 if (academic_url or academic_anchor) and stem else 0  # Academic + STEM
    score += 5 if curriculum else 0                 # Curriculum pattern
    score += 2 if is_pdf else 0                     # PDF
    score += 1 if in_sitemap else 0                 # From sitemap
    score -= 5 * len(low)                           # Penalize low-priority terms

    # Build reason string
    matched = sorted(set(academic_url) | set(academic_anchor) | set(stem))
    parts = []
    
    if academic_url:
        parts.append(f"url:{','.join(sorted(academic_url)[:3])}")
    if academic_anchor:
        parts.append(f"anchor:{','.join(sorted(academic_anchor)[:3])}")
    if stem:
        parts.append(f"stem:{','.join(sorted(stem)[:3])}")
    if curriculum:
        parts.append("curriculum-pattern")
    if is_pdf:
        parts.append("pdf")
    if in_sitemap:
        parts.append("sitemap")
    if low:
        parts.append(f"low-priority:{','.join(sorted(low)[:3])}")
        
    reason = " + ".join(parts) if parts else "no term matches"
    
    return score, matched, reason


# ── SEED DISCOVERER ──────────────────────────────────────────────────────────

class SeedDiscoverer:
    """
    Automatic seed URL discovery from university websites.
    
    Discovers seed URLs by:
    1. Scanning homepage links
    2. Parsing robots.txt for sitemaps
    3. Processing sitemap XML files
    """

    def __init__(self, cfg: dict):
        """Initialize the seed discoverer with configuration."""
        sc = (cfg or {}).get("scraper", {})
        
        self.timeout = sc.get("request_timeout_sec", 20)
        self.max_seeds = int(sc.get("max_auto_seeds_per_institution", DEFAULT_MAX_SEEDS))
        self.min_score = int(sc.get("min_seed_score", MIN_SEED_SCORE))
        
        self.limiter = RateLimiter(min_delay=sc.get("request_delay_sec", 1.0))
        self.robots = RobotsCache(
            ACADEMIC_UA,
            enabled=sc.get("respect_robots", True),
            logger=logger
        )
        
        self.session = requests.Session()
        self.session.headers["User-Agent"] = ACADEMIC_UA

    def discover(self, university: dict) -> List[dict]:
        """
        Discover seed URLs for a university.
        
        Args:
            university: University configuration dictionary
            
        Returns:
            List of discovered seed URLs with metadata
        """
        name = university.get("name", "?")
        base = (university.get("base_url") 
                or (university.get("catalog_urls") or [""])[0])
        
        base = normalize_url(base)
        if not base:
            logger.warning(f"Seed discovery: {name} has no base URL — skipping")
            return []

        parsed = urlparse(base)
        root = f"{parsed.scheme}://{parsed.netloc}"
        allowed = registered_domain(parsed.netloc)

        # Collect candidates
        candidates: Dict[str, Candidate] = {}
        
        def add_candidate(url: str, source: str, anchor: str = "") -> None:
            """Add a candidate URL to the collection."""
            url = normalize_url(url, base=root)
            if not url or not same_registered_domain(url, allowed):
                return
            if url.rstrip("/") == root.rstrip("/"):
                return
                
            if url in candidates:
                # Update existing candidate
                existing = candidates[url]
                if anchor and not existing.anchor:
                    existing.anchor = anchor
                existing.in_sitemap = existing.in_sitemap or source == "sitemap"
            else:
                if len(candidates) >= MAX_CANDIDATES:
                    return
                candidates[url] = Candidate(
                    url=url,
                    source=source,
                    anchor=anchor,
                    in_sitemap=source == "sitemap"
                )

        # 1. Crawl homepage
        html = self._fetch_text(base)
        if html:
            for href, anchor in self._extract_anchors(html):
                add_candidate(href, "homepage", anchor)

        # 2. Parse robots.txt for sitemaps
        robots_txt = self._fetch_text(root + "/robots.txt", check_robots=False)
        sitemap_urls = [root + "/sitemap.xml"]
        
        for sm in parse_robots_sitemaps(robots_txt or ""):
            if sm not in sitemap_urls:
                sitemap_urls.append(sm)

        # 3. Process sitemaps
        n_sitemap_urls = 0
        fetched = 0
        queue = list(sitemap_urls)
        seen_sitemaps: Set[str] = set(sitemap_urls)  # Fixed: track visited sitemaps
        
        while queue and fetched < MAX_SITEMAP_FETCHES:
            sm_url = queue.pop(0)
            body = self._fetch_text(sm_url, check_robots=False)
            fetched += 1
            
            if not body:
                continue
                
            urls, children = parse_sitemap(body)
            n_sitemap_urls += len(urls)
            
            for u in urls:
                add_candidate(u, "sitemap")
                
            # Queue child sitemaps
            for child in children[:MAX_CHILD_SITEMAPS]:
                if child in seen_sitemaps:
                    continue
                if same_registered_domain(child, allowed):
                    seen_sitemaps.add(child)
                    queue.append(child)

        # Score candidates
        scored: List[ScoredCandidate] = []
        for url, info in candidates.items():
            score, matched, reason = score_candidate(
                url, 
                anchor=info.anchor, 
                in_sitemap=info.in_sitemap
            )
            
            if score < self.min_score:
                continue
                
            scored.append(ScoredCandidate(
                institution=name,
                seed_url=url,
                source=info.source,
                score=score,
                matched_terms="|".join(matched),
                reason=reason
            ))

        # Sort and limit results
        scored.sort(key=lambda r: (-r.score, r.seed_url))
        kept = scored[:self.max_seeds]

        # Log results
        logger.info(
            f"Seed discovery: {name} | homepage_links={len(candidates)} "
            f"sitemap_urls={n_sitemap_urls} candidates={len(candidates)} "
            f"scored>={self.min_score}: {len(scored)} → kept {len(kept)}"
        )
        
        for s in kept[:5]:
            logger.info(f"    [{s.score:>3}] {s.seed_url}  ({s.reason})")

        # Convert to dictionary format for compatibility
        return [
            {
                "institution": s.institution,
                "seed_url": s.seed_url,
                "source": s.source,
                "score": s.score,
                "matched_terms": s.matched_terms,
                "reason": s.reason
            }
            for s in kept
        ]

    # ── FETCH METHODS ──────────────────────────────────────────────────────

    def _fetch_text(self, url: str, check_robots: bool = True) -> str:
        """
        Fetch and decode text content from a URL.
        
        Args:
            url: URL to fetch
            check_robots: Whether to check robots.txt
            
        Returns:
            Decoded text content or empty string on failure
        """
        if check_robots and not self.robots.allowed(url):
            logger.info(f"  robots.txt disallows: {url}")
            return ""

        self.limiter.wait()

        try:
            resp = self.session.get(
                url, 
                timeout=self.timeout,
                allow_redirects=True, 
                stream=True
            )
            
            if resp.status_code != 200:
                logger.debug(f"  {resp.status_code}: {url}")
                return ""

            # Read content with size limit
            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > MAX_FETCH_BYTES:
                    logger.debug(f"  Truncated oversized fetch: {url}")
                    break
            resp.close()

            # Handle gzip compression
            if content[:2] == _GZIP_MAGIC:
                try:
                    content = gzip.decompress(content)
                except Exception as e:
                    logger.debug(f"  Failed to decompress gzip: {url} - {e}")
                    return ""

            # Decode content
            return content.decode("utf-8", errors="replace")

        except requests.exceptions.RequestException as e:
            logger.debug(f"  fetch failed {url}: {type(e).__name__}")
            return ""

    # ── HTML PARSING ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_anchors(html: str) -> List[Tuple[str, str]]:
        """
        Extract all anchor links and their text from HTML.
        
        Args:
            html: HTML content
            
        Returns:
            List of (href, anchor_text) tuples
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
        except ImportError:
            logger.error("BeautifulSoup not installed: pip install beautifulsoup4")
            return []
        
        out: List[Tuple[str, str]] = []
        
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            
            # Skip non-HTTP links
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
                
            anchor_text = a.get_text(" ", strip=True)[:200]
            out.append((href, anchor_text))
            
        return out


# ── CONVENIENCE FUNCTIONS ──────────────────────────────────────────────────

def discover_seeds_for_university(university: dict, config: dict) -> List[dict]:
    """
    Convenience function to discover seeds for a single university.
    
    Args:
        university: University configuration
        config: Main configuration dictionary
        
    Returns:
        List of discovered seed URLs with metadata
    """
    discoverer = SeedDiscoverer(config)
    return discoverer.discover(university)


def discover_seeds_for_all(universities: List[dict], config: dict) -> Dict[str, List[dict]]:
    """
    Discover seeds for multiple universities.
    
    Args:
        universities: List of university configurations
        config: Main configuration dictionary
        
    Returns:
        Dictionary mapping university names to seed lists
    """
    discoverer = SeedDiscoverer(config)
    results = {}
    
    for university in universities:
        name = university.get("name", "?")
        seeds = discoverer.discover(university)
        if seeds:
            results[name] = seeds
            
    return results