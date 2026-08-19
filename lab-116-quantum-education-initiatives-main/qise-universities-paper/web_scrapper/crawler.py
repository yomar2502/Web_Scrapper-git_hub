"""
crawler.py — Crawlers especializados por tipo de fuente.
"""

import heapq
import itertools
import re
import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Generator, Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field

import requests

from keywords import (
    ACADEMIC_SEED_TERMS, STEM_TERMS, LOW_PRIORITY_TERMS, match_terms,
)
from utils import (
    RateLimiter, RobotsCache, get_logger, slugify,
    normalize_url, same_registered_domain, registered_domain, url_hash,
    looks_like_pdf_url, external_doc_download_url,
)

logger = get_logger("crawler")

ACADEMIC_UA = (
    "QISE-LatAm-Research-Bot/2.0 (academic research on quantum education; "
    "+contact via project README)"
)


# ── SESSION CONFIGURATION ──────────────────────────────────────────────────

def _make_session(user_agent: str, timeout: int, max_retries: int) -> requests.Session:
    """Create a configured requests session with retry logic."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or ACADEMIC_UA,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "es,pt,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    
    retry = Retry(
        total=max_retries,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session


# ── WEB CRAWLER ────────────────────────────────────────────────────────────

@dataclass
class CrawlStats:
    """Statistics for a crawl session."""
    pages_crawled: int = 0
    html_pages: int = 0
    pdfs_detected: int = 0


@dataclass
class FetchResult:
    """Result of fetching a URL."""
    content: bytes
    text: str
    content_type: str
    final_url: str
    is_pdf: bool
    is_html: bool
    doc_kind: str = ""


class WebCrawler:
    """Main web crawler for academic course pages."""

    # Course URL patterns for identifying relevant pages
    COURSE_URL_PATTERNS = re.compile(
        r"(curso|course|syllabus|silabo|s[íi]labo"
        r"|asignatura|disciplina|programa|materia|pensum|ementa"
        r"|catalogo|catalog|oferta.?academ|curricul|malla|grade.?curricular"
        r"|matriz.?curricular|plano.?de.?ensino"
        r"|posgrado|postgrado|graduate|undergraduate|graduacao"
        r"|licenciatura|maestr[íi]a|maestria|mestrado|doutorado|doctorado"
        r"|especializacion|especializaci[oó]n|carrera"
        r"|fisica|f[íi]sica|ingenieria|ingenier[íi]a|ciencias|computac"
        r"|pregrado|plan.?de.?estudio)",
        re.IGNORECASE,
    )

    # Patterns for anchor text that indicates course content
    COURSE_ANCHOR_PATTERNS = re.compile(
        r"(malla|plan de estudio|pensum|s[íi]labo|syllabus|programa|curr[íi]cul"
        r"|grade curricular|matriz curricular|ementa|asignatura|disciplina|curso)",
        re.IGNORECASE,
    )

    # High-priority curriculum patterns
    CURRICULUM_PRIORITY_PATTERNS = re.compile(
        r"(plan.?de.?estudio|malla|pensum|curricul|grade.?curricular"
        r"|matriz.?curricular|plano.?de.?ensino|silabo|s[íi]labo|syllabus"
        r"|(?<![a-z])ementa|oferta.?academ|catalogo|catalog)",
        re.IGNORECASE,
    )

    # Junk to NEVER queue: binary assets, auth/admin, per-user pages
    HARD_SKIP_PATTERNS = re.compile(
        r"\.(jpg|jpeg|png|gif|svg|ico|mp4|mp3|zip|rar|exe|js|css"
        r"|woff|woff2|ttf|eot)(\?.*)?$"
        r"|/(login|logout|wp-admin|wp-login|search|tag|autor|author"
        r"|comment|registro|register|password|reset|shop"
        r"|contact|contacto|sitemap|privacidad|privacy|terminos|terms"
        r"|rss|atom|newsletter|suscri)"
        r"|/admin(?![a-z])"  # Fixed: only matches "admin" not "administracion"
        r"|/cart(?![a-z])",  # Fixed: only matches "cart" not "cartelera"
        re.IGNORECASE,
    )

    # Low-priority URL patterns (crawled but de-prioritized)
    LOW_PRIORITY_URL_PATTERNS = re.compile(
        r"/(noticias?|news|blog|boletin|eventos?|events?|agenda|calendario"
        r"|prensa|press|comunicado|galeria|gallery"
        r"|admision(es)?|admissions?|vestibular"
        r"|alumni|egresados|exalumnos|deportes?|sports"
        r"|profesor|docentes?|staff|equipo|team|investigador|researcher"
        r"|transparencia|licitacion|administrativ)",
        re.IGNORECASE,
    )

    def __init__(self, cfg: dict):
        """Initialize the web crawler with configuration."""
        sc = cfg["scraper"]
        
        # Crawl settings
        self.delay = sc["request_delay_sec"]
        self.timeout = sc["request_timeout_sec"]
        self.max_retries = sc["max_retries"]
        self.global_max_depth = sc["max_depth"]
        self.global_max_pages = sc["max_pages_per_university"]
        self.download_pdfs = sc.get("download_pdfs", True)
        self.use_cache = sc.get("use_cache", True)
        self.max_pdf_bytes = int(sc.get("max_pdf_mb", 40)) * 1024 * 1024
        self.max_pdfs_per_domain = int(sc.get("max_pdfs_per_domain", 50))
        self.fetch_external_docs = sc.get("fetch_external_docs", True)
        self.user_agent = sc.get("user_agent") or ACADEMIC_UA
        
        # State
        self.stats: Dict[str, CrawlStats] = {}
        self._seen_hosts: set[str] = set()
        self.raw_dir = Path(cfg["output"]["raw_dir"])
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        
        # Network components
        self.limiter = RateLimiter(min_delay=self.delay)
        self.session = _make_session(self.user_agent, self.timeout, self.max_retries)
        self.robots = RobotsCache(
            self.user_agent, 
            enabled=sc.get("respect_robots", True), 
            logger=logger
        )

    def crawl_university(self, university: dict) -> Generator[dict, None, None]:
        """
        Crawl a university's academic content.
        
        Args:
            university: University configuration dictionary
            
        Yields:
            Dictionary with fetched content and metadata
        """
        base = university.get("base_url") or (university.get("catalog_urls") or [""])[0]
        allowed_domain = registered_domain(urlparse(base).netloc)
        seeds = [normalize_url(u) for u in university.get("catalog_urls", []) if u]

        # Apply limits
        max_pages = university.get("max_pages") or self.global_max_pages
        max_depth = university.get("max_depth") or self.global_max_depth
        max_pdfs = university.get("max_pdfs") or self.max_pdfs_per_domain

        # Initialize stats
        uni_name = university.get("name", "?")
        self.stats[uni_name] = CrawlStats()
        stats = self.stats[uni_name]

        # Queue setup
        visited: set[str] = set()
        seq = itertools.count()
        queue: list[tuple[int, int, str, int, str]] = []
        
        for u in seeds:
            priority = 1 if looks_like_pdf_url(u) else max(1, self._link_priority(u, urlparse(u).path.lower(), "") - 1)
            queue.append((priority, next(seq), u, 0, ""))
        heapq.heapify(queue)
        
        self._seen_hosts = {urlparse(u).netloc for u in seeds}
        pdf_cap_warned = False

        logger.info(
            f"Crawl: {uni_name} | seeds={len(seeds)} "
            f"max_pages={max_pages} max_depth={max_depth} max_pdfs={max_pdfs} "
            f"pdfs={'on' if self.download_pdfs else 'off'} "
            f"seed_origin={university.get('seed_origin', 'manual')}"
        )

        while queue and stats.pages_crawled < max_pages:
            _prio, _, url, depth, found_on = heapq.heappop(queue)
            url = normalize_url(url)
            
            if not url or url in visited:
                continue
            visited.add(url)

            if not self.robots.allowed(url):
                logger.info(f"  robots.txt disallows, skipping: {url}")
                continue

            result = self._fetch(url, university)
            if result is None:
                continue
                
            stats.pages_crawled += 1

            # Handle PDF documents
            if result.is_pdf or result.doc_kind:
                stats.pdfs_detected += 1
                if stats.pdfs_detected > max_pdfs:
                    if not pdf_cap_warned:
                        logger.warning(f"  max_pdfs_per_domain ({max_pdfs}) reached")
                        pdf_cap_warned = True
                    continue
                    
                doc_type = "pdf" if result.is_pdf else result.doc_kind
                logger.debug(f"  {doc_type.upper()}: {url} (found on: {found_on or 'seed'})")
                yield {
                    "type": doc_type,
                    "content": result.content,
                    "url": result.final_url,
                    "found_on": found_on,
                    "university": university
                }
                continue

            # Skip non-HTML content
            if not result.is_html:
                logger.debug(f"  Skipping non-HTML/PDF ({result.content_type}): {url}")
                continue

            # Process HTML page
            stats.html_pages += 1
            logger.debug(f"  HTML ({len(result.text):,}b) depth={depth}: {url}")
            yield {
                "type": "html",
                "content": result.text,
                "url": result.final_url,
                "found_on": found_on,
                "university": university
            }

            # Extract links
            scored_links, pdf_links = self._extract_links(result.text, url, allowed_domain)

            # Queue PDFs
            if self.download_pdfs and stats.pdfs_detected < max_pdfs:
                for link in pdf_links:
                    if link not in visited:
                        heapq.heappush(queue, (1, next(seq), link, depth + 1, url))

            # Queue HTML links
            if depth < max_depth:
                page_host = urlparse(url).netloc
                for priority, link in scored_links:
                    if link not in visited:
                        link_depth = 0 if urlparse(link).netloc != page_host else depth + 1
                        heapq.heappush(queue, (priority, next(seq), link, link_depth, url))

        logger.info(
            f"  Done: {uni_name} | "
            f"{stats.pages_crawled} pages fetched "
            f"({stats.html_pages} HTML, {stats.pdfs_detected} PDFs)"
        )

    # ── FETCH METHODS ──────────────────────────────────────────────────────

    def _fetch(self, url: str, university: dict) -> Optional[FetchResult]:
        """Fetch a URL with caching and retry logic."""
        cached = self._load_cache(url, university)
        if cached is not None:
            return cached

        self.limiter.wait()
        
        try:
            resp = self.session.get(
                url, 
                timeout=(6, self.timeout),
                allow_redirects=True, 
                stream=True
            )
            resp.raise_for_status()
            
            content_type = resp.headers.get("Content-Type", "").lower()

            # Read content with size limit
            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > self.max_pdf_bytes:
                    logger.warning(f"  Truncated oversized download: {url}")
                    break
                    
            final_url = normalize_url(resp.url) or url
            resp.close()
            
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            logger.warning(f"  HTTP {code}: {url}")
            return None
            
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Retry with HTTPS if HTTP failed
            if url.startswith("http://"):
                https_url = "https://" + url[len("http://"):]
                logger.info(f"  Retrying with HTTPS: {https_url}")
                return self._fetch(https_url, university)
            logger.warning(f"  {type(e).__name__}: {url}")
            return None
            
        except Exception as e:
            logger.warning(f"  Error: {url} → {e}")
            return None

        # Determine content type
        is_pdf = self._is_pdf(url, content_type, content)
        doc_kind = "" if is_pdf else self._is_spreadsheet(url, content_type, content)
        is_html = (not is_pdf and not doc_kind and 
                  ("html" in content_type or content_type == "" or "xml" in content_type))
        
        result = self._build_result(content, content_type, final_url, is_pdf, is_html, doc_kind)
        self._save_cache(url, university, result)
        return result

    @staticmethod
    def _is_pdf(url: str, content_type: str, content: bytes) -> bool:
        """Check if content is a PDF."""
        if "pdf" in content_type:
            return True
        if looks_like_pdf_url(url):
            return True
        return content[:1024].lstrip()[:5] == b"%PDF-"

    @staticmethod
    def _is_spreadsheet(url: str, content_type: str, content: bytes) -> str:
        """Check if content is a spreadsheet and return its type."""
        path = urlparse(url.lower()).path
        
        if "spreadsheetml" in content_type or path.endswith(".xlsx"):
            return "xlsx"
        if "vnd.ms-excel" in content_type or path.endswith(".xls"):
            return "xls"
            
        # Check ZIP-based spreadsheets
        if content[:4] == b"PK\x03\x04":
            import io as _io
            import zipfile
            try:
                with zipfile.ZipFile(_io.BytesIO(content)) as z:
                    if any(n.startswith("xl/") for n in z.namelist()[:80]):
                        return "xlsx"
            except Exception:
                pass
                
        # Check OLE-based spreadsheets
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "xls"
            
        return ""

    @staticmethod
    def _build_result(content: bytes, content_type: str, final_url: str,
                      is_pdf: bool, is_html: bool, doc_kind: str = "") -> FetchResult:
        """Build a FetchResult from response data."""
        text = ""
        if is_html:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")
                
        return FetchResult(
            content=content,
            text=text,
            content_type=content_type,
            final_url=final_url,
            is_pdf=is_pdf,
            is_html=is_html,
            doc_kind=doc_kind
        )

    # ── CACHE METHODS ──────────────────────────────────────────────────────

    def _cache_paths(self, url: str, university: dict) -> tuple[Path, Path]:
        """Get cache file paths for a URL."""
        slug = slugify(university.get("name") or "misc")
        out_dir = self.raw_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        h = url_hash(url)
        return out_dir / f"{h}.meta.json", out_dir / h

    def _load_cache(self, url: str, university: dict) -> Optional[FetchResult]:
        """Load cached content if available."""
        if not self.use_cache:
            return None
            
        meta_path, _ = self._cache_paths(url, university)
        if not meta_path.exists():
            return None
            
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body_path = Path(meta["body_path"])
            content = body_path.read_bytes()
            
            return self._build_result(
                content,
                meta.get("content_type", ""),
                meta.get("final_url", url),
                meta.get("is_pdf", False),
                meta.get("is_html", False),
                meta.get("doc_kind", "")
            )
        except Exception:
            return None

    def _save_cache(self, url: str, university: dict, result: FetchResult) -> None:
        """Save content to cache."""
        if not self.use_cache:
            return
            
        meta_path, body_base = self._cache_paths(url, university)
        
        ext = ("pdf" if result.is_pdf 
               else result.doc_kind 
               or ("html" if result.is_html else "bin"))
        body_path = body_base.with_suffix("." + ext)
        
        try:
            body_path.write_bytes(result.content)
            meta_path.write_text(
                json.dumps({
                    "url": url,
                    "final_url": result.final_url,
                    "content_type": result.content_type,
                    "is_pdf": result.is_pdf,
                    "is_html": result.is_html,
                    "doc_kind": result.doc_kind,
                    "body_path": str(body_path),
                }, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"Cache write failed for {url}: {e}")

    # ── LINK EXTRACTION ────────────────────────────────────────────────────

    def _extract_links(self, html: str, base_url: str, allowed_domain: str) -> Tuple[List[Tuple[int, str]], List[str]]:
        """
        Extract and score links from HTML.
        
        Returns:
            Tuple of (scored_links, pdf_links)
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("BeautifulSoup not installed: pip install beautifulsoup4")
            return [], []

        soup = BeautifulSoup(html, "html.parser")
        scored_links: List[Tuple[int, str]] = []
        pdf_links: List[str] = []
        seen: set[str] = set()

        for el in soup.find_all(["a", "iframe", "embed", "object"]):
            raw = (el.get("href") or el.get("src") or el.get("data") or "").strip()
            
            # Skip invalid URLs
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "about:", "data:")):
                continue

            # Handle external documents (Google Drive, SharePoint, etc.)
            if self.fetch_external_docs and "://" in raw:
                ext_url = external_doc_download_url(raw)
                if ext_url and ext_url not in seen:
                    seen.add(ext_url)
                    pdf_links.append(ext_url)
                continue

            # Normalize and filter URL
            full_url = normalize_url(raw, base=base_url)
            if not full_url or full_url in seen:
                continue
            if not same_registered_domain(full_url, allowed_domain):
                continue
            seen.add(full_url)

            anchor_text = el.get_text(" ", strip=True) if el.name == "a" else ""
            path_low = urlparse(full_url).path.lower()

            # Check for downloadable documents
            is_download = any(w in path_low for w in ("download", "descargar", "documento", "archivo", "adjunto"))
            is_doc = (looks_like_pdf_url(full_url) 
                     or path_low.endswith((".xlsx", ".xls"))
                     or (is_download and self.COURSE_ANCHOR_PATTERNS.search(anchor_text)))
                     
            if is_doc:
                pdf_links.append(full_url)
                continue

            # Skip non-anchor elements for HTML crawling
            if el.name != "a":
                continue

            # Skip junk URLs
            if self.HARD_SKIP_PATTERNS.search(path_low):
                continue

            # Calculate priority
            priority = self._link_priority(full_url, path_low, anchor_text)

            # Adjust priority for new domains
            host = urlparse(full_url).netloc
            if host not in self._seen_hosts:
                self._seen_hosts.add(host)
                host_and_anchor = re.sub(r"[-.]", " ", host) + " " + anchor_text
                first_label = host.split(".")[0]
                
                if (match_terms(host_and_anchor, ACADEMIC_SEED_TERMS) 
                    or match_terms(host_and_anchor, STEM_TERMS)
                    or re.fullmatch(r"[a-z]{2,4}", first_label)):
                    priority = min(priority, 2)
                else:
                    priority = min(priority, 3)

            scored_links.append((priority, full_url))

        return scored_links, pdf_links

    def _link_priority(self, url: str, path_low: str, anchor_text: str) -> int:
        """
        Calculate priority for a link (lower = higher priority).
        
        Priority levels:
            1-2: Very high (curriculum, syllabus)
            3-4: High (course-related)
            5: Low (news, events)
            6: Default
        """
        path_text = re.sub(r"[-_/.+%]+", " ", path_low)
        combined = path_text + " " + anchor_text
        
        # Highest priority: curriculum materials
        if (self.CURRICULUM_PRIORITY_PATTERNS.search(url) 
            or self.CURRICULUM_PRIORITY_PATTERNS.search(anchor_text)):
            return 2 if match_terms(combined, STEM_TERMS) else 3
        
        # Low priority: news, events, etc.
        if (self.LOW_PRIORITY_URL_PATTERNS.search(path_low)
            or match_terms(anchor_text, LOW_PRIORITY_TERMS)):
            return 5
        
        # Medium priority: course-related
        if (self.COURSE_URL_PATTERNS.search(url)
            or self.COURSE_ANCHOR_PATTERNS.search(anchor_text)
            or match_terms(combined, STEM_TERMS)):
            return 3 if match_terms(combined, STEM_TERMS) else 4
        
        return 6


