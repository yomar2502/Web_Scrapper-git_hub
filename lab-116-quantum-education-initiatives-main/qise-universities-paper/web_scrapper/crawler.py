"""Tests for crawler.py - 100% coverage."""

import pytest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, mock_open

import requests

from crawler import (
    WebCrawler,
    RSSCrawler,
    TwitterCrawler,
    RedditCrawler,
    CrawlStats,
    FetchResult,
    _make_session,
    ACADEMIC_UA,
    logger,
)


# ── TEST MAKE SESSION ─────────────────────────────────────────────────────

class TestMakeSession:
    """Test _make_session function."""

    def test_make_session_default(self):
        """Test creating session with default user agent."""
        session = _make_session(None, 30, 3)
        assert session.headers["User-Agent"] == ACADEMIC_UA
        assert "Accept" in session.headers
        assert "Accept-Language" in session.headers

    def test_make_session_custom_ua(self):
        """Test creating session with custom user agent."""
        session = _make_session("custom-bot", 30, 3)
        assert session.headers["User-Agent"] == "custom-bot"


# ── TEST CRAWLSTATS ───────────────────────────────────────────────────────

class TestCrawlStats:
    """Test CrawlStats dataclass."""

    def test_defaults(self):
        """Test default values."""
        stats = CrawlStats()
        assert stats.pages_crawled == 0
        assert stats.html_pages == 0
        assert stats.pdfs_detected == 0

    def test_custom_values(self):
        """Test custom values."""
        stats = CrawlStats(pages_crawled=10, html_pages=5, pdfs_detected=3)
        assert stats.pages_crawled == 10
        assert stats.html_pages == 5
        assert stats.pdfs_detected == 3


# ── TEST FETCHRESULT ─────────────────────────────────────────────────────

class TestFetchResult:
    """Test FetchResult dataclass."""

    def test_defaults(self):
        """Test default values."""
        result = FetchResult(
            content=b"test",
            text="test",
            content_type="text/html",
            final_url="https://test.com",
            is_pdf=False,
            is_html=True,
        )
        assert result.doc_kind == ""

    def test_with_doc_kind(self):
        """Test with doc_kind."""
        result = FetchResult(
            content=b"test",
            text="test",
            content_type="application/vnd.ms-excel",
            final_url="https://test.com",
            is_pdf=False,
            is_html=False,
            doc_kind="xls",
        )
        assert result.doc_kind == "xls"


# ── TEST WEBCRAWLER ──────────────────────────────────────────────────────

