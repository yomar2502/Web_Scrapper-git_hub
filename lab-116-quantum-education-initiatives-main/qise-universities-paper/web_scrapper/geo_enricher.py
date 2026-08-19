"""Build country-level research metadata for the QISE candidate dataset.

Sources:
  * a locally installed QS rankings CSV;
  * the World Bank API, with value years and an on-disk cache;
  * explicitly versioned manual metadata.

Example:
    python geo_enricher.py --input data/qise_candidates.csv
    python geo_enricher.py --countries AR,BR,CL,PE --offline
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from setup_qs_csv import validate_qs_csv

logger = logging.getLogger("geo_enricher")

WORLD_BANK_API = "https://api.worldbank.org/v2"
DEFAULT_INPUT = Path("data/qise_candidates.csv")
DEFAULT_OUTPUT = Path("data/processed/geo_metadata.csv")
DEFAULT_QS_PATH = Path("data/qs_rankings.csv")
DEFAULT_CACHE_PATH = Path("data/processed/wb_cache.json")
DEFAULT_USER_AGENT = "QISE-LatAm-Research-Bot/2.0 (academic research)"
CACHE_VERSION = 2
MANUAL_DATA_REFERENCE_YEAR = 2024

# These values are not fetched automatically. They must be reviewed before a
# publication and are labelled with their reference year in every output row.
MANUAL_DATA = {
    "AR": {"times_top_rank": 601, "scimago_country_rank_physics": 32,
           "ibm_quantum_network_member": True, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": True},
    "BR": {"times_top_rank": 501, "scimago_country_rank_physics": 13,
           "ibm_quantum_network_member": True, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": True},
    "CL": {"times_top_rank": 601, "scimago_country_rank_physics": 39,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": True},
    "CO": {"times_top_rank": 801, "scimago_country_rank_physics": 47,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": True},
    "MX": {"times_top_rank": 601, "scimago_country_rank_physics": 26,
           "ibm_quantum_network_member": True, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": True},
    "PE": {"times_top_rank": 1001, "scimago_country_rank_physics": 62,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
    "VE": {"times_top_rank": 1001, "scimago_country_rank_physics": 71,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
    "UY": {"times_top_rank": 1001, "scimago_country_rank_physics": 58,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
    "CR": {"times_top_rank": 1001, "scimago_country_rank_physics": 78,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
    "EC": {"times_top_rank": 1001, "scimago_country_rank_physics": 85,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
    "CU": {"times_top_rank": 1001, "scimago_country_rank_physics": 90,
           "ibm_quantum_network_member": False, "national_quantum_initiative": False,
           "latam_quantum_alliance_member": False},
}

WB_INDICATORS = {
    "gdp_per_capita_usd": "NY.GDP.PCAP.CD",
    "rd_expenditure_pct_gdp": "GB.XPD.RSDV.GD.ZS",
    "internet_penetration_pct": "IT.NET.USER.ZS",
    "researchers_per_million": "SP.POP.SCIE.RD.P6",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _normalize_country_codes(values: Sequence[str]) -> tuple[list[str], list[str]]:
    valid: set[str] = set()
    invalid: set[str] = set()
    for value in values:
        code = (value or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", code):
            valid.add(code)
        elif code:
            invalid.add(code)
    return sorted(valid), sorted(invalid)


def load_config(path: Path) -> dict:
    """Load the project YAML config and reject malformed root structures."""
    if not path.is_file():
        raise FileNotFoundError(f"Config does not exist or is not a file: {path}")
    try:
        import yaml

        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except ModuleNotFoundError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not parse config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("Config root must be a YAML mapping")
    return config


def country_codes_from_dataset(path: Path) -> list[str]:
    """Read distinct ISO-2 country codes from a candidate CSV or JSON file."""
    if not path.is_file():
        raise FileNotFoundError(f"Candidate dataset does not exist: {path}")

    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if "country_code" not in (reader.fieldnames or []):
                raise ValueError("Candidate CSV has no 'country_code' column")
            raw_codes = [row.get("country_code", "") for row in reader]
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Candidate JSON must contain a list of rows")
        raw_codes = [
            row.get("country_code", "")
            for row in data
            if isinstance(row, dict)
        ]
    else:
        raise ValueError("Candidate dataset must be .csv or .json")

    valid, invalid = _normalize_country_codes(raw_codes)
    if invalid:
        logger.warning("Ignored invalid country codes in dataset: %s", invalid)
    if not valid:
        raise ValueError("Candidate dataset contains no valid ISO-2 country codes")
    return valid


class _OfflineSession:
    """Minimal session used when the CLI is explicitly cache-only."""

    def __init__(self):
        self.headers: dict[str, str] = {}

    def get(self, *_args, **_kwargs):
        raise RuntimeError("Network access is disabled in offline mode")

    def close(self) -> None:
        pass


class GeoEnricher:
    """Combine World Bank, QS and versioned manual data by country."""

    def __init__(
        self,
        cfg: dict | None = None,
        cache_path: str | Path = DEFAULT_CACHE_PATH,
        qs_path: str | Path = DEFAULT_QS_PATH,
        *,
        session: Any | None = None,
        cache_ttl_days: int = 30,
        offline: bool = False,
    ):
        if cache_ttl_days < 0:
            raise ValueError("cache_ttl_days must be >= 0")
        self.cfg = cfg or {}
        if not isinstance(self.cfg, dict):
            raise ValueError("cfg must be a mapping")
        scraper = self.cfg.get("scraper") or {}
        geo_cfg = self.cfg.get("geo_enrichment") or {}
        if not isinstance(scraper, dict) or not isinstance(geo_cfg, dict):
            raise ValueError("scraper and geo_enrichment config sections must be mappings")

        self.cache_path = Path(cache_path)
        self.qs_path = Path(qs_path)
        self.cache_ttl = timedelta(days=cache_ttl_days)
        self.offline = offline
        self.timeout = float(geo_cfg.get("request_timeout_sec", 15))
        self.request_delay = max(0.0, float(geo_cfg.get("request_delay_sec", 0.25)))
        self.max_retries = max(0, int(geo_cfg.get("max_retries", 2)))
        self._cache = self._load_cache()

        self.session = session or (_OfflineSession() if offline else self._create_session())
        if not hasattr(self.session, "headers"):
            self.session.headers = {}
        self.session.headers["User-Agent"] = scraper.get(
            "user_agent", DEFAULT_USER_AGENT
        )
        self.qs_by_country = self._load_qs_csv()

    @staticmethod
    def _create_session():
        import requests

        return requests.Session()

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "GeoEnricher":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def enrich_dataset(self, country_codes: Sequence[str]) -> dict[str, dict]:
        valid, invalid = _normalize_country_codes(country_codes)
        if invalid:
            logger.warning("Ignored invalid country codes: %s", invalid)
        return {code: self._get_country_data(code) for code in valid}

    def save_geo_metadata(
        self,
        country_codes: Sequence[str],
        output_path: str | Path,
    ) -> int:
        data = self.enrich_dataset(country_codes)
        if not data:
            raise ValueError("No valid countries were provided")

        output = Path(output_path)
        if output.suffix.lower() != ".csv":
            raise ValueError("Geo metadata output must be a .csv file")
        output.parent.mkdir(parents=True, exist_ok=True)

        all_fields = set().union(*(row.keys() for row in data.values()))
        fieldnames = ["country_code"] + sorted(all_fields - {"country_code"})
        temporary = output.with_name(f".{output.name}.tmp")
        try:
            with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for code, row in sorted(data.items()):
                    writer.writerow({"country_code": code, **row})
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()

        logger.info("Geo metadata saved: %s (%s countries)", output, len(data))
        return len(data)

    def _load_qs_csv(self) -> dict[str, dict]:
        if not self.qs_path.exists():
            logger.warning("QS CSV not found at %s; QS fields will be empty", self.qs_path)
            return {}
        try:
            report = validate_qs_csv(self.qs_path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not load QS CSV %s: %s", self.qs_path, exc)
            return {}

        by_country: dict[str, list[dict[str, Any]]] = {}
        for row in report.latin_american_rows:
            by_country.setdefault(str(row["code"]), []).append({
                "rank": int(row["rank"]),
                "name": str(row["name"]),
            })

        result: dict[str, dict] = {}
        for code, universities in by_country.items():
            ordered = sorted(universities, key=lambda item: (item["rank"], item["name"]))
            result[code] = {
                "qs_top_rank": ordered[0]["rank"],
                "qs_top_university": ordered[0]["name"],
                "qs_universities_in_ranking": len(ordered),
            }
        logger.info(
            "QS CSV loaded: %s countries, %s universities",
            len(result),
            sum(len(rows) for rows in by_country.values()),
        )
        return result

    def _get_country_data(self, country_code: str) -> dict:
        code = country_code.strip().upper()
        manual = MANUAL_DATA.get(code, {})
        result = {
            "country_code": code,
            **self._fetch_worldbank(code),
            **manual,
            **self.qs_by_country.get(code, {}),
        }
        if manual:
            result["manual_data_reference_year"] = MANUAL_DATA_REFERENCE_YEAR
        return result

    @staticmethod
    def _cache_record(value: Any) -> dict[str, Any]:
        if isinstance(value, dict) and "value" in value:
            return {
                "value": value.get("value"),
                "year": value.get("year"),
                "fetched_at": value.get("fetched_at"),
            }
        return {"value": value, "year": None, "fetched_at": None}

    def _is_cache_fresh(self, record: dict[str, Any]) -> bool:
        if self.offline:
            return True
        fetched_at = record.get("fetched_at")
        if not fetched_at or self.cache_ttl <= timedelta(0):
            return False
        try:
            timestamp = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return _utc_now() - timestamp <= self.cache_ttl

    def _fetch_worldbank(self, country_code: str) -> dict:
        result: dict[str, Any] = {}
        cache_changed = False
        indicators = list(WB_INDICATORS.items())

        for index, (field, indicator) in enumerate(indicators):
            cache_key = f"{country_code}_{indicator}"
            cached_raw = self._cache.get(cache_key)
            cached = self._cache_record(cached_raw) if cached_raw is not None else None

            if cached is not None and self._is_cache_fresh(cached):
                record = cached
            elif self.offline:
                record = {"value": None, "year": None, "fetched_at": None}
            else:
                value, year, request_succeeded = self._wb_api_call(
                    country_code, indicator
                )
                if value is None and cached is not None and cached.get("value") is not None:
                    logger.warning("Using stale cache for %s/%s", country_code, indicator)
                    record = cached
                elif not request_succeeded:
                    record = {"value": None, "year": None, "fetched_at": None}
                else:
                    record = {"value": value, "year": year, "fetched_at": _iso_now()}
                    self._cache[cache_key] = record
                    cache_changed = True
                if self.request_delay and index < len(indicators) - 1:
                    time.sleep(self.request_delay)

            result[field] = record.get("value")
            result[f"{field}_year"] = record.get("year")

        if cache_changed:
            self._save_cache()
        return result

    def _wb_api_call(
        self,
        country_code: str,
        indicator: str,
    ) -> tuple[float | None, str | None, bool]:
        url = f"{WORLD_BANK_API}/country/{country_code}/indicator/{indicator}"
        params = {"format": "json", "mrv": 10, "per_page": 10}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                entries = data[1] if isinstance(data, list) and len(data) > 1 else []
                if not isinstance(entries, list):
                    return None, None, True
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("value") is None:
                        continue
                    try:
                        value = Decimal(str(entry["value"])).quantize(
                            Decimal("0.0001"),
                            rounding=ROUND_HALF_UP,
                        )
                    except (InvalidOperation, ValueError):
                        continue
                    return float(value), str(entry.get("date") or "") or None, True
                return None, None, True
            except Exception as exc:
                if attempt >= self.max_retries:
                    logger.warning("World Bank API %s/%s: %s", country_code, indicator, exc)
                    break
                time.sleep(min(2 ** attempt, 4))
        return None, None, False

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid World Bank cache %s: %s", self.cache_path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("Ignoring World Bank cache with invalid root type")
            return {}
        if data.get("version") == CACHE_VERSION and isinstance(data.get("entries"), dict):
            return data["entries"]
        # Version 1 stored cache entries directly as key -> scalar.
        return data

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "updated_at": _iso_now(),
            "entries": self._cache,
        }
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(self.cache_path)
        except OSError as exc:
            logger.warning("Could not save World Bank cache: %s", exc)
        finally:
            if temporary.exists():
                temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--countries",
        help="Comma/space-separated ISO-2 codes; otherwise codes come from --input",
    )
    source.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Candidate CSV/JSON (default: {DEFAULT_INPUT})",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--qs-csv", type=Path, default=DEFAULT_QS_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--cache-ttl-days", type=int, default=30)
    parser.add_argument("--refresh", action="store_true", help="Ignore cache freshness")
    parser.add_argument("--offline", action="store_true", help="Use cache only; no API calls")
    return parser


def _configure_cli_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_cli_logging()

    try:
        if args.cache_ttl_days < 0:
            raise ValueError("--cache-ttl-days must be >= 0")
        config = load_config(args.config)
        if args.countries:
            raw_codes = re.split(r"[,;\s]+", args.countries)
            country_codes, invalid = _normalize_country_codes(raw_codes)
            if invalid:
                raise ValueError(f"Invalid ISO-2 country codes: {invalid}")
        else:
            country_codes = country_codes_from_dataset(args.input)

        with GeoEnricher(
            config,
            cache_path=args.cache,
            qs_path=args.qs_csv,
            cache_ttl_days=0 if args.refresh else args.cache_ttl_days,
            offline=args.offline,
        ) as enricher:
            count = enricher.save_geo_metadata(country_codes, args.output)
        print(f"Saved metadata for {count} countries to {args.output}")
        return 0
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency {exc.name or exc}. "
            "Run: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Geo enrichment failed: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Geo enrichment interrupted")
        return 130
    except Exception:
        logger.exception("Unexpected geo enrichment failure")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
