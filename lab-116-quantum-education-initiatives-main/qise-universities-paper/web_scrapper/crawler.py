"""
crawler.py — Crawlers especializados por tipo de fuente.
"""

import heapq
import itertools
import re
import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Generator

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


def _make_session(user_agent: str, timeout: int, max_retries: int) -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or ACADEMIC_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/pdf;q=0.9,*/*;q=0.8",
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


class WebCrawler:

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

    COURSE_ANCHOR_PATTERNS = re.compile(
        r"(malla|plan de estudio|pensum|s[íi]labo|syllabus|programa|curr[íi]cul"
        r"|grade curricular|matriz curricular|ementa|asignatura|disciplina|curso)",
        re.IGNORECASE,
    )

    CURRICULUM_PRIORITY_PATTERNS = re.compile(
        r"(plan.?de.?estudio|malla|pensum|curricul|grade.?curricular"
        r"|matriz.?curricular|plano.?de.?ensino|silabo|s[íi]labo|syllabus"
        r"|(?<![a-z])ementa|oferta.?academ|catalogo|catalog)",
        re.IGNORECASE,
    )

    # Junk to NEVER queue: binary assets, auth/admin, per-user pages. Anything
    # merely off-topic (news, events, admissions…) is queued at LOW priority
    # instead — it may still hold contextual quantum evidence.
    #
    # CORREGIDO: "admin" y "cart" vivían dentro de la alternancia general sin
    # ningún límite de fin de palabra/segmento — como el patrón solo exige un
    # "/" ANTES de la palabra (no un límite DESPUÉS), "admin" y "cart" hacían
    # match como simple PREFIJO de palabras reales completamente distintas:
    # "/administracion" (carrera de Administración de Empresas, un programa
    # académico real) y "/cartelera-de-eventos" (tablero de eventos) quedaban
    # descartadas para siempre 
    HARD_SKIP_PATTERNS = re.compile(
        r"\.(jpg|jpeg|png|gif|svg|ico|mp4|mp3|zip|rar|exe|js|css"
        r"|woff|woff2|ttf|eot)(\?.*)?$"
        r"|/(login|logout|wp-admin|wp-login|search|tag|autor|author"
        r"|comment|registro|register|password|reset|shop"
        r"|contact|contacto|sitemap|privacidad|privacy|terminos|terms"
        r"|rss|atom|newsletter|suscri)"
        r"|/admin(?![a-z])"
        r"|/cart(?![a-z])",
        re.IGNORECASE,
    )

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
        sc = cfg["scraper"]
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
        self.stats: dict[str, dict] = {}
        self._seen_hosts: set[str] = set()

        self.raw_dir = Path(cfg["output"]["raw_dir"])
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(min_delay=self.delay)
        self.session = _make_session(self.user_agent, self.timeout, self.max_retries)
        self.robots = RobotsCache(
            self.user_agent, enabled=sc.get("respect_robots", True), logger=logger
        )

    def crawl_university(self, university: dict) -> Generator[dict, None, None]:
        base = university.get("base_url") or (university.get("catalog_urls") or [""])[0]
        allowed_domain = registered_domain(urlparse(base).netloc)
        seeds = [normalize_url(u) for u in university.get("catalog_urls", []) if u]

        max_pages = university.get("max_pages")
        max_pages = self.global_max_pages if max_pages is None else max_pages
        max_depth = university.get("max_depth")
        max_depth = self.global_max_depth if max_depth is None else max_depth
        max_pdfs = university.get("max_pdfs")
        max_pdfs = self.max_pdfs_per_domain if max_pdfs is None else max_pdfs

        st = {"pages_crawled": 0, "html_pages": 0, "pdfs_detected": 0}
        self.stats[university.get("name", "?")] = st

        visited: set[str] = set()
        seq = itertools.count()
        queue: list[tuple[int, int, str, int, str]] = []
        for u in seeds:
            if looks_like_pdf_url(u):
                prio = 1
            else:
                prio = max(1, self._link_priority(
                    u, urlparse(u).path.lower(), "") - 1)
            queue.append((prio, next(seq), u, 0, ""))
        heapq.heapify(queue)
        pdf_cap_warned = False
        self._seen_hosts = {urlparse(u).netloc for u in seeds}

        logger.info(
            f"Crawl: {university.get('name','?')} | seeds={len(seeds)} "
            f"max_pages={max_pages} max_depth={max_depth} max_pdfs={max_pdfs} "
            f"pdfs={'on' if self.download_pdfs else 'off'} "
            f"seed_origin={university.get('seed_origin', 'manual')}"
        )

        while queue and st["pages_crawled"] < max_pages:
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
            st["pages_crawled"] += 1

            doc_kind = "pdf" if result["is_pdf"] else result.get("doc_kind", "")
            if doc_kind:
                st["pdfs_detected"] += 1
                if st["pdfs_detected"] > max_pdfs:
                    if not pdf_cap_warned:
                        logger.warning(f"  max_pdfs_per_domain ({max_pdfs}) "
                                       f"reached — further documents skipped")
                        pdf_cap_warned = True
                    continue
                logger.debug(f"  {doc_kind.upper()}: {url} "
                             f"(found on: {found_on or 'seed'})")
                yield {"type": doc_kind, "content": result["content"],
                       "url": result["final_url"], "found_on": found_on,
                       "university": university}
                continue

            if not result["is_html"]:
                logger.debug(f"  Skipping non-HTML/PDF ({result['content_type']}): {url}")
                continue

            html = result["text"]
            st["html_pages"] += 1
            logger.debug(f"  HTML ({len(html):,}b) depth={depth}: {url}")
            yield {"type": "html", "content": html,
                   "url": result["final_url"], "found_on": found_on,
                   "university": university}

            scored_links, pdf_links = self._extract_links(html, url, allowed_domain)

            if self.download_pdfs and st["pdfs_detected"] < max_pdfs:
                for link in pdf_links:
                    if link not in visited:
                        heapq.heappush(queue, (1, next(seq), link, depth + 1, url))

            if depth < max_depth:
                page_host = urlparse(url).netloc
                for prio, link in scored_links:
                    if link not in visited:
                        link_depth = (0 if urlparse(link).netloc != page_host
                                      else depth + 1)
                        heapq.heappush(queue,
                                       (prio, next(seq), link, link_depth, url))

        logger.info(
            f"  Done: {university.get('name','?')} | "
            f"{st['pages_crawled']} pages fetched "
            f"({st['html_pages']} HTML, {st['pdfs_detected']} PDFs)"
        )

    def _fetch(self, url: str, university: dict) -> dict | None:
        cached = self._load_cache(url, university)
        if cached is not None:
            return cached

        self.limiter.wait()
        try:
            resp = self.session.get(url, timeout=(6, self.timeout),
                                    allow_redirects=True, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()

            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > self.max_pdf_bytes:
                    logger.warning(f"  Truncated oversized download (> "
                                   f"{self.max_pdf_bytes // (1024*1024)}MB): {url}")
                    break
            final_url = normalize_url(resp.url) or url
            resp.close()
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            logger.warning(f"  HTTP {code}: {url}")
            return None
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if url.startswith("http://"):
                https_url = "https://" + url[len("http://"):]
                logger.info(f"  {type(e).__name__} on http, retrying https: {https_url}")
                return self._fetch(https_url, university)
            logger.warning(f"  {type(e).__name__}: {url}")
            return None
        except Exception as e:
            logger.warning(f"  Error: {url} → {e}")
            return None

        is_pdf = self._is_pdf(url, content_type, content)
        doc_kind = "" if is_pdf else self._is_spreadsheet(url, content_type, content)
        is_html = (not is_pdf) and (not doc_kind) and (
            "html" in content_type or content_type == "" or "xml" in content_type)
        result = self._normalize_result(content, content_type, final_url,
                                        is_pdf, is_html, doc_kind)
        self._save_cache(url, university, result)
        return result

    @staticmethod
    def _is_pdf(url: str, content_type: str, content: bytes) -> bool:
        if "pdf" in content_type:
            return True
        if looks_like_pdf_url(url):
            return True
        return content[:1024].lstrip()[:5] == b"%PDF-"

    @staticmethod
    def _is_spreadsheet(url: str, content_type: str, content: bytes) -> str:
        path = urlparse(url.lower()).path
        if "spreadsheetml" in content_type or path.endswith(".xlsx"):
            return "xlsx"
        if "vnd.ms-excel" in content_type or path.endswith(".xls"):
            return "xls"
        if content[:4] == b"PK\x03\x04":
            import io as _io
            import zipfile
            try:
                with zipfile.ZipFile(_io.BytesIO(content)) as z:
                    if any(n.startswith("xl/") for n in z.namelist()[:80]):
                        return "xlsx"
            except Exception:
                pass
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "xls"
        return ""

    @staticmethod
    def _normalize_result(content, content_type, final_url, is_pdf, is_html,
                          doc_kind="") -> dict:
        text = ""
        if is_html:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")
        return {"content": content, "text": text, "content_type": content_type,
                "final_url": final_url, "is_pdf": is_pdf, "is_html": is_html,
                "doc_kind": doc_kind}

    def _cache_paths(self, url: str, university: dict) -> tuple[Path, Path]:
        slug = slugify(university.get("name") or "misc")
        out_dir = self.raw_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        h = url_hash(url)
        return out_dir / f"{h}.meta.json", out_dir / h

    def _load_cache(self, url: str, university: dict) -> dict | None:
        if not self.use_cache:
            return None
        meta_path, _ = self._cache_paths(url, university)
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body_path = Path(meta["body_path"])
            content = body_path.read_bytes()
        except Exception:
            return None
        return self._normalize_result(
            content, meta.get("content_type", ""), meta.get("final_url", url),
            meta.get("is_pdf", False), meta.get("is_html", False),
            meta.get("doc_kind", ""),
        )

    def _save_cache(self, url: str, university: dict, result: dict) -> None:
        if not self.use_cache:
            return
        meta_path, body_base = self._cache_paths(url, university)
        ext = ("pdf" if result["is_pdf"]
               else result.get("doc_kind")
               or ("html" if result["is_html"] else "bin"))
        body_path = body_base.with_suffix("." + ext)
        try:
            body_path.write_bytes(result["content"])
            meta_path.write_text(json.dumps({
                "url": url,
                "final_url": result["final_url"],
                "content_type": result["content_type"],
                "is_pdf": result["is_pdf"],
                "is_html": result["is_html"],
                "doc_kind": result.get("doc_kind", ""),
                "body_path": str(body_path),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"cache write failed for {url}: {e}")

    def _extract_links(self, html, base_url,
                       allowed_domain) -> tuple[list[tuple[int, str]], list[str]]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return [], []

        soup = BeautifulSoup(html, "html.parser")
        scored_links: list[tuple[int, str]] = []
        pdf_links: list[str] = []
        seen: set[str] = set()

        for el in soup.find_all(["a", "iframe", "embed", "object"]):
            raw = (el.get("href") or el.get("src") or el.get("data") or "").strip()
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:",
                                          "about:", "data:")):
                continue

            if self.fetch_external_docs and "://" in raw:
                ext_url = external_doc_download_url(raw)
                if ext_url:
                    if ext_url not in seen:
                        seen.add(ext_url)
                        pdf_links.append(ext_url)
                    continue

            full_url = normalize_url(raw, base=base_url)
            if not full_url or full_url in seen:
                continue
            if not same_registered_domain(full_url, allowed_domain):
                continue
            seen.add(full_url)

            anchor_text = el.get_text(" ", strip=True) if el.name == "a" else ""
            path_low = urlparse(full_url).path.lower()

            is_download = any(w in path_low for w in ("download", "descargar",
                                                      "documento", "archivo", "adjunto"))
            is_doc = (looks_like_pdf_url(full_url)
                      or path_low.endswith((".xlsx", ".xls"))
                      or (is_download
                          and self.COURSE_ANCHOR_PATTERNS.search(anchor_text)))
            if is_doc:
                pdf_links.append(full_url)
                continue

            if el.name != "a":
                continue

            if self.HARD_SKIP_PATTERNS.search(path_low):
                continue

            prio = self._link_priority(full_url, path_low, anchor_text)

            host = urlparse(full_url).netloc
            if host not in self._seen_hosts:
                self._seen_hosts.add(host)
                host_and_anchor = re.sub(r"[-.]", " ", host) + " " + anchor_text
                first_label = host.split(".")[0]
                if (match_terms(host_and_anchor, ACADEMIC_SEED_TERMS)
                        or match_terms(host_and_anchor, STEM_TERMS)
                        or re.fullmatch(r"[a-z]{2,4}", first_label)):
                    prio = min(prio, 2)
                else:
                    prio = min(prio, 3)

            scored_links.append((prio, full_url))

        return scored_links, pdf_links

    def _link_priority(self, url: str, path_low: str, anchor_text: str) -> int:
        path_text = re.sub(r"[-_/.+%]+", " ", path_low)
        if (self.CURRICULUM_PRIORITY_PATTERNS.search(url)
                or self.CURRICULUM_PRIORITY_PATTERNS.search(anchor_text)):
            stem = match_terms(path_text + " " + anchor_text, STEM_TERMS)
            return 2 if stem else 3
        if (self.LOW_PRIORITY_URL_PATTERNS.search(path_low)
                or match_terms(anchor_text, LOW_PRIORITY_TERMS)):
            return 5
        if (self.COURSE_URL_PATTERNS.search(url)
                or self.COURSE_ANCHOR_PATTERNS.search(anchor_text)
                or match_terms(path_text + " " + anchor_text, STEM_TERMS)):
            if match_terms(path_text + " " + anchor_text, STEM_TERMS):
                return 3
            return 4
        return 6


class RSSCrawler:
    def __init__(self, cfg: dict):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=1.5)
        self.session = _make_session("", self.timeout, 2)

    def crawl_feed(self, source: dict) -> Generator[dict, None, None]:
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser no instalado: pip install feedparser")
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
            yield {"type": "rss", "content": entry,
                   "url": getattr(entry, "link", ""),
                   "university": None,
                   "rss_source_name": source["name"]}
        logger.info(f"  RSS {source['name']}: {len(feed.entries)} entradas")


class TwitterCrawler:
    def __init__(self, cfg: dict, bearer_token: str | None = None):
        self.bearer_token = bearer_token
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=3.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_queries(self, source: dict) -> Generator[dict, None, None]:
        if not self.bearer_token:
            return
        for query in source.get("queries", []):
            yield from self._api_search(query)

    def _api_search(self, query: str) -> Generator[dict, None, None]:
        self.limiter.wait()
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        params = {
            "query": f"{query} -is:retweet lang:es OR lang:pt OR lang:en",
            "max_results": 100,
            "tweet.fields": "created_at,author_id",
        }
        try:
            resp = self.session.get(url, headers=headers, params=params)
            resp.raise_for_status()
            for tweet in resp.json().get("data", []):
                yield {"type": "tweet", "content": tweet,
                       "url": f"https://twitter.com/i/web/status/{tweet['id']}",
                       "university": None, "search_query": query}
        except Exception as e:
            logger.warning(f"Twitter API '{query}': {e}")


class RedditCrawler:
    def __init__(self, cfg: dict, praw_cfg: dict | None = None):
        self.timeout = cfg["scraper"]["request_timeout_sec"]
        self.limiter = RateLimiter(min_delay=2.0)
        self.session = _make_session("", self.timeout, 2)

    def crawl_source(self, source: dict) -> Generator[dict, None, None]:
        for subreddit in source.get("subreddits", []):
            for query in source.get("queries", []):
                yield from self._search(subreddit, query)

    def _search(self, subreddit: str, query: str) -> Generator[dict, None, None]:
        self.limiter.wait()
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {"q": query, "sort": "relevance", "limit": 25, "restrict_sr": 1}
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            for post in resp.json().get("data", {}).get("children", []):
                data = post.get("data", {})
                yield {"type": "reddit_post", "content": data,
                       "url": data.get("url", ""), "university": None,
                       "subreddit": subreddit}
        except Exception as e:
            logger.warning(f"Reddit r/{subreddit} '{query}': {e}")