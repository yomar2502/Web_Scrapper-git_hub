import gzip
import re
from urllib.parse import urlparse, unquote

import requests

from crawler import ACADEMIC_UA, WebCrawler
from keywords import (
    ACADEMIC_SEED_TERMS, STEM_TERMS, LOW_PRIORITY_TERMS, match_terms,
)
from utils import (
    RateLimiter, RobotsCache, get_logger,
    normalize_url, same_registered_domain, registered_domain, looks_like_pdf_url,
)

logger = get_logger("seed_discovery")

MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_SITEMAP_FETCHES = 6
MAX_CHILD_SITEMAPS = 5
MAX_URLS_PER_SITEMAP = 3000
MAX_CANDIDATES = 8000

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_DECL_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_URL_SEPARATORS = re.compile(r"[-_/.+%?=&#]+")


def parse_robots_sitemaps(robots_txt: str) -> list[str]:
    return [m.group(1).strip() for m in _SITEMAP_DECL_RE.finditer(robots_txt or "")]


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    children: list[str] = []
    for loc in _LOC_RE.findall(xml_text or ""):
        loc = loc.strip()
        if not loc.startswith("http"):
            continue
        low = loc.lower().split("?")[0]
        if low.endswith(".xml") or low.endswith(".xml.gz") or "sitemap" in low.rsplit("/", 1)[-1]:
            children.append(loc)
        else:
            urls.append(loc)
        if len(urls) >= MAX_URLS_PER_SITEMAP:
            break
    return urls, children


def score_candidate(url: str, anchor: str = "",
                    in_sitemap: bool = False) -> tuple[int, list[str], str]:
    parsed = urlparse(url)
    url_text = _URL_SEPARATORS.sub(" ", unquote(parsed.path + " " + parsed.query))
    anchor = (anchor or "").strip()

    academic_url = match_terms(url_text, ACADEMIC_SEED_TERMS)
    academic_anchor = match_terms(anchor, ACADEMIC_SEED_TERMS) if anchor else []
    stem = match_terms(url_text + " " + anchor, STEM_TERMS)
    low = match_terms(url_text + " " + anchor, LOW_PRIORITY_TERMS)
    is_pdf = looks_like_pdf_url(url)
    curriculum = bool(WebCrawler.CURRICULUM_PRIORITY_PATTERNS.search(url))

    score = 0
    score += 3 * min(len(academic_url), 3)
    score += 2 * min(len(academic_anchor), 3)
    score += 2 * min(len(stem), 3)
    if (academic_url or academic_anchor) and stem:
        score += 4
    if curriculum:
        score += 5
    if is_pdf:
        score += 2
    if in_sitemap:
        score += 1
    score -= 5 * len(low)

    matched = sorted(set(academic_url) | set(academic_anchor) | set(stem))
    parts = []
    if academic_url:
        parts.append("url:" + ",".join(sorted(academic_url)[:3]))
    if academic_anchor:
        parts.append("anchor:" + ",".join(sorted(academic_anchor)[:3]))
    if stem:
        parts.append("stem:" + ",".join(sorted(stem)[:3]))
    if curriculum:
        parts.append("curriculum-pattern")
    if is_pdf:
        parts.append("pdf")
    if in_sitemap:
        parts.append("sitemap")
    if low:
        parts.append("low-priority:" + ",".join(sorted(low)[:3]))
    return score, matched, " + ".join(parts) or "no term matches"


