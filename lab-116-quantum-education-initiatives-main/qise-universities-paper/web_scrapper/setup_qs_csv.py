"""Validate and install a QS rankings CSV for ``geo_enricher.py``.

Examples:
    python setup_qs_csv.py path/to/qs_rankings.csv
    python setup_qs_csv.py path/to/qs_rankings.csv --force
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEST = Path("data/qs_rankings.csv")


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).strip()


_COUNTRY_ALIASES = {
    "Argentina": "AR",
    "Bolivia": "BO",
    "Bolivia (Plurinational State of)": "BO",
    "Brazil": "BR",
    "Brasil": "BR",
    "Chile": "CL",
    "Colombia": "CO",
    "Costa Rica": "CR",
    "Cuba": "CU",
    "Dominican Republic": "DO",
    "República Dominicana": "DO",
    "Ecuador": "EC",
    "El Salvador": "SV",
    "Guatemala": "GT",
    "Honduras": "HN",
    "Mexico": "MX",
    "México": "MX",
    "Nicaragua": "NI",
    "Panama": "PA",
    "Panamá": "PA",
    "Paraguay": "PY",
    "Peru": "PE",
    "Perú": "PE",
    "Puerto Rico": "PR",
    "Uruguay": "UY",
    "Venezuela": "VE",
    "Venezuela (Bolivarian Republic of)": "VE",
}
COUNTRY_TO_CODE = dict(_COUNTRY_ALIASES)
_COUNTRY_LOOKUP = {_fold(name): code for name, code in _COUNTRY_ALIASES.items()}
LATAM_CODES = frozenset(COUNTRY_TO_CODE.values())


@dataclass(frozen=True)
class QSValidation:
    headers: list[str]
    rows: list[dict[str, str]]
    name_column: str
    country_column: str
    rank_column: str | None
    encoding: str
    delimiter: str
    latin_american_rows: list[dict[str, str | int]]


def country_code_from_name(country: str) -> str:
    """Return a supported Latin American ISO-2 code, or an empty string."""
    raw = (country or "").strip()
    upper = raw.upper()
    if upper in LATAM_CODES:
        return upper
    return _COUNTRY_LOOKUP.get(_fold(raw), "")


def find_column(headers: list[str], candidates: Sequence[str]) -> str | None:
    """Find a column using normalized exact matches before substring matches."""
    normalized = {header: _fold(header) for header in headers if header}
    wanted = [_fold(candidate) for candidate in candidates]

    for candidate in candidates:
        for header in headers:
            if header and header.strip().casefold() == candidate.strip().casefold():
                return header
    for candidate in wanted:
        for header, key in normalized.items():
            if key == candidate:
                return header
    for candidate in wanted:
        for header, key in normalized.items():
            if candidate and candidate in key:
                return header
    return None


def parse_rank(value: object, fallback: int) -> int:
    """Parse ranks such as ``=123``, ``801-850`` or ``1,201+``."""
    text = str(value or "").strip().replace(",", "")
    match = re.search(r"\d+", text)
    return int(match.group()) if match else fallback


def _decode_csv(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode CSV: {path}")


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str, str]:
    """Read a CSV with common encodings and comma/semicolon/tab delimiters."""
    if not path.is_file():
        raise FileNotFoundError(f"CSV does not exist or is not a file: {path}")
    if path.suffix.lower() != ".csv":
        raise ValueError(f"Expected a .csv file: {path}")

    text, encoding = _decode_csv(path)
    if not text.strip():
        raise ValueError("CSV is empty")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        dialect = csv.excel
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [(header or "").strip() for header in (reader.fieldnames or [])]
    if not headers or not any(headers):
        raise ValueError("CSV has no header row")
    reader.fieldnames = headers
    rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("CSV has a header but no data rows")
    return headers, rows, encoding, delimiter


def validate_qs_csv(path: Path) -> QSValidation:
    """Validate required columns and identify usable Latin American rows."""
    headers, rows, encoding, delimiter = read_csv_rows(path)
    name_col = find_column(
        headers,
        ("institution name", "university name", "institution", "university", "name"),
    )
    country_col = find_column(
        headers,
        ("country/territory", "country territory", "country", "territory", "pais"),
    )
    rank_col = find_column(headers, ("rank", "ranking", "position", "#"))

    if not name_col or not country_col:
        raise ValueError(
            "CSV needs institution/university and country/territory columns; "
            f"found: {headers}"
        )

    latin_rows: list[dict[str, str | int]] = []
    seen: set[tuple[str, str]] = set()
    for position, row in enumerate(rows, start=1):
        name = (row.get(name_col) or "").strip()
        country = (row.get(country_col) or "").strip()
        code = country_code_from_name(country)
        key = (code, _fold(name))
        if not name or not code or key in seen:
            continue
        seen.add(key)
        latin_rows.append({
            "row": position + 1,
            "rank": parse_rank(row.get(rank_col), position) if rank_col else position,
            "name": name,
            "country": country,
            "code": code,
        })

    if not latin_rows:
        raise ValueError("CSV contains no recognized Latin American universities")

    return QSValidation(
        headers=headers,
        rows=rows,
        name_column=name_col,
        country_column=country_col,
        rank_column=rank_col,
        encoding=encoding,
        delimiter=delimiter,
        latin_american_rows=latin_rows,
    )


def install_qs_csv(source: Path, destination: Path, force: bool = False) -> QSValidation:
    """Validate ``source`` and atomically copy it to ``destination``."""
    report = validate_qs_csv(source)
    source_resolved = source.resolve(strict=True)
    destination_resolved = destination.resolve(strict=False)

    if source_resolved == destination_resolved:
        return report
    if destination.exists() and not force:
        raise FileExistsError(
            f"Destination already exists: {destination}. Use --force to replace it."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="QS rankings CSV to validate")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Installation path (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing destination after validation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = install_qs_csv(args.source, args.output, force=args.force)
    except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    countries = sorted({str(row["code"]) for row in report.latin_american_rows})
    print(f"Columns: {report.headers}")
    print(f"Encoding/delimiter: {report.encoding} / {report.delimiter!r}")
    print(
        f"Latin American universities: {len(report.latin_american_rows)} "
        f"across {len(countries)} countries ({', '.join(countries)})"
    )
    for row in report.latin_american_rows[:10]:
        print(f"  [{row['rank']:>4}] {row['code']} — {row['name']}")
    print(f"Installed QS CSV: {args.output}")
    print("Next: python geo_enricher.py --input data/qise_candidates.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