# ── RSS CRAWLER ────────────────────────────────────────────────────────────

class RSSCrawler:
    """Simple RSS feed crawler."""
    
    def __init__(self, cfg: dict):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=1.5)
        self.session = _make_session("", self.timeout, 2)

    def crawl_feed(self, source: dict) -> Generator[dict, None, None]:
        """Crawl an RSS feed."""
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser not installed: pip install feedparser")
            return

        self.limiter.wait()
        logger.info(f"RSS: {source['name']}")
        
        try:
            resp = self.session.get(source["url"], timeout=self.timeout)
            feed = feedparser.parse(resp.text)
        except Exception as e:
            logger.warning(f"RSS error {source['name']}: {e}")
            return

        for entry in feed.entries:
            yield {
                "type": "rss",
                "content": entry,
                "url": getattr(entry, "link", ""),
                "university": None,
                "rss_source_name": source["name"]
            }
            
        logger.info(f"  RSS {source['name']}: {len(feed.entries)} entries")


# ── TWITTER CRAWLER ────────────────────────────────────────────────────────

class TwitterCrawler:
    """Twitter API crawler for quantum education mentions."""
    
    def __init__(self, cfg: dict, bearer_token: Optional[str] = None):
        self.bearer_token = bearer_token
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=3.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_queries(self, source: dict) -> Generator[dict, None, None]:
        """Crawl Twitter for given queries."""
        if not self.bearer_token:
            logger.warning("Twitter crawler: No bearer token provided")
            return
            
        for query in source.get("queries", []):
            yield from self._api_search(query)

    def _api_search(self, query: str) -> Generator[dict, None, None]:
        """Execute Twitter API search."""
        self.limiter.wait()
        
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        params = {
            "query": f"{query} -is:retweet lang:es OR lang:pt OR lang:en",
            "max_results": 100,
            "tweet.fields": "created_at,author_id",
        }
        
        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            
            for tweet in resp.json().get("data", []):
                yield {
                    "type": "tweet",
                    "content": tweet,
                    "url": f"https://twitter.com/i/web/status/{tweet['id']}",
                    "university": None,
                    "search_query": query
                }
        except Exception as e:
            logger.warning(f"Twitter API '{query}': {e}")


# ── REDDIT CRAWLER ─────────────────────────────────────────────────────────

class RedditCrawler:
    """Reddit crawler for quantum education mentions."""
    
    def __init__(self, cfg: dict, praw_cfg: Optional[dict] = None):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=2.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_source(self, source: dict) -> Generator[dict, None, None]:
        """Crawl Reddit for given subreddits and queries."""
        for subreddit in source.get("subreddits", []):
            for query in source.get("queries", []):
                yield from self._search(subreddit, query)

    def _search(self, subreddit: str, query: str) -> Generator[dict, None, None]:
        """Execute Reddit search."""
        self.limiter.wait()
        
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {
            "q": query,
            "sort": "relevance",
            "limit": 25,
            "restrict_sr": 1
        }
        
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            
            for post in resp.json().get("data", {}).get("children", []):
                data = post.get("data", {})
                yield {
                    "type": "reddit_post",
                    "content": data,
                    "url": data.get("url", ""),
                    "university": None,
                    "subreddit": subreddit
                }
        except Exception as e:
            logger.warning(f"Reddit r/{subreddit} '{query}': {e}")