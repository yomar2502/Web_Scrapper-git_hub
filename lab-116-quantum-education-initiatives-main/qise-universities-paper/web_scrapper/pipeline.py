"""
pipeline.py — Institution-level orchestration.

Flow:
    input (CSV/YAML)  ─┐
    config.yaml       ─┴─► Dispatcher (WebCrawler → HTML/PDF)
                             │
                             ▼
                         Extractor  →  evidence fragments (HTML pages, PDF pages)
                             │
                             ▼
                         QISEClassifier  →  candidate rows (4-category, auditable)
                             │
                             ▼
                    dedupe (source_url × semantic_category)
                             │
                             ▼
              qise_candidates.csv  +  .json  +  run_summary.json
                             │
                             ▼
              analytical_dataset.json  +  university_summary.csv  ← NUEVO
"""

import csv
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field

import yaml

from dispatcher import Dispatcher
from keywords import fold
from qise_classifier import QISEClassifier
from input_loader import load_universities
from utils import get_logger, truncate_text, now_iso, normalize_url

logger = get_logger("pipeline")

# ── CONSTANTS ──────────────────────────────────────────────────────────────────

OUTPUT_FIELDS = [
    "timestamp",
    "institution",
    "country",
    "country_code",
    "classification",
    "confidence",
    "is_qise_core",
    "academic_level",
    "semantic_category",
    "keyword_tier",
    "matched_keyword",
    "matched_keywords",
    "course_title",
    "evidence_snippet",
    "source_type",
    "media_type",
    "source_url",
    "pdf_url",
    "pdf_page",
    "found_on_page",
    "seed_origin",
    "extraction_status",
    "language",
]

_CONFIG_KEY_ALIASES = {
    "max_pages_per_domain": "max_pages_per_university",
    "request_delay_seconds": "request_delay_sec",
    "timeout_seconds": "request_timeout_sec",
    "respect_robots_txt": "respect_robots",
}

_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "": 0, None: 0}

_CLASS_ORDER = {
    "qise_core": 0,
    "quantum_foundations_or_adjacent": 1,
    "unclear": 2,
    "non_course_or_contextual": 3,
}

_COURSE_CODE_PREFIX = re.compile(r"^[a-z]{1,4}[- ]?\d{2,4}[a-z]?\b[\s.:–—-]*")


# ── DATA CLASSES ──────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    """Pipeline configuration."""
    scraper: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    classifier: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        return cls(
            scraper=data.get("scraper", {}),
            output=data.get("output", {}),
            classifier=data.get("classifier", {}),
        )


@dataclass
class SummaryStats:
    """Pipeline execution statistics."""
    run_timestamp: str
    elapsed_seconds: float
    seeds_discovered: int = 0
    pages_crawled: int = 0
    pdfs_detected: int = 0
    pdf_documents_processed: int = 0
    pdf_documents_extracted: int = 0
    fragments_processed: int = 0
    candidate_rows: int = 0
    rows_from_pdf: int = 0
    rows_needing_manual_review: int = 0
    by_classification: Dict[str, int] = field(default_factory=dict)
    by_confidence: Dict[str, int] = field(default_factory=dict)
    by_seed_origin: Dict[str, int] = field(default_factory=dict)
    qise_core_rows: int = 0
    institutions_with_qise_core: List[str] = field(default_factory=list)
    countries_with_qise_core: List[str] = field(default_factory=list)
    by_country: Dict[str, Dict] = field(default_factory=dict)
    crawl_stats_per_institution: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_timestamp": self.run_timestamp,
            "elapsed_seconds": self.elapsed_seconds,
            "seeds_discovered": self.seeds_discovered,
            "pages_crawled": self.pages_crawled,
            "pdfs_detected": self.pdfs_detected,
            "pdf_documents_processed": self.pdf_documents_processed,
            "pdf_documents_extracted": self.pdf_documents_extracted,
            "fragments_processed": self.fragments_processed,
            "candidate_rows": self.candidate_rows,
            "rows_from_pdf": self.rows_from_pdf,
            "rows_needing_manual_review": self.rows_needing_manual_review,
            "by_classification": self.by_classification,
            "by_confidence": self.by_confidence,
            "by_seed_origin": self.by_seed_origin,
            "qise_core_rows": self.qise_core_rows,
            "institutions_with_qise_core": self.institutions_with_qise_core,
            "countries_with_qise_core": self.countries_with_qise_core,
            "by_country": self.by_country,
            "crawl_stats_per_institution": self.crawl_stats_per_institution,
        }


