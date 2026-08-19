"""dispatcher.py — Routes each source configuration to the appropriate crawler."""

import os
from typing import Generator, List, Dict, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path

from crawler import (
    WebCrawler,
    RSSCrawler,
    TwitterCrawler,
    RedditCrawler,
)

from extractor import (
    extract_from_html,
    extract_from_pdf,
    extract_from_xlsx,
    extract_from_rss_entry,
    extract_from_tweet,
    extract_from_reddit_post,
)

from utils import get_logger


logger = get_logger("dispatcher")


# ── CONSTANTS ──────────────────────────────────────────────────────────────────

SOURCE_TYPE_WEB = "web"
SOURCE_TYPE_RSS = "rss"
SOURCE_TYPE_TWITTER = "twitter"
SOURCE_TYPE_REDDIT = "reddit"
SOURCE_TYPE_LINKEDIN = "linkedin"

CONTENT_TYPE_HTML = "html"
CONTENT_TYPE_PDF = "pdf"
CONTENT_TYPE_XLSX = "xlsx"
CONTENT_TYPE_XLS = "xls"
CONTENT_TYPE_RSS = "rss"
CONTENT_TYPE_TWEET = "tweet"
CONTENT_TYPE_REDDIT_POST = "reddit_post"


# ── DATA CLASSES ──────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    """Configuration for a source."""
    name: str
    source_type: str
    url: Optional[str] = None
    country: str = ""
    country_code: str = ""
    queries: List[str] = field(default_factory=list)
    subreddits: List[str] = field(default_factory=list)
    catalog_urls: List[str] = field(default_factory=list)
    base_url: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceConfig":
        """Create a SourceConfig from a dictionary."""
        return cls(
            name=data.get("name", "unknown"),
            source_type=data.get("type", ""),
            url=data.get("url", ""),
            country=data.get("country", ""),
            country_code=data.get("country_code", ""),
            queries=data.get("queries", []),
            subreddits=data.get("subreddits", []),
            catalog_urls=data.get("catalog_urls", []),
            base_url=data.get("base_url", data.get("url", "")),
        )