class TestWebCrawler:
    """Test WebCrawler class."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        return {
            "scraper": {
                "request_delay_sec": 0.1,
                "request_timeout_sec": 10,
                "max_retries": 2,
                "max_depth": 2,
                "max_pages_per_university": 10,
                "download_pdfs": True,
                "use_cache": True,
                "respect_robots": True,
                "max_pdf_mb": 40,
                "max_pdfs_per_domain": 5,
                "fetch_external_docs": True,
                "user_agent": "test-bot",
            },
            "output": {"raw_dir": "data/raw"},
        }

    @pytest.fixture
    def mock_university(self):
        """Create mock university."""
        return {
            "name": "Test University",
            "base_url": "https://test.edu",
            "catalog_urls": ["https://test.edu/cursos"],
            "seed_origin": "manual",
        }

    def test_init(self, mock_config):
        """Test crawler initialization."""
        crawler = WebCrawler(mock_config)
        assert crawler.delay == 0.1
        assert crawler.timeout == 10
        assert crawler.max_retries == 2
        assert crawler.global_max_depth == 2
        assert crawler.global_max_pages == 10
        assert crawler.download_pdfs is True
        assert crawler.use_cache is True
        assert crawler.max_pdf_bytes == 40 * 1024 * 1024
        assert crawler.max_pdfs_per_domain == 5
        assert crawler.fetch_external_docs is True
        assert crawler.user_agent == "test-bot"
        assert crawler.stats == {}
        assert crawler._seen_hosts == set()
        assert crawler.raw_dir == Path("data/raw")

    def test_init_creates_dir(self, mock_config, tmp_path):
        """Test that raw directory is created."""
        mock_config["output"]["raw_dir"] = str(tmp_path / "raw")
        crawler = WebCrawler(mock_config)
        assert (tmp_path / "raw").exists()

    def test_init_defaults(self):
        """Test initialization with missing config values."""
        config = {
            "scraper": {
                "request_delay_sec": 1.0,
                "request_timeout_sec": 30,
                "max_retries": 3,
                "max_depth": 2,
                "max_pages_per_university": 100,
            },
            "output": {"raw_dir": "data/raw"},
        }
        crawler = WebCrawler(config)
        assert crawler.download_pdfs is True
        assert crawler.use_cache is True
        assert crawler.max_pdf_bytes == 40 * 1024 * 1024
        assert crawler.max_pdfs_per_domain == 50
        assert crawler.fetch_external_docs is True
        assert crawler.user_agent == ACADEMIC_UA

    def test_is_pdf_by_content_type(self, mock_config):
        """Test PDF detection by Content-Type."""
        crawler = WebCrawler(mock_config)
        assert crawler._is_pdf("test.pdf", "application/pdf", b"") is True
        assert crawler._is_pdf("test.pdf", "application/x-pdf", b"") is True

    def test_is_pdf_by_url(self, mock_config):
        """Test PDF detection by URL."""
        crawler = WebCrawler(mock_config)
        assert crawler._is_pdf("test.pdf", "text/html", b"") is True
        assert crawler._is_pdf("test?format=pdf", "text/html", b"") is True

    def test_is_pdf_by_magic_bytes(self, mock_config):
        """Test PDF detection by magic bytes."""
        crawler = WebCrawler(mock_config)
        assert crawler._is_pdf("test", "text/html", b"%PDF-1.4") is True
        assert crawler._is_pdf("test", "text/html", b"not pdf") is False

    def test_is_spreadsheet_xlsx_content_type(self, mock_config):
        """Test XLSX detection by Content-Type."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet(
            "test.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b""
        )
        assert result == "xlsx"

    def test_is_spreadsheet_xlsx_extension(self, mock_config):
        """Test XLSX detection by extension."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet("test.xlsx", "text/plain", b"")
        assert result == "xlsx"

    def test_is_spreadsheet_xls_content_type(self, mock_config):
        """Test XLS detection by Content-Type."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet(
            "test.xls",
            "application/vnd.ms-excel",
            b""
        )
        assert result == "xls"

    def test_is_spreadsheet_xls_extension(self, mock_config):
        """Test XLS detection by extension."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet("test.xls", "text/plain", b"")
        assert result == "xls"

    def test_is_spreadsheet_zip_magic(self, mock_config):
        """Test XLSX detection by ZIP magic bytes."""
        crawler = WebCrawler(mock_config)
        # Simular un archivo ZIP con contenido xl/
        with patch('zipfile.ZipFile') as mock_zip:
            mock_zip.return_value.__enter__.return_value.namelist.return_value = ["xl/workbook.xml"]
            result = crawler._is_spreadsheet("test", "text/plain", b"PK\x03\x04")
            assert result == "xlsx"

    def test_is_spreadsheet_ole_magic(self, mock_config):
        """Test XLS detection by OLE magic bytes."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet("test", "text/plain", b"\xd0\xcf\x11\xe0")
        assert result == "xls"

    def test_is_spreadsheet_unknown(self, mock_config):
        """Test unknown spreadsheet type."""
        crawler = WebCrawler(mock_config)
        result = crawler._is_spreadsheet("test", "text/plain", b"unknown")
        assert result == ""

    def test_build_result_html(self, mock_config):
        """Test building HTML result."""
        crawler = WebCrawler(mock_config)
        result = crawler._build_result(
            content=b"<html>test</html>",
            content_type="text/html",
            final_url="https://test.com",
            is_pdf=False,
            is_html=True,
        )
        assert result.text == "<html>test</html>"
        assert result.is_html is True
        assert result.content == b"<html>test</html>"

    def test_build_result_html_unicode_error(self, mock_config):
        """Test building HTML result with Unicode error."""
        crawler = WebCrawler(mock_config)
        result = crawler._build_result(
            content=b"\xff\xfe<html>",
            content_type="text/html",
            final_url="https://test.com",
            is_pdf=False,
            is_html=True,
        )
        assert result.text is not None

    def test_build_result_pdf(self, mock_config):
        """Test building PDF result."""
        crawler = WebCrawler(mock_config)
        result = crawler._build_result(
            content=b"%PDF-1.4",
            content_type="application/pdf",
            final_url="https://test.com",
            is_pdf=True,
            is_html=False,
        )
        assert result.is_pdf is True
        assert result.text == ""

    def test_cache_paths(self, mock_config, mock_university):
        """Test cache path generation."""
        crawler = WebCrawler(mock_config)
        meta_path, body_path = crawler._cache_paths("https://test.edu/url", mock_university)
        assert "Test_University" in str(meta_path)
        assert meta_path.suffix == ".json"
        assert body_path.suffix == ""

    def test_load_cache_missing(self, mock_config, mock_university):
        """Test loading missing cache."""
        crawler = WebCrawler(mock_config)
        with patch('pathlib.Path.exists', return_value=False):
            result = crawler._load_cache("https://test.edu", mock_university)
            assert result is None

    def test_load_cache_disabled(self, mock_config, mock_university):
        """Test loading cache when disabled."""
        mock_config["scraper"]["use_cache"] = False
        crawler = WebCrawler(mock_config)
        result = crawler._load_cache("https://test.edu", mock_university)
        assert result is None

    def test_load_cache_valid(self, mock_config, mock_university, tmp_path):
        """Test loading valid cache."""
        mock_config["output"]["raw_dir"] = str(tmp_path)
        crawler = WebCrawler(mock_config)
        
        meta_content = {
            "content_type": "text/html",
            "final_url": "https://test.edu",
            "is_pdf": False,
            "is_html": True,
            "doc_kind": "",
            "body_path": str(tmp_path / "body.html"),
        }
        
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.read_text', return_value=json.dumps(meta_content)):
                with patch('pathlib.Path.read_bytes', return_value=b"<html>test</html>"):
                    result = crawler._load_cache("https://test.edu", mock_university)
                    assert result is not None
                    assert result.content == b"<html>test</html>"

    def test_save_cache(self, mock_config, mock_university, tmp_path):
        """Test saving cache."""
        mock_config["output"]["raw_dir"] = str(tmp_path)
        crawler = WebCrawler(mock_config)
        
        result = FetchResult(
            content=b"<html>test</html>",
            text="<html>test</html>",
            content_type="text/html",
            final_url="https://test.edu",
            is_pdf=False,
            is_html=True,
        )
        
        with patch('pathlib.Path.write_bytes') as mock_write_bytes:
            with patch('pathlib.Path.write_text') as mock_write_text:
                crawler._save_cache("https://test.edu", mock_university, result)
                mock_write_bytes.assert_called_once()
                mock_write_text.assert_called_once()

    def test_save_cache_disabled(self, mock_config, mock_university):
        """Test saving cache when disabled."""
        mock_config["scraper"]["use_cache"] = False
        crawler = WebCrawler(mock_config)
        
        result = FetchResult(
            content=b"test",
            text="test",
            content_type="text/html",
            final_url="https://test.edu",
            is_pdf=False,
            is_html=True,
        )
        
        with patch('pathlib.Path.write_bytes') as mock_write:
            crawler._save_cache("https://test.edu", mock_university, result)
            mock_write.assert_not_called()

    def test_fetch_with_cache(self, mock_config, mock_university):
        """Test fetch with cache hit."""
        crawler = WebCrawler(mock_config)
        
        cached_result = FetchResult(
            content=b"cached",
            text="cached",
            content_type="text/html",
            final_url="https://test.edu",
            is_pdf=False,
            is_html=True,
        )
        
        with patch.object(crawler, '_load_cache', return_value=cached_result):
            result = crawler._fetch("https://test.edu", mock_university)
            assert result == cached_result

    def test_fetch_http_error(self, mock_config, mock_university):
        """Test fetch with HTTP error."""
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.HTTPError("404")
            result = crawler._fetch("https://test.edu", mock_university)
            assert result is None

    def test_fetch_connection_error_retry_https(self, mock_config, mock_university):
        """Test fetch with connection error and HTTPS retry."""
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = [
                requests.exceptions.ConnectionError("Connection refused"),
                Mock(
                    status_code=200,
                    headers={"Content-Type": "text/html"},
                    content=b"<html>test</html>",
                    url="https://test.edu",
                )
            ]
            
            with patch.object(crawler, '_is_pdf', return_value=False):
                with patch.object(crawler, '_is_spreadsheet', return_value=""):
                    with patch.object(crawler, '_save_cache'):
                        result = crawler._fetch("http://test.edu", mock_university)
                        assert mock_get.call_count == 2

    def test_fetch_timeout(self, mock_config, mock_university):
        """Test fetch with timeout."""
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Timeout")
            result = crawler._fetch("https://test.edu", mock_university)
            assert result is None

    def test_fetch_general_exception(self, mock_config, mock_university):
        """Test fetch with general exception."""
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = Exception("Unknown error")
            result = crawler._fetch("https://test.edu", mock_university)
            assert result is None

    def test_fetch_pdf(self, mock_config, mock_university):
        """Test fetch PDF content."""
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.4",
                url="https://test.edu/doc.pdf",
            )
            
            with patch.object(crawler, '_is_pdf', return_value=True):
                with patch.object(crawler, '_save_cache'):
                    result = crawler._fetch("https://test.edu/doc.pdf", mock_university)
                    assert result is not None
                    assert result.is_pdf is True

    def test_fetch_truncated(self, mock_config, mock_university):
        """Test fetch with truncated content."""
        mock_config["scraper"]["max_pdf_mb"] = 1
        crawler = WebCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                iter_content=lambda *args: [b"x" * 1024 * 1024 * 2],  # 2MB
                url="https://test.edu/doc.pdf",
                close=Mock(),
            )
            
            with patch.object(crawler, '_is_pdf', return_value=True):
                with patch.object(crawler, '_save_cache'):
                    result = crawler._fetch("https://test.edu/doc.pdf", mock_university)
                    assert result is not None

    def test_link_priority_curriculum(self, mock_config):
        """Test link priority for curriculum."""
        crawler = WebCrawler(mock_config)
        priority = crawler._link_priority(
            "https://test.edu/plan-de-estudios",
            "/plan-de-estudios",
            "Plan de estudios"
        )
        assert priority in (2, 3)

    def test_link_priority_low(self, mock_config):
        """Test link priority for low priority."""
        crawler = WebCrawler(mock_config)
        priority = crawler._link_priority(
            "https://test.edu/noticias",
            "/noticias",
            "Noticias"
        )
        assert priority == 5

    def test_link_priority_default(self, mock_config):
        """Test link priority default."""
        crawler = WebCrawler(mock_config)
        priority = crawler._link_priority(
            "https://test.edu/random",
            "/random",
            "Random text"
        )
        assert priority == 6

    def test_link_priority_course(self, mock_config):
        """Test link priority for course."""
        crawler = WebCrawler(mock_config)
        priority = crawler._link_priority(
            "https://test.edu/curso-fisica",
            "/curso-fisica",
            "Curso de Física"
        )
        assert priority == 4

    def test_extract_links_requires_bs4(self, mock_config):
        """Test extract_links when BeautifulSoup not installed."""
        crawler = WebCrawler(mock_config)
        
        with patch('crawler.BeautifulSoup', None):
            with patch('builtins.__import__', side_effect=ImportError):
                scored, pdf = crawler._extract_links("<html></html>", "https://test.edu", "test.edu")
                assert scored == []
                assert pdf == []

    def test_extract_links_with_bs4(self, mock_config):
        """Test extract_links with BeautifulSoup."""
        crawler = WebCrawler(mock_config)
        html = """
        <html>
            <body>
                <a href="/doc.pdf">PDF</a>
                <a href="/curso">Curso</a>
                <a href="/download/malla">Descargar malla</a>
                <iframe src="https://drive.google.com/file/d/123"></iframe>
            </body>
        </html>
        """
        
        # Mock BeautifulSoup para evitar import real
        with patch('crawler.BeautifulSoup') as mock_bs:
            # Crear mock de elementos
            mock_a = Mock()
            mock_a.name = "a"
            mock_a.get.return_value = "/doc.pdf"
            mock_a.get_text.return_value = "PDF"
            
            mock_a2 = Mock()
            mock_a2.name = "a"
            mock_a2.get.return_value = "/curso"
            mock_a2.get_text.return_value = "Curso"
            
            mock_iframe = Mock()
            mock_iframe.name = "iframe"
            mock_iframe.get.return_value = "https://drive.google.com/file/d/123"
            mock_iframe.get_text.return_value = ""
            
            mock_soup = Mock()
            mock_soup.find_all.return_value = [mock_a, mock_a2, mock_iframe]
            mock_bs.return_value = mock_soup
            
            # Mock normalize_url y same_registered_domain
            with patch('crawler.normalize_url') as mock_norm:
                mock_norm.side_effect = lambda u, base=None: u if u.startswith("http") else f"https://test.edu{u}"
                
                with patch('crawler.same_registered_domain', return_value=True):
                    with patch('crawler.looks_like_pdf_url') as mock_pdf:
                        mock_pdf.return_value = False
                        
                        with patch.object(crawler, '_link_priority', return_value=4):
                            with patch('crawler.external_doc_download_url', return_value=""):
                                scored, pdf = crawler._extract_links(html, "https://test.edu", "test.edu")
                                # Debería tener al menos el enlace /curso
                                assert len(scored) >= 1

    def test_crawl_university_no_seeds(self, mock_config):
        """Test crawling with no seeds."""
        crawler = WebCrawler(mock_config)
        university = {"name": "Test", "catalog_urls": []}
        
        results = list(crawler.crawl_university(university))
        assert len(results) == 0

    def test_crawl_university_with_seeds(self, mock_config):
        """Test crawling with seeds."""
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/seed1", "https://test.edu/seed2"],
            "base_url": "https://test.edu",
        }
        
        # Mock robots allowed
        with patch.object(crawler.robots, 'allowed', return_value=True):
            # Mock fetch
            with patch.object(crawler, '_fetch') as mock_fetch:
                mock_fetch.return_value = FetchResult(
                    content=b"<html>test</html>",
                    text="<html>test</html>",
                    content_type="text/html",
                    final_url="https://test.edu/seed1",
                    is_pdf=False,
                    is_html=True,
                )
                
                # Mock extract_links
                with patch.object(crawler, '_extract_links', return_value=([], [])):
                    results = list(crawler.crawl_university(university))
                    assert len(results) == 1
                    assert results[0]["type"] == "html"

    def test_crawl_university_pdf(self, mock_config):
        """Test crawling PDF."""
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/doc.pdf"],
            "base_url": "https://test.edu",
        }
        
        with patch.object(crawler.robots, 'allowed', return_value=True):
            with patch.object(crawler, '_fetch') as mock_fetch:
                mock_fetch.return_value = FetchResult(
                    content=b"%PDF-1.4",
                    text="",
                    content_type="application/pdf",
                    final_url="https://test.edu/doc.pdf",
                    is_pdf=True,
                    is_html=False,
                )
                
                results = list(crawler.crawl_university(university))
                assert len(results) == 1
                assert results[0]["type"] == "pdf"

    def test_crawl_university_pdf_cap(self, mock_config):
        """Test PDF cap."""
        mock_config["scraper"]["max_pdfs_per_domain"] = 1
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/doc1.pdf", "https://test.edu/doc2.pdf"],
            "base_url": "https://test.edu",
        }
        
        with patch.object(crawler.robots, 'allowed', return_value=True):
            with patch.object(crawler, '_fetch') as mock_fetch:
                mock_fetch.return_value = FetchResult(
                    content=b"%PDF-1.4",
                    text="",
                    content_type="application/pdf",
                    final_url="https://test.edu/doc1.pdf",
                    is_pdf=True,
                    is_html=False,
                )
                
                results = list(crawler.crawl_university(university))
                # Solo debería haber 1 PDF debido al cap
                assert len(results) == 1

    def test_crawl_university_robots_disallowed(self, mock_config):
        """Test crawling with robots.txt disallow."""
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/seed"],
            "base_url": "https://test.edu",
        }
        
        with patch.object(crawler.robots, 'allowed', return_value=False):
            results = list(crawler.crawl_university(university))
            assert len(results) == 0

    def test_crawl_university_fetch_fails(self, mock_config):
        """Test crawling when fetch fails."""
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/seed"],
            "base_url": "https://test.edu",
        }
        
        with patch.object(crawler.robots, 'allowed', return_value=True):
            with patch.object(crawler, '_fetch', return_value=None):
                results = list(crawler.crawl_university(university))
                assert len(results) == 0

    def test_crawl_university_non_html(self, mock_config):
        """Test crawling non-HTML content."""
        crawler = WebCrawler(mock_config)
        university = {
            "name": "Test",
            "catalog_urls": ["https://test.edu/image.png"],
            "base_url": "https://test.edu",
        }
        
        with patch.object(crawler.robots, 'allowed', return_value=True):
            with patch.object(crawler, '_fetch') as mock_fetch:
                mock_fetch.return_value = FetchResult(
                    content=b"image",
                    text="",
                    content_type="image/png",
                    final_url="https://test.edu/image.png",
                    is_pdf=False,
                    is_html=False,
                )
                
                results = list(crawler.crawl_university(university))
                assert len(results) == 0