class SeedDiscoverer:

    def __init__(self, cfg: dict):
        sc = (cfg or {}).get("scraper", {})
        self.timeout = sc.get("request_timeout_sec", 20)
        self.max_seeds = int(sc.get("max_auto_seeds_per_institution", 20))
        self.min_score = int(sc.get("min_seed_score", 3))
        self.limiter = RateLimiter(min_delay=sc.get("request_delay_sec", 1.0))
        self.robots = RobotsCache(ACADEMIC_UA,
                                  enabled=sc.get("respect_robots", True),
                                  logger=logger)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = ACADEMIC_UA

    def discover(self, university: dict) -> list[dict]:
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

        cands: dict[str, dict] = {}

        def add(url, source, anchor=""):
            url = normalize_url(url, base=root)
            if not url or not same_registered_domain(url, allowed):
                return
            if url.rstrip("/") == root.rstrip("/"):
                return
            cur = cands.get(url)
            if cur is None:
                if len(cands) >= MAX_CANDIDATES:
                    return
                cands[url] = {"source": source, "anchor": anchor,
                              "in_sitemap": source == "sitemap"}
            else:
                if anchor and not cur["anchor"]:
                    cur["anchor"] = anchor
                cur["in_sitemap"] = cur["in_sitemap"] or source == "sitemap"

        n_home = 0
        html = self._fetch_text(base)
        if html:
            for href, anchor in self._extract_anchors(html):
                add(href, "homepage", anchor)
            n_home = len(cands)

        sitemap_urls = [root + "/sitemap.xml"]
        robots_txt = self._fetch_text(root + "/robots.txt", check_robots=False)
        for sm in parse_robots_sitemaps(robots_txt or ""):
            if sm not in sitemap_urls:
                sitemap_urls.append(sm)

        n_sitemap_urls = 0
        fetched = 0
        queue = list(sitemap_urls)
        # CAMBIO (bug): no había ningún control de sitemaps YA vistos en esta
        # cola de recursión. 
        seen_sitemaps: set[str] = set(sitemap_urls)
        while queue and fetched < MAX_SITEMAP_FETCHES:
            sm_url = queue.pop(0)
            body = self._fetch_text(sm_url, check_robots=False)
            fetched += 1
            if not body:
                continue
            urls, children = parse_sitemap(body)
            n_sitemap_urls += len(urls)
            for u in urls:
                add(u, "sitemap")
            for child in children[:MAX_CHILD_SITEMAPS]:
                if child in seen_sitemaps:
                    continue
                if same_registered_domain(child, allowed):
                    seen_sitemaps.add(child)
                    queue.append(child)

        scored: list[dict] = []
        for url, info in cands.items():
            score, matched, reason = score_candidate(
                url, anchor=info["anchor"], in_sitemap=info["in_sitemap"])
            if score < self.min_score:
                continue
            scored.append({
                "institution": name,
                "seed_url": url,
                "source": info["source"],
                "score": score,
                "matched_terms": "|".join(matched),
                "reason": reason,
            })
        scored.sort(key=lambda r: (-r["score"], r["seed_url"]))
        kept = scored[:self.max_seeds]

        logger.info(
            f"Seed discovery: {name} | homepage_links={n_home} "
            f"sitemap_urls={n_sitemap_urls} candidates={len(cands)} "
            f"scored>={self.min_score}: {len(scored)} → kept {len(kept)}"
        )
        for s in kept[:5]:
            logger.info(f"    [{s['score']:>3}] {s['seed_url']}  ({s['reason']})")
        return kept

    def _fetch_text(self, url: str, check_robots: bool = True) -> str:
        if check_robots and not self.robots.allowed(url):
            logger.info(f"  robots.txt disallows: {url}")
            return ""
        self.limiter.wait()
        try:
            resp = self.session.get(url, timeout=self.timeout,
                                    allow_redirects=True, stream=True)
            if resp.status_code != 200:
                logger.debug(f"  {resp.status_code}: {url}")
                return ""
            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > MAX_FETCH_BYTES:
                    break
            resp.close()
            if content[:2] == b"\x1f\x8b":
                try:
                    content = gzip.decompress(content)
                except Exception:
                    return ""
            return content.decode("utf-8", errors="replace")
        except requests.exceptions.RequestException as e:
            logger.debug(f"  fetch failed {url}: {type(e).__name__}")
            return ""

    @staticmethod
    def _extract_anchors(html: str) -> list[tuple[str, str]]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []
        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            out.append((href, a.get_text(" ", strip=True)[:200]))
        return out