@dataclass
class ExtractedRecord:
    """Standardized extracted record."""
    source_type: str
    source_name: str
    raw_text: str
    url: str
    title: str = ""
    date: str = ""
    country: str = ""
    country_code: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for output."""
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "raw_text": self.raw_text,
            "url": self.url,
            "title": self.title,
            "date": self.date,
            "country": self.country,
            "country_code": self.country_code,
            "metadata": self.metadata,
        }


# ── DISPATCHER ────────────────────────────────────────────────────────────────

class Dispatcher:
    """Coordinate crawlers and extractors for all configured sources."""

    def __init__(self, cfg: Dict[str, Any], sources: Dict[str, Any]):
        """Initialize the dispatcher with configuration and sources."""
        self.cfg = cfg or {}
        self.sources = sources or {}
        
        self._validate_config()

        self.web_crawler = WebCrawler(self.cfg)
        self.rss_crawler = RSSCrawler(self.cfg)

        twitter_token = os.getenv("TWITTER_BEARER_TOKEN")
        if not twitter_token:
            logger.warning("TWITTER_BEARER_TOKEN is not set. Twitter sources will be skipped.")
        
        self.twitter_crawler = TwitterCrawler(
            self.cfg,
            bearer_token=twitter_token,
        )

        praw_config = self._build_reddit_config()
        if praw_config is None:
            logger.warning("REDDIT_CLIENT_ID is not set. Reddit sources may be skipped.")

        self.reddit_crawler = RedditCrawler(
            self.cfg,
            praw_cfg=praw_config,
        )

    def _validate_config(self) -> None:
        """Validate configuration and log any issues."""
        if not self.cfg:
            logger.warning("Empty configuration provided")
        
        if not self.sources:
            logger.warning("No sources configured")
        else:
            uni_count = len(self.sources.get("universities", []))
            news_count = len(self.sources.get("news_sources", []))
            social_count = len(self.sources.get("social_sources", []))
            logger.info(
                f"Configuration loaded: {uni_count} universities, "
                f"{news_count} news sources, {social_count} social sources"
            )

    def stream_all_records(self) -> Generator[Dict[str, Any], None, None]:
        """Yield extracted records from all configured source types."""
        universities = self.sources.get("universities", [])
        news_sources = self.sources.get("news_sources", [])
        social_sources = self.sources.get("social_sources", [])
        
        logger.info(
            f"Starting dispatch: {len(universities)} universities, "
            f"{len(news_sources)} news, {len(social_sources)} social"
        )

        yield from self._stream_universities()
        yield from self._stream_news()
        yield from self._stream_social()
        
        logger.info("Dispatch complete")

    @staticmethod
    def _build_reddit_config() -> Optional[Dict[str, str]]:
        """Build PRAW configuration from environment variables."""
        client_id = os.getenv("REDDIT_CLIENT_ID")
        if not client_id:
            return None

        return {
            "client_id": client_id,
            "client_secret": os.getenv("REDDIT_CLIENT_SECRET", ""),
            "user_agent": os.getenv(
                "REDDIT_USER_AGENT",
                "QISE-LatAm-Research-Bot/1.0"
            ),
        }

    def _stream_universities(self) -> Generator[Dict[str, Any], None, None]:
        """Crawl and extract records from configured universities."""
        universities = self.sources.get("universities", [])
        
        if not universities:
            logger.debug("No university sources configured")
            return

        logger.info(f"Dispatching {len(universities)} university sources")

        for university in universities:
            name = university.get("name", "unknown")
            source_type = university.get("type", "")
            
            if source_type != SOURCE_TYPE_WEB:
                logger.warning(
                    f"Skipping university '{name}': "
                    f"unsupported type '{source_type}' (only 'web' supported)"
                )
                continue

            try:
                for raw in self.web_crawler.crawl_university(university):
                    for record in self._extract_raw(raw, university):
                        if self._is_valid_record(record):
                            yield record

            except Exception as e:
                logger.exception(
                    f"Failed to crawl university '{name}': {e}"
                )

    def _stream_news(self) -> Generator[Dict[str, Any], None, None]:
        """Crawl and extract records from configured news sources."""
        news_sources = self.sources.get("news_sources", [])
        
        if not news_sources:
            logger.debug("No news sources configured")
            return

        logger.info(f"Dispatching {len(news_sources)} news sources")

        for source in news_sources:
            source_config = SourceConfig.from_dict(source)
            source_name = source_config.name
            source_type = source_config.source_type

            try:
                if source_type == SOURCE_TYPE_RSS:
                    yield from self._process_rss_source(source_config)

                elif source_type == SOURCE_TYPE_WEB:
                    yield from self._process_web_news_source(source_config)

                else:
                    logger.warning(
                        f"Skipping news source '{source_name}': "
                        f"unsupported type '{source_type}'"
                    )

            except Exception as e:
                logger.exception(
                    f"Failed to process news source '{source_name}': {e}"
                )

    def _process_rss_source(self, source: SourceConfig) -> Generator[Dict[str, Any], None, None]:
        """Process an RSS news source."""
        for raw in self.rss_crawler.crawl_feed({
            "name": source.name,
            "url": source.url,
        }):
            entry = raw.get("content")
            if not entry:
                continue

            record = extract_from_rss_entry(entry, source.name)
            if record and record.get("raw_text"):
                record["country"] = source.country
                record["country_code"] = source.country_code
                yield record

    def _process_web_news_source(self, source: SourceConfig) -> Generator[Dict[str, Any], None, None]:
        """Process a web news source."""
        if not source.url:
            logger.warning(
                f"Skipping web news source '{source.name}': missing URL"
            )
            return

        dummy_source = {
            "name": source.name,
            "country": source.country,
            "country_code": source.country_code,
            "base_url": source.url,
            "catalog_urls": [source.url],
        }

        for raw in self.web_crawler.crawl_university(dummy_source):
            for record in self._extract_raw(raw, dummy_source):
                if self._is_valid_record(record):
                    yield record

    def _stream_social(self) -> Generator[Dict[str, Any], None, None]:
        """Crawl and extract records from social media sources."""
        social_sources = self.sources.get("social_sources", [])
        
        if not social_sources:
            logger.debug("No social sources configured")
            return

        logger.info(f"Dispatching {len(social_sources)} social sources")

        for source in social_sources:
            source_config = SourceConfig.from_dict(source)
            source_name = source_config.name
            source_type = source_config.source_type

            try:
                if source_type == SOURCE_TYPE_TWITTER:
                    yield from self._process_twitter_source(source_config)

                elif source_type == SOURCE_TYPE_REDDIT:
                    yield from self._process_reddit_source(source_config)

                elif source_type == SOURCE_TYPE_LINKEDIN:
                    logger.warning(
                        f"LinkedIn source '{source_name}' skipped. "
                        "Automated scraping is not supported."
                    )

                else:
                    logger.warning(
                        f"Skipping social source '{source_name}': "
                        f"unsupported type '{source_type}'"
                    )

            except Exception as e:
                logger.exception(
                    f"Failed to process social source '{source_name}': {e}"
                )

    def _process_twitter_source(self, source: SourceConfig) -> Generator[Dict[str, Any], None, None]:
        """Process a Twitter source."""
        if not os.getenv("TWITTER_BEARER_TOKEN"):
            logger.warning(
                f"Skipping Twitter source '{source.name}': "
                "TWITTER_BEARER_TOKEN is not set"
            )
            return

        for raw in self.twitter_crawler.crawl_queries({
            "name": source.name,
            "queries": source.queries,
        }):
            tweet = raw.get("content")
            if not tweet:
                continue

            record = extract_from_tweet(tweet, raw.get("search_query", ""))
            if record and record.get("raw_text"):
                yield record

    def _process_reddit_source(self, source: SourceConfig) -> Generator[Dict[str, Any], None, None]:
        """Process a Reddit source."""
        if not os.getenv("REDDIT_CLIENT_ID"):
            logger.warning(
                f"Skipping Reddit source '{source.name}': "
                "REDDIT_CLIENT_ID is not set"
            )
            return

        for raw in self.reddit_crawler.crawl_source({
            "name": source.name,
            "subreddits": source.subreddits,
            "queries": source.queries,
        }):
            post = raw.get("content")
            if not post:
                continue

            record = extract_from_reddit_post(post, raw.get("subreddit", ""))
            if record and record.get("raw_text"):
                yield record

    def _extract_raw(
        self,
        raw: Dict[str, Any],
        source: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Route raw crawler output to the appropriate extractor."""
        if not raw:
            return []

        raw_type = raw.get("type")
        url = raw.get("url", "")
        content = raw.get("content", "")
        found_on = raw.get("found_on", "")
        source_name = source.get("name", "unknown")

        if not content:
            logger.debug(f"Empty content for '{url}' ({raw_type})")
            return []

        try:
            if raw_type == CONTENT_TYPE_HTML:
                return extract_from_html(
                    content,
                    url,
                    source,
                    found_on_page=found_on,
                )

            if raw_type == CONTENT_TYPE_PDF:
                pdf_bytes = self._ensure_bytes(content)
                return extract_from_pdf(
                    pdf_bytes,
                    url,
                    source,
                    found_on_page=found_on,
                )

            if raw_type in {CONTENT_TYPE_XLSX, CONTENT_TYPE_XLS}:
                excel_bytes = self._ensure_bytes(content)
                return extract_from_xlsx(
                    excel_bytes,
                    url,
                    source,
                    found_on_page=found_on,
                )

            logger.warning(
                f"Unknown raw type '{raw_type}' from source '{source_name}', URL: {url}"
            )
            return []

        except Exception as e:
            logger.exception(
                f"Extraction failed for source '{source_name}', "
                f"type '{raw_type}', URL: {url}: {e}"
            )
            return []

    @staticmethod
    def _ensure_bytes(content: Any) -> bytes:
        """Convert crawler content to bytes safely."""
        if isinstance(content, bytes):
            return content

        if isinstance(content, bytearray):
            return bytes(content)

        if isinstance(content, str):
            return content.encode("utf-8")

        raise TypeError(
            f"Expected bytes, bytearray, or str; "
            f"got {type(content).__name__}"
        )

    @staticmethod
    def _is_valid_record(record: Dict[str, Any]) -> bool:
        """Check if a record is valid and has content."""
        if not record:
            return False
            
        if not record.get("raw_text"):
            return False
            
        if len(record.get("raw_text", "").strip()) < 10:
            return False
            
        return True


# ── CONVENIENCE FUNCTIONS ──────────────────────────────────────────────────

def run_dispatcher(config_path: str) -> List[Dict[str, Any]]:
    """
    Convenience function to run the dispatcher and collect all records.
    
    Args:
        config_path: Path to configuration file (JSON or YAML)
        
    Returns:
        List of all extracted records
    """
    import json
    import yaml
    
    config_path = Path(config_path)
    
    # Soporte para YAML y JSON
    if config_path.suffix.lower() in (".yaml", ".yml"):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    
    dispatcher = Dispatcher(config, config.get("sources", {}))
    
    records = list(dispatcher.stream_all_records())
    logger.info(f"Collected {len(records)} total records")
    return records


def save_records(records: List[Dict[str, Any]], output_path: str) -> None:
    """
    Save extracted records to a JSON file.
    
    Args:
        records: List of extracted records
        output_path: Path to output file
    """
    import json
    from pathlib import Path
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Saved {len(records)} records to {output_path}")