# ── TEST RSS CRAWLER ─────────────────────────────────────────────────────

class TestRSSCrawler:
    """Test RSSCrawler class."""

    @pytest.fixture
    def mock_config(self):
        return {"scraper": {"request_timeout_sec": 30}}

    def test_init(self, mock_config):
        """Test initialization."""
        crawler = RSSCrawler(mock_config)
        assert crawler.timeout == 30
        assert crawler.limiter is not None

    def test_crawl_feed_no_feedparser(self, mock_config):
        """Test crawling without feedparser."""
        crawler = RSSCrawler(mock_config)
        
        with patch('crawler.feedparser', None):
            with patch('builtins.__import__', side_effect=ImportError):
                results = list(crawler.crawl_feed({"name": "test", "url": "https://test.com"}))
                assert len(results) == 0

    def test_crawl_feed_success(self, mock_config):
        """Test successful feed crawl."""
        crawler = RSSCrawler(mock_config)
        
        mock_feed = Mock()
        mock_feed.entries = [
            Mock(link="https://test.com/1", title="Entry 1"),
            Mock(link="https://test.com/2", title="Entry 2"),
        ]
        
        with patch('crawler.feedparser') as mock_feedparser:
            mock_feedparser.parse.return_value = mock_feed
            
            with patch.object(crawler.session, 'get') as mock_get:
                mock_get.return_value = Mock(text="<rss>test</rss>")
                
                results = list(crawler.crawl_feed({"name": "test", "url": "https://test.com"}))
                assert len(results) == 2
                assert results[0]["type"] == "rss"
                assert results[0]["rss_source_name"] == "test"

    def test_crawl_feed_error(self, mock_config):
        """Test feed crawl with error."""
        crawler = RSSCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = Exception("Network error")
            
            results = list(crawler.crawl_feed({"name": "test", "url": "https://test.com"}))
            assert len(results) == 0


