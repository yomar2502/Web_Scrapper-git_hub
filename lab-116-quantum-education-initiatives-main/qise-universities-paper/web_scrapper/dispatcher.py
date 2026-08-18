"""
dispatcher.py

Routes each source configuration to the appropriate crawler and then
passes the raw crawler output to the corresponding extractor.

Returns a unified stream of extracted text records ready for classification.
"""

import os
from typing import Generator

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


class Dispatcher:
    """Coordinate crawlers and extractors for all configured sources."""

    def __init__(self, cfg: dict, sources: dict):
        self.cfg = cfg or {}
        self.sources = sources or {}

        # Web / RSS crawlers
        self.web_crawler = WebCrawler(self.cfg)
        self.rss_crawler = RSSCrawler(self.cfg)

        # Twitter crawler
        twitter_token = os.getenv("TWITTER_BEARER_TOKEN")

        if not twitter_token:
            logger.warning(
                "TWITTER_BEARER_TOKEN is not set. "
                "Twitter sources will be skipped."
            )

        self.twitter_crawler = TwitterCrawler(
            self.cfg,
            bearer_token=twitter_token,
        )

        # -------------------------
        # Reddit crawler
        # -------------------------
        praw_cfg = self._build_reddit_config()

        if praw_cfg is None:
            logger.warning(
                "REDDIT_CLIENT_ID is not set. "
                "Reddit sources may be skipped."
            )

        self.reddit_crawler = RedditCrawler(
            self.cfg,
            praw_cfg=praw_cfg,
        )


    def stream_all_records(self) -> Generator[dict, None, None]:
        """
        Yield extracted records from all configured source types.

        The order is:
            1. Universities
            2. News
            3. Social media
        """

        yield from self._stream_universities()
        yield from self._stream_news()
        yield from self._stream_social()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reddit_config() -> dict | None:
        """Build PRAW configuration from environment variables."""

        client_id = os.getenv("REDDIT_CLIENT_ID")

        if not client_id:
            return None

        return {
            "client_id": client_id,
            "client_secret": os.getenv("REDDIT_CLIENT_SECRET", ""),
        }

    def _stream_universities(self) -> Generator[dict, None, None]:
        """Crawl and extract records from configured universities."""

        universities = self.sources.get("universities", [])

        logger.info(
            "Dispatching %d university sources",
            len(universities),
        )

        for university in universities:

            if university.get("type") != "web":
                logger.warning(
                    "Skipping university '%s': unsupported type '%s'",
                    university.get("name", "unknown"),
                    university.get("type"),
                )
                continue

            name = university.get("name", "unknown")

            try:
                for raw in self.web_crawler.crawl_university(university):
                    yield from self._extract_raw(raw, university)

            except Exception:
                logger.exception(
                    "Failed to crawl university '%s'",
                    name,
                )

    def _stream_news(self) -> Generator[dict, None, None]:
        """Crawl and extract records from configured news sources."""

        news_sources = self.sources.get("news_sources", [])

        logger.info(
            "Dispatching %d news sources",
            len(news_sources),
        )

        for source in news_sources:

            source_type = source.get("type")
            source_name = source.get("name", "unknown")

            try:


                if source_type == "rss":

                    for raw in self.rss_crawler.crawl_feed(source):

                        entry = raw.get("content")

                        if not entry:
                            continue

                        record = extract_from_rss_entry(
                            entry,
                            source_name,
                        )

                        if record and record.get("raw_text"):
                            yield record

                elif source_type == "web":

                    dummy_source = {
                        "name": source_name,
                        "country": source.get("country", ""),
                        "country_code": source.get(
                            "country_code",
                            "",
                        ),
                        "base_url": source.get("url", ""),
                        "catalog_urls": [
                            source.get("url", "")
                        ],
                    }

                    if not dummy_source["base_url"]:
                        logger.warning(
                            "Skipping news source '%s': missing URL",
                            source_name,
                        )
                        continue

                    for raw in self.web_crawler.crawl_university(
                        dummy_source
                    ):
                        yield from self._extract_raw(
                            raw,
                            dummy_source,
                        )

                else:
                    logger.warning(
                        "Skipping news source '%s': "
                        "unsupported type '%s'",
                        source_name,
                        source_type,
                    )

            except Exception:
                logger.exception(
                    "Failed to process news source '%s'",
                    source_name,
                )

    def _stream_social(self) -> Generator[dict, None, None]:
        """Crawl and extract records from social media sources."""

        social_sources = self.sources.get("social_sources", [])

        logger.info(
            "Dispatching %d social sources",
            len(social_sources),
        )

        for source in social_sources:

            source_type = source.get("type")
            source_name = source.get("name", "unknown")

            try:

                if source_type == "twitter":

                    if not os.getenv("TWITTER_BEARER_TOKEN"):
                        logger.warning(
                            "Skipping Twitter source '%s': "
                            "TWITTER_BEARER_TOKEN is not set",
                            source_name,
                        )
                        continue

                    for raw in self.twitter_crawler.crawl_queries(
                        source
                    ):

                        tweet = raw.get("content")

                        if not tweet:
                            continue

                        record = extract_from_tweet(
                            tweet,
                            raw.get("search_query", ""),
                        )

                        if record and record.get("raw_text"):
                            yield record

                elif source_type == "reddit":

                    if not os.getenv("REDDIT_CLIENT_ID"):
                        logger.warning(
                            "Skipping Reddit source '%s': "
                            "REDDIT_CLIENT_ID is not set",
                            source_name,
                        )
                        continue

                    for raw in self.reddit_crawler.crawl_source(
                        source
                    ):

                        post = raw.get("content")

                        if not post:
                            continue

                        record = extract_from_reddit_post(
                            post,
                            raw.get("subreddit", ""),
                        )

                        if record and record.get("raw_text"):
                            yield record

                elif source_type == "linkedin":

                    logger.warning(
                        "LinkedIn source '%s' skipped. "
                        "Automated scraping is not supported. "
                        "Use the official LinkedIn API or "
                        "manual collection.",
                        source_name,
                    )

                else:
                    logger.warning(
                        "Skipping social source '%s': "
                        "unsupported type '%s'",
                        source_name,
                        source_type,
                    )

            except Exception:
                logger.exception(
                    "Failed to process social source '%s'",
                    source_name,
                )


    def _extract_raw(
        self,
        raw: dict,
        source: dict,
    ) -> list[dict]:
        """
        Route raw crawler output to the appropriate extractor.
        """

        if not raw:
            return []

        raw_type = raw.get("type")
        url = raw.get("url", "")
        content = raw.get("content", "")
        found_on = raw.get("found_on", "")

        source_name = source.get("name", "unknown")

        if not content:
            logger.debug(
                "Empty content for '%s' (%s)",
                url,
                raw_type,
            )
            return []

        try:

            if raw_type == "html":

                return extract_from_html(
                    content,
                    url,
                    source,
                    found_on_page=found_on,
                )

            # -------------------------
            # PDF
            # -------------------------
            if raw_type == "pdf":

                pdf_bytes = self._ensure_bytes(content)

                return extract_from_pdf(
                    pdf_bytes,
                    url,
                    source,
                    found_on_page=found_on,
                )


            if raw_type in {"xlsx", "xls"}:

                excel_bytes = self._ensure_bytes(content)

                return extract_from_xlsx(
                    excel_bytes,
                    url,
                    source,
                    found_on_page=found_on,
                )

            logger.warning(
                "Unknown raw type '%s' from source '%s', URL: %s",
                raw_type,
                source_name,
                url,
            )

            return []

        except Exception:
            logger.exception(
                "Extraction failed for source '%s', "
                "type '%s', URL: %s",
                source_name,
                raw_type,
                url,
            )
            return []


    @staticmethod
    def _ensure_bytes(content) -> bytes:
        """
        Convert crawler content to bytes safely.

        Binary content is returned unchanged.
        Text content is encoded as UTF-8.
        """

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