# ── HELPER FUNCTIONS ──────────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    """Generate a deduplication key from a course title."""
    t = _COURSE_CODE_PREFIX.sub("", fold(title or ""))
    return re.sub(r"\s+", " ", t).strip()


def _filter_by_country(universities: List[Dict], country: Optional[str]) -> List[Dict]:
    """Filter universities by country."""
    if not country:
        return universities
    
    c = country.strip().lower()
    return [
        u for u in universities
        if c in (u.get("country", "").lower(), u.get("country_code", "").lower())
    ]


def _get_confidence_rank(confidence: str) -> int:
    """Get numeric rank for confidence level."""
    return _CONF_RANK.get(confidence, 0)


# ── PIPELINE ──────────────────────────────────────────────────────────────────

class Pipeline:
    """Main pipeline orchestrator for QISE-LatAm scraping."""

    def __init__(
        self,
        config_path: str = "config.yaml",
        input_path: Optional[str] = None,
        sources_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the pipeline."""
        logger.info("=" * 60)
        logger.info("QISE-LatAm-Scraper pipeline starting")
        logger.info("=" * 60)

        self.cfg = self._load_config(config_path)
        self._apply_overrides(overrides or {})
        self._ensure_output_dirs()
        self.universities = self._load_universities(input_path, sources_path)
        self.classifier = QISEClassifier(self.cfg)
        self._sources_path = sources_path

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load and normalize configuration."""
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        
        cfg.setdefault("scraper", {})
        cfg.setdefault("output", {})
        
        sc = cfg["scraper"]
        for alias, canonical in _CONFIG_KEY_ALIASES.items():
            if alias in sc and canonical not in sc:
                sc[canonical] = sc[alias]
        
        return cfg

    def _apply_overrides(self, overrides: Dict[str, Any]) -> None:
        """Apply configuration overrides."""
        sc = self.cfg["scraper"]
        override_keys = [
            "max_depth", "max_pages_per_university", "download_pdfs",
            "use_cache", "respect_robots", "request_delay_sec",
            "auto_discover_seeds", "min_seed_score", "max_auto_seeds_per_institution"
        ]
        
        for key in override_keys:
            if overrides.get(key) is not None:
                sc[key] = overrides[key]

    def _ensure_output_dirs(self) -> None:
        """Create necessary output directories."""
        output = self.cfg["output"]
        for dkey in ("raw_dir", "processed_dir", "log_dir"):
            d = output.get(dkey)
            if d:
                Path(d).mkdir(parents=True, exist_ok=True)

    def _load_universities(
        self,
        input_path: Optional[str],
        sources_path: Optional[str]
    ) -> List[Dict]:
        """Load universities from input."""
        if input_path:
            return load_universities(input_path)
        elif sources_path and Path(sources_path).exists():
            return load_universities(sources_path)
        else:
            raise FileNotFoundError(
                "No input provided. Pass --input <file.csv|yaml> or keep sources.yaml."
            )

    # ── MAIN ENTRY POINT ──────────────────────────────────────────────────────

    def run(
        self,
        output_path: str,
        dry_run: bool = False,
        limit: Optional[int] = None,
        country: Optional[str] = None,
        resume: bool = False,
        include_news: bool = False,
        include_social: bool = False,
        force_discover: bool = False,
    ) -> SummaryStats:
        """Run the complete pipeline."""
        start = time.time()
        ts = now_iso()

        universities = _filter_by_country(self.universities, country)
        logger.info(
            f"Universities to process: {len(universities)}"
            + (f" (country={country})" if country else "")
        )

        existing_rows, universities = self._handle_resume(
            output_path, resume, universities
        )

        seeds_discovered = self._resolve_seeds(
            universities,
            dry_run=dry_run,
            force=force_discover
        )

        sources = self._build_sources(universities, include_news, include_social)
        dispatcher = Dispatcher(self.cfg, sources)
        
        best, fragments_seen, pdf_stats = self._process_fragments(
            dispatcher, universities, limit, ts
        )

        new_rows = list(best.values())
        all_rows = self._merge(existing_rows, new_rows)
        
        logger.info(
            f"Phase 1/2 complete — {len(new_rows)} new candidate rows "
            f"({len(all_rows)} total)"
        )

        logger.info("Phase 2/2 — writing output...")
        if not dry_run:
            output_path_obj = Path(output_path)
            
            # ── 1. CSV principal ──────────────────────────────────────────────
            self._write_csv(output_path_obj, all_rows)
            
            # ── 2. JSON principal ─────────────────────────────────────────────
            self._write_json(output_path_obj.with_suffix(".json"), all_rows)
            
            # ── 3. NUEVO: Dataset analítico ──────────────────────────────────
            analytical = self._build_analytical_dataset(all_rows)
            analytical_path = output_path_obj.with_suffix(".analytical.json")
            analytical_path.write_text(
                json.dumps(analytical, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            logger.info(f"Analytical dataset saved → {analytical_path}")
            
            # ── 4. NUEVO: Resumen por universidad ────────────────────────────
            uni_summary_path = output_path_obj.parent / "university_summary.csv"
            self._write_university_summary(uni_summary_path, analytical["summary_by_university"])
            
            # ── 5. NUEVO: Resumen por país ────────────────────────────────────
            country_summary_path = output_path_obj.parent / "country_summary.csv"
            self._write_country_summary(country_summary_path, analytical["summary_by_country"])

        elapsed = round(time.time() - start, 1)
        summary = self._build_summary(
            all_rows,
            elapsed,
            fragments_seen,
            dispatcher,
            seeds_discovered,
            pdf_stats,
        )
        
        self._print_summary(summary)
        
        if not dry_run:
            self._save_summary(summary)

        return summary

    # ── NUEVO: DATASET ANALÍTICO ─────────────────────────────────────────────

    def _build_analytical_dataset(self, rows: List[Dict]) -> Dict[str, Any]:
        """
        Build the final analytical dataset for the paper.
        
        Returns:
            Dict with:
                - summary_by_university: {university: {qise_core, adjacent, unclear, non_course, total, country, country_code}}
                - summary_by_country: {country_code: {qise_core, adjacent, unclear, non_course, total, universities}}
                - unique_qise_courses: List of unique course titles with QISE
                - qise_core_count: Total QISE courses
                - classification_stats: {classification: count}
                - confidence_stats: {confidence: count}
                - total_rows: Total rows
                - universities_with_qise: Number of universities with QISE
                - countries_with_qise: Number of countries with QISE
        """
        
        # ── 1. Resumen por universidad ──────────────────────────────────────
        by_university: Dict[str, Dict] = {}
        for r in rows:
            name = r.get("institution", "unknown")
            if name not in by_university:
                by_university[name] = {
                    "qise_core": 0,
                    "adjacent": 0,
                    "unclear": 0,
                    "non_course": 0,
                    "total": 0,
                    "country": r.get("country", ""),
                    "country_code": r.get("country_code", ""),
                }
            cls = r.get("classification", "unclear")
            if cls == "qise_core":
                by_university[name]["qise_core"] += 1
            elif cls == "quantum_foundations_or_adjacent":
                by_university[name]["adjacent"] += 1
            elif cls == "unclear":
                by_university[name]["unclear"] += 1
            else:
                by_university[name]["non_course"] += 1
            by_university[name]["total"] += 1

        # ── 2. Resumen por país ─────────────────────────────────────────────
        by_country: Dict[str, Dict] = {}
        for uni, stats in by_university.items():
            code = stats.get("country_code", "??")
            if code not in by_country:
                by_country[code] = {
                    "qise_core": 0,
                    "adjacent": 0,
                    "unclear": 0,
                    "non_course": 0,
                    "total": 0,
                    "universities": [],
                    "country": stats.get("country", ""),
                }
            by_country[code]["qise_core"] += stats["qise_core"]
            by_country[code]["adjacent"] += stats["adjacent"]
            by_country[code]["unclear"] += stats["unclear"]
            by_country[code]["non_course"] += stats["non_course"]
            by_country[code]["total"] += stats["total"]
            by_country[code]["universities"].append(uni)

        # ── 3. Cursos únicos QISE ───────────────────────────────────────────
        unique_qise = {}
        for r in rows:
            if r.get("classification") == "qise_core":
                title = r.get("course_title", "")
                if title and title not in unique_qise:
                    unique_qise[title] = {
                        "course": title,
                        "institution": r.get("institution", ""),
                        "country": r.get("country", ""),
                        "semantic_category": r.get("semantic_category", ""),
                        "source_url": r.get("source_url", ""),
                    }

        # ── 4. Estadísticas de clasificación ────────────────────────────────
        classification_stats = {}
        confidence_stats = {}
        for r in rows:
            cls = r.get("classification", "unclear")
            classification_stats[cls] = classification_stats.get(cls, 0) + 1
            
            conf = r.get("confidence", "low")
            confidence_stats[conf] = confidence_stats.get(conf, 0) + 1

        return {
            "summary_by_university": by_university,
            "summary_by_country": by_country,
            "unique_qise_courses": list(unique_qise.values()),
            "qise_core_count": len(unique_qise),
            "classification_stats": classification_stats,
            "confidence_stats": confidence_stats,
            "total_rows": len(rows),
            "universities_with_qise": len([u for u, s in by_university.items() if s["qise_core"] > 0]),
            "countries_with_qise": len([c for c, s in by_country.items() if s["qise_core"] > 0]),
        }

    # ── NUEVO: GUARDAR RESUMEN POR UNIVERSIDAD ──────────────────────────────

    def _write_university_summary(self, path: Path, summary: Dict[str, Dict]) -> None:
        """Write university summary to CSV."""
        fieldnames = ["university", "country", "country_code", "qise_core", "adjacent", "unclear", "non_course", "total"]
        
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for name, stats in sorted(summary.items(), key=lambda x: -x[1]["qise_core"]):
                w.writerow({
                    "university": name,
                    "country": stats.get("country", ""),
                    "country_code": stats.get("country_code", ""),
                    "qise_core": stats["qise_core"],
                    "adjacent": stats["adjacent"],
                    "unclear": stats["unclear"],
                    "non_course": stats["non_course"],
                    "total": stats["total"],
                })
        logger.info(f"University summary written → {path}")

    # ── NUEVO: GUARDAR RESUMEN POR PAÍS ─────────────────────────────────────

    def _write_country_summary(self, path: Path, summary: Dict[str, Dict]) -> None:
        """Write country summary to CSV."""
        fieldnames = ["country_code", "country", "qise_core", "adjacent", "unclear", "non_course", "total", "universities"]
        
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for code, stats in sorted(summary.items(), key=lambda x: -x[1]["qise_core"]):
                w.writerow({
                    "country_code": code,
                    "country": stats.get("country", ""),
                    "qise_core": stats["qise_core"],
                    "adjacent": stats["adjacent"],
                    "unclear": stats["unclear"],
                    "non_course": stats["non_course"],
                    "total": stats["total"],
                    "universities": " | ".join(sorted(stats["universities"])),
                })
        logger.info(f"Country summary written → {path}")

    # ── STAGE 1: SEED RESOLUTION ────────────────────────────────────────────

    def _resolve_seeds(
        self,
        universities: List[Dict],
        dry_run: bool = False,
        force: bool = False
    ) -> int:
        """Resolve seeds for each university."""
        for u in universities:
            u["seed_origin"] = (
                "manual" if u.get("has_manual_seeds")
                else "homepage_crawl"
            )

        auto = self.cfg["scraper"].get("auto_discover_seeds", True)
        targets = [
            u for u in universities
            if force or (not u.get("has_manual_seeds") and auto)
        ]

        if not targets:
            if any(not u.get("has_manual_seeds") for u in universities):
                logger.info("Seed discovery disabled — crawling from homepage")
            return 0

        from seed_discovery import SeedDiscoverer
        
        discoverer = SeedDiscoverer(self.cfg)
        all_candidates: List[Dict] = []
        
        for u in targets:
            found = discoverer.discover(u)
            all_candidates.extend(found)
            
            if found:
                seeds = [f["seed_url"] for f in found]
                base = normalize_url(u.get("base_url") or "")
                
                u["catalog_urls"] = seeds + (
                    [base] if base and base not in seeds else []
                )
                u["seed_origin"] = "auto_discovered"
            elif not u.get("has_manual_seeds"):
                u["seed_origin"] = "homepage_crawl"

        if not dry_run:
            self._write_discovered_seeds(all_candidates)

        origins = {}
        for u in universities:
            origin = u["seed_origin"]
            origins[origin] = origins.get(origin, 0) + 1
        
        logger.info(f"Seed resolution: {origins} | {len(all_candidates)} seeds discovered")
        return len(all_candidates)

    def _write_discovered_seeds(self, candidates: List[Dict]) -> None:
        """Write discovered seeds to CSV."""
        if not candidates:
            return

        proc_dir = Path(self.cfg["output"].get("processed_dir", "data/processed"))
        proc_dir.mkdir(parents=True, exist_ok=True)
        
        path = proc_dir / "discovered_seeds.csv"
        fields = ["institution", "seed_url", "source", "score", "matched_terms", "reason"]
        
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(candidates)
        
        logger.info(f"Discovered seeds written → {path} ({len(candidates)} rows)")

    # ── STAGE 2: FRAGMENT PROCESSING ───────────────────────────────────────

    def _process_fragments(
        self,
        dispatcher: Dispatcher,
        universities: List[Dict],
        limit: Optional[int],
        timestamp: str,
    ) -> Tuple[Dict[Tuple, Dict], int, Dict[str, int]]:
        """Process fragments from the dispatcher."""
        best: Dict[Tuple, Dict] = {}
        fragments_seen = 0
        pdf_docs_seen: Set[str] = set()
        pdf_docs_extracted: Set[str] = set()
        pdf_url_to_country: Dict[str, str] = {}

        logger.info("Phase 1/2 — crawling, extracting, classifying...")

        for fragment in dispatcher.stream_all_records():
            fragments_seen += 1
            
            if fragment.get("media_type") == "pdf":
                url = fragment.get("source_url", "")
                pdf_docs_seen.add(url)
                if fragment.get("extraction_status") == "extracted":
                    pdf_docs_extracted.add(url)
                
                country = fragment.get("country") or fragment.get("university_country", "")
                if country:
                    pdf_url_to_country[url] = country

            if limit and fragments_seen > limit:
                logger.info(f"Fragment limit reached ({limit}). Stopping.")
                break

            for cand in self.classifier.classify(fragment):
                row = self._to_row(cand, timestamp)
                
                key = (
                    row["source_url"],
                    row["semantic_category"],
                    _title_key(row.get("course_title", ""))
                )
                
                prev = best.get(key)
                if prev is None or _get_confidence_rank(row["confidence"]) > _get_confidence_rank(prev["confidence"]):
                    best[key] = row

            if fragments_seen % 100 == 0:
                logger.info(f"  {fragments_seen} fragments | {len(best)} candidate rows")

        return best, fragments_seen, {
            "pdf_docs_seen": len(pdf_docs_seen),
            "pdf_docs_extracted": len(pdf_docs_extracted),
            "pdf_url_to_country": pdf_url_to_country,
        }

    # ── HELPERS ──────────────────────────────────────────────────────────────

    def _handle_resume(
        self,
        output_path: str,
        resume: bool,
        universities: List[Dict]
    ) -> Tuple[List[Dict], List[Dict]]:
        """Handle resume logic."""
        existing_rows = []
        out_path = Path(output_path)
        
        if resume and out_path.exists():
            existing_rows = self._read_existing(out_path)
            done_institutions = {r.get("institution", "") for r in existing_rows}
            
            remaining = [
                u for u in universities
                if u["name"] not in done_institutions
            ]
            
            logger.info(
                f"Resume: {len(done_institutions)} institutions already done, "
                f"{len(remaining)} remaining"
            )
            return existing_rows, remaining
        
        return existing_rows, universities

    def _build_sources(
        self,
        universities: List[Dict],
        include_news: bool,
        include_social: bool
    ) -> Dict[str, List]:
        """Build sources configuration for dispatcher."""
        sources = {"universities": universities}
        
        if (include_news or include_social) and self._sources_path:
            if Path(self._sources_path).exists():
                try:
                    with open(self._sources_path, encoding="utf-8") as f:
                        extra = yaml.safe_load(f) or {}
                    
                    if include_news:
                        sources["news_sources"] = extra.get("news_sources", [])
                    if include_social:
                        sources["social_sources"] = extra.get("social_sources", [])
                        
                except Exception as e:
                    logger.warning(f"Could not load extra sources: {e}")
        
        return sources

    @staticmethod
    def _to_row(cand: Dict, ts: str) -> Dict:
        """Convert a candidate to an output row."""
        classification = cand.get("classification", "unclear")
        pdf_page = cand.get("pdf_page")
        
        return {
            "timestamp": ts,
            "institution": cand.get("university", ""),
            "country": cand.get("country", ""),
            "country_code": cand.get("country_code", ""),
            "classification": classification,
            "confidence": cand.get("confidence", "low"),
            "is_qise_core": classification == "qise_core",
            "academic_level": cand.get("academic_level") or "unknown",
            "semantic_category": cand.get("semantic_category", ""),
            "keyword_tier": cand.get("keyword_tier", ""),
            "matched_keyword": cand.get("matched_keyword", ""),
            "matched_keywords": "|".join(cand.get("matched_keywords", []) or []),
            "course_title": (cand.get("course_title") or "")[:200],
            "evidence_snippet": truncate_text(cand.get("evidence_snippet", ""), 400),
            "source_type": cand.get("source_type", ""),
            "media_type": cand.get("media_type", ""),
            "source_url": cand.get("source_url", ""),
            "pdf_url": cand.get("pdf_url", ""),
            "pdf_page": pdf_page if pdf_page is not None else "",
            "found_on_page": cand.get("found_on_page", ""),
            "seed_origin": cand.get("seed_origin", ""),
            "extraction_status": cand.get("extraction_status", "extracted"),
            "language": cand.get("language", ""),
        }

    @staticmethod
    def _merge(existing: List[Dict], new: List[Dict]) -> List[Dict]:
        """Merge existing and new rows with deduplication."""
        by_key: Dict[Tuple, Dict] = {}
        
        for r in existing + new:
            key = (
                r.get("source_url", ""),
                r.get("semantic_category", ""),
                _title_key(r.get("course_title", ""))
            )
            
            prev = by_key.get(key)
            if prev is None or _get_confidence_rank(r.get("confidence")) >= _get_confidence_rank(prev.get("confidence")):
                by_key[key] = r
        
        return list(by_key.values())

    @staticmethod
    def _read_existing(path: Path) -> List[Dict]:
        """Read existing output CSV for resume."""
        try:
            with open(path, encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            logger.warning(f"Could not read existing output for resume: {e}")
            return []

    # ── OUTPUT ───────────────────────────────────────────────────────────────

    def _write_csv(self, path: Path, rows: List[Dict]) -> None:
        """Write CSV output."""
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        
        logger.info(f"Wrote {len(rows)} rows → {path}")

    @staticmethod
    def _write_json(path: Path, rows: List[Dict]) -> None:
        """Write JSON output."""
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def _save_summary(self, summary: SummaryStats) -> None:
        """Save summary to JSON."""
        proc_dir = Path(self.cfg["output"].get("processed_dir", "data/processed"))
        proc_dir.mkdir(parents=True, exist_ok=True)
        
        summary_path = proc_dir / "run_summary.json"
        summary_path.write_text(
            json.dumps(summary.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    # ── SUMMARY ─────────────────────────────────────────────────────────────

    def _build_summary(
        self,
        rows: List[Dict],
        elapsed: float,
        fragments_seen: int,
        dispatcher: Dispatcher,
        seeds_discovered: int,
        pdf_stats: Dict[str, Any],
    ) -> SummaryStats:
        """Build summary statistics."""
        by_class: Dict[str, int] = {}
        by_conf: Dict[str, int] = {}
        
        for r in rows:
            cls = r.get("classification", "unclear")
            by_class[cls] = by_class.get(cls, 0) + 1
            
            conf = r.get("confidence", "unknown")
            by_conf[conf] = by_conf.get(conf, 0) + 1

        qise_core = [r for r in rows if r.get("classification") == "qise_core"]
        institutions_with_core = sorted({
            r["institution"] for r in qise_core
            if r.get("institution")
        })
        countries_with_core = sorted({
            r["country_code"] for r in qise_core
            if r.get("country_code")
        })

        by_country: Dict[str, Dict] = {}
        for r in rows:
            code = r.get("country_code") or "??"
            b = by_country.setdefault(code, {
                "rows": 0,
                "qise_core": 0,
                "institutions": set(),
                "institutions_with_core": set()
            })
            b["rows"] += 1
            if r.get("institution"):
                b["institutions"].add(r["institution"])
            if r.get("classification") == "qise_core":
                b["qise_core"] += 1
                if r.get("institution"):
                    b["institutions_with_core"].add(r["institution"])

        for code, b in by_country.items():
            b["institutions"] = len(b["institutions"])
            b["institutions_with_core"] = sorted(b["institutions_with_core"])

        pdf_rows = sum(1 for r in rows if r.get("media_type") == "pdf")
        manual_review = sum(
            1 for r in rows
            if r.get("extraction_status") != "extracted"
        )

        by_seed_origin: Dict[str, int] = {}
        for r in rows:
            origin = r.get("seed_origin") or ""
            by_seed_origin[origin] = by_seed_origin.get(origin, 0) + 1

        crawl_stats = dispatcher.web_crawler.stats if hasattr(dispatcher, "web_crawler") else {}
        pages_crawled = sum(s.get("pages_crawled", 0) for s in crawl_stats.values())
        pdfs_detected = sum(s.get("pdfs_detected", 0) for s in crawl_stats.values())

        return SummaryStats(
            run_timestamp=now_iso(),
            elapsed_seconds=elapsed,
            seeds_discovered=seeds_discovered,
            pages_crawled=pages_crawled,
            pdfs_detected=pdfs_detected,
            pdf_documents_processed=pdf_stats.get("pdf_docs_seen", 0),
            pdf_documents_extracted=pdf_stats.get("pdf_docs_extracted", 0),
            fragments_processed=fragments_seen,
            candidate_rows=len(rows),
            rows_from_pdf=pdf_rows,
            rows_needing_manual_review=manual_review,
            by_classification=by_class,
            by_confidence=by_conf,
            by_seed_origin=by_seed_origin,
            qise_core_rows=len(qise_core),
            institutions_with_qise_core=institutions_with_core,
            countries_with_qise_core=countries_with_core,
            by_country=by_country,
            crawl_stats_per_institution=crawl_stats,
        )

    @staticmethod
    def _print_summary(summary: SummaryStats) -> None:
        """Print summary to console."""
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE")
        logger.info(f"  Seeds discovered    : {summary.seeds_discovered}")
        logger.info(f"  Pages crawled       : {summary.pages_crawled}")
        logger.info(f"  PDFs detected       : {summary.pdfs_detected}")
        logger.info(f"  PDFs extracted OK   : {summary.pdf_documents_extracted}/{summary.pdf_documents_processed}")
        logger.info(f"  Fragments processed : {summary.fragments_processed}")
        logger.info(f"  Candidate rows      : {summary.candidate_rows}")
        logger.info(f"  From PDFs           : {summary.rows_from_pdf}")
        logger.info(f"  Need manual review  : {summary.rows_needing_manual_review}")
        logger.info(f"  By seed origin      : {summary.by_seed_origin}")
        logger.info(f"  Elapsed             : {summary.elapsed_seconds}s")
        logger.info("-" * 60)
        logger.info("  By classification:")
        for k, v in sorted(summary.by_classification.items()):
            logger.info(f"    {k:<34s}: {v}")
        logger.info("-" * 60)
        logger.info(f"  qise_core rows      : {summary.qise_core_rows}")
        logger.info(f"  Institutions w/ core: {len(summary.institutions_with_qise_core)}")
        for name in summary.institutions_with_qise_core[:10]:
            logger.info(f"      • {name}")
        if len(summary.institutions_with_qise_core) > 10:
            logger.info(f"      ... and {len(summary.institutions_with_qise_core) - 10} more")
        logger.info("=" * 60)

    # ── DISCOVER-ONLY MODE ──────────────────────────────────────────────────

    def discover_only(self, country: Optional[str] = None, force: bool = False) -> int:
        """Run seed discovery only, without crawling."""
        from seed_discovery import SeedDiscoverer
        
        universities = _filter_by_country(self.universities, country)
        discoverer = SeedDiscoverer(self.cfg)
        all_candidates: List[Dict] = []

        for u in universities:
            has_manual = u.get("has_manual_seeds", False)
            
            if has_manual and not force:
                logger.info(
                    f"{u['name']}: manual seeds present "
                    f"({len(u.get('catalog_urls') or [])}) — skipping "
                    f"(use --force-discover to override)"
                )
                continue

            candidates = discoverer.discover(u)
            all_candidates.extend(candidates)

        self._write_discovered_seeds(all_candidates)

        institutions = len({c["institution"] for c in all_candidates})
        logger.info(
            f"Seed discovery complete: {len(all_candidates)} seeds "
            f"across {institutions} institution(s)"
        )

        return len(all_candidates)


# ── CONVENIENCE FUNCTIONS ──────────────────────────────────────────────────

def run_pipeline(
    config_path: str = "config.yaml",
    input_path: Optional[str] = None,
    output_path: str = "data/processed/qise_candidates.csv",
    **kwargs
) -> SummaryStats:
    """Convenience function to run the pipeline."""
    pipeline = Pipeline(config_path, input_path)
    return pipeline.run(output_path, **kwargs)