# ── TEST TWITTER CRAWLER ─────────────────────────────────────────────────

class TestTwitterCrawler:
    """Test TwitterCrawler class."""

    @pytest.fixture
    def mock_config(self):
        return {"scraper": {"request_timeout_sec": 30}}

    def test_init_no_token(self, mock_config):
        """Test initialization without token."""
        crawler = TwitterCrawler(mock_config, None)
        assert crawler.bearer_token is None

    def test_init_with_token(self, mock_config):
        """Test initialization with token."""
        crawler = TwitterCrawler(mock_config, "test_token")
        assert crawler.bearer_token == "test_token"

    def test_crawl_queries_no_token(self, mock_config):
        """Test crawling without token."""
        crawler = TwitterCrawler(mock_config, None)
        results = list(crawler.crawl_queries({"queries": ["q1"]}))
        assert len(results) == 0

    def test_crawl_queries_success(self, mock_config):
        """Test successful query crawl."""
        crawler = TwitterCrawler(mock_config, "test_token")
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {"data": [{"id": "1", "text": "tweet"}]},
            )
            
            results = list(crawler.crawl_queries({"queries": ["quantum"]}))
            assert len(results) == 1
            assert results[0]["type"] == "tweet"
            assert results[0]["search_query"] == "quantum"

    def test_crawl_queries_error(self, mock_config):
        """Test query crawl with error."""
        crawler = TwitterCrawler(mock_config, "test_token")
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = Exception("API error")
            
            results = list(crawler.crawl_queries({"queries": ["quantum"]}))
            assert len(results) == 0


# ── TEST REDDIT CRAWLER ──────────────────────────────────────────────────

class TestRedditCrawler:
    """Test RedditCrawler class."""

    @pytest.fixture
    def mock_config(self):
        return {"scraper": {"request_timeout_sec": 30}}

    def test_init(self, mock_config):
        """Test initialization."""
        crawler = RedditCrawler(mock_config)
        assert crawler.timeout == 30

    def test_crawl_source_success(self, mock_config):
        """Test successful source crawl."""
        crawler = RedditCrawler(mock_config)
        
        with patch.object(crawler, '_search') as mock_search:
            mock_search.return_value = [{"type": "reddit_post", "content": {"title": "post"}}]
            
            results = list(crawler.crawl_source({
                "subreddits": ["QuantumComputing"],
                "queries": ["quantum"],
            }))
            assert len(results) == 1

    def test_search_success(self, mock_config):
        """Test successful search."""
        crawler = RedditCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {
                    "data": {
                        "children": [
                            {"data": {"title": "post1", "url": "https://reddit.com/1"}},
                            {"data": {"title": "post2", "url": "https://reddit.com/2"}},
                        ]
                    }
                },
            )
            
            results = list(crawler._search("QuantumComputing", "quantum"))
            assert len(results) == 2
            assert results[0]["type"] == "reddit_post"
            assert results[0]["subreddit"] == "QuantumComputing"

    def test_search_error(self, mock_config):
        """Test search with error."""
        crawler = RedditCrawler(mock_config)
        
        with patch.object(crawler.session, 'get') as mock_get:
            mock_get.side_effect = Exception("API error")
            
            results = list(crawler._search("QuantumComputing", "quantum"))
            assert len(results) == 0