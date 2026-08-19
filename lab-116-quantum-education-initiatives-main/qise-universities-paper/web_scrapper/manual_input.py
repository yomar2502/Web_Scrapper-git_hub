"""Add auditable manual course evidence to the scraper dataset.

Use this only when an official university page is inaccessible to the crawler.
Every saved row still requires an official source URL and passes through the
same classifier and output schema as crawled evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections.abc import Sequence
from pathlib import Path

from pipeline import OUTPUT_FIELDS, Pipeline, _CONF_RANK
from qise_classifier import QISEClassifier
from utils import get_logger, normalize_url, now_iso

logger = get_logger("manual_input")

DEFAULT_DATASET_PATH = Path("data/qise_candidates.csv")
ACADEMIC_LEVELS = ("undergraduate", "graduate", "unknown")
_LEVEL_ALIASES = {
    "undergrad": "undergraduate",
    "bachelor": "undergraduate",
    "pregrado": "undergraduate",
    "masters": "graduate",
    "master": "graduate",
    "phd": "graduate",
    "doctorate": "graduate",
    "posgrado": "graduate",
    "postgrado": "graduate",
    "": "unknown",
}
_CLASS_ORDER = {
    "qise_core": 0,
    "quantum_foundations_or_adjacent": 1,
    "unclear": 2,
    "non_course_or_contextual": 3,
}


def normalize_academic_level(value: str) -> str:
    normalized = (value or "").strip().casefold()
    normalized = _LEVEL_ALIASES.get(normalized, normalized)
    if normalized not in ACADEMIC_LEVELS:
        expected = ", ".join(ACADEMIC_LEVELS)
        raise ValueError(f"Invalid academic level {value!r}; use: {expected}")
    return normalized


def build_fragment(
    *,
    university: str,
    country: str,
    country_code: str,
    course_name: str,
    academic_level: str,
    source_url: str,
    description: str = "",
    language: str = "es",
) -> dict:
    """Validate manual evidence and build an extractor-compatible fragment."""
    university = (university or "").strip()
    country = (country or "").strip()
    country_code = (country_code or "").strip().upper()
    course_name = (course_name or "").strip()
    language = (language or "").strip().lower()
    level = normalize_academic_level(academic_level)
    normalized_url = normalize_url(source_url)

    if not university:
        raise ValueError("University is required")
    if not country:
        raise ValueError("Country is required")
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        raise ValueError("Country code must contain exactly two letters")
    if not course_name:
        raise ValueError("Course name is required")
    if not normalized_url:
        raise ValueError("A valid official http(s) source URL is required")
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise ValueError("Language must be a two- or three-letter code")

    description = (description or "").strip()
    evidence_text = description or course_name
    return {
        "media_type": "html",
        "source_url": normalized_url,
        "found_on_page": normalized_url,
        "pdf_url": "",
        "pdf_page": None,
        "source_type": "syllabus" if description else "course_list",
        "title": course_name,
        "raw_text": f"{course_name}\n{evidence_text}",
        "university": university,
        "country": country,
        "country_code": country_code,
        "language": language,
        "academic_level_hint": "" if level == "unknown" else level,
        "seed_origin": "manual",
        "extraction_status": "extracted",
        "_course_name": course_name,
    }


def classify_fragment(
    fragment: dict,
    classifier: QISEClassifier,
    timestamp: str | None = None,
) -> list[dict]:
    """Classify and retain every distinct semantic category for the course."""
    rows = [
        Pipeline._to_row(
            {**candidate, "course_title": fragment.get("_course_name", "")},
            timestamp or now_iso(),
        )
        for candidate in classifier.classify(fragment)
    ]
    rows = Pipeline._merge([], rows)
    return sorted(
        rows,
        key=lambda row: (
            _CLASS_ORDER.get(row.get("classification"), 9),
            -_CONF_RANK.get(row.get("confidence"), 0),
            row.get("semantic_category", ""),
        ),
    )


def _best_row(rows: list[dict]) -> dict | None:
    """Compatibility helper returning the best row from classified evidence."""
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            _CLASS_ORDER.get(row.get("classification"), 9),
            -_CONF_RANK.get(row.get("confidence"), 0),
        ),
    )[0]


def _read_existing_dataset(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError(f"Dataset path is not a file: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [field for field in OUTPUT_FIELDS if field not in headers]
        if missing:
            raise ValueError(
                f"Existing dataset has an incompatible schema; missing: {missing}"
            )
        rows = list(reader)
        for row in rows:
            raw_flag = str(row.get("is_qise_core", "")).strip().lower()
            row["is_qise_core"] = raw_flag in ("1", "true", "yes")
        return rows


def save_rows(rows: list[dict], path: Path) -> tuple[int, int]:
    """Merge rows without duplicates and keep the CSV/JSON pair synchronized."""
    if not rows:
        raise ValueError("There are no classified rows to save")

    existing = _read_existing_dataset(path)
    merged = Pipeline._merge(existing, rows)
    normalized = [
        {field: row.get(field, "") for field in OUTPUT_FIELDS}
        for row in merged
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    json_path = path.with_suffix(".json")
    csv_temp = path.with_name(f".{path.name}.tmp")
    json_temp = json_path.with_name(f".{json_path.name}.tmp")
    try:
        with csv_temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(normalized)
        json_temp.write_text(
            json.dumps(normalized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        csv_temp.replace(path)
        json_temp.replace(json_path)
    finally:
        for temporary in (csv_temp, json_temp):
            if temporary.exists():
                temporary.unlink()

    added = max(0, len(normalized) - len(existing))
    logger.info(
        "Manual evidence saved: %s new row(s), %s total → %s",
        added,
        len(normalized),
        path,
    )
    return added, len(normalized)


def append_to_dataset(row: dict, path: Path) -> None:
    """Backward-compatible wrapper for callers that save one row."""
    save_rows([row], path)


def _prompt_required(label: str) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        print("  This field is required.")


def _prompt_level() -> str:
    choices = "/".join(ACADEMIC_LEVELS)
    while True:
        value = input(f"Academic level ({choices}) [unknown]: ").strip()
        try:
            return normalize_academic_level(value)
        except ValueError as exc:
            print(f"  {exc}")


def _read_multiline() -> str:
    print("Course description/content (press Enter twice to finish):")
    lines: list[str] = []
    while True:
        line = input()
        if line == "" and lines and lines[-1] == "":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def input_course() -> dict:
    print("\n" + "=" * 58)
    print("  NEW COURSE — official evidence is required")
    print("=" * 58)
    return build_fragment(
        university=_prompt_required("University: "),
        country=_prompt_required("Country: "),
        country_code=_prompt_required("Country code (AR/BR/CL/...): "),
        course_name=_prompt_required("Course name: "),
        academic_level=_prompt_level(),
        source_url=_prompt_required("Official source URL: "),
        description=_read_multiline(),
        language=input("Language code [es]: ").strip() or "es",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Candidate dataset path (default: {DEFAULT_DATASET_PATH})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    classifier = QISEClassifier({})

    print("\nQISE-LatAm — manual course evidence")
    print("Use official evidence for sites the crawler cannot access.")

    try:
        while True:
            try:
                fragment = input_course()
            except ValueError as exc:
                print(f"  Invalid entry: {exc}")
                continue

            rows = classify_fragment(fragment, classifier)
            if not rows:
                print("  No quantum-related evidence detected; nothing was saved.")
            else:
                print(f"\n  Classifications ({len(rows)} row(s)):")
                for row in rows:
                    print(
                        f"    {row['classification']:<34} "
                        f"{row['confidence']:<6} {row['semantic_category'] or '(none)'}"
                    )

                if input("\n  Save these rows? (y/n): ").strip().lower() in ("y", "s"):
                    added, total = save_rows(rows, args.output)
                    print(f"  Saved {added} new row(s); dataset now has {total}.")
                else:
                    print("  Discarded.")

            if input("\nEnter another course? (y/n): ").strip().lower() not in ("y", "s"):
                break
    except (EOFError, KeyboardInterrupt):
        print("\nManual entry cancelled.")
        return 130
    except (OSError, ValueError) as exc:
        logger.error("Could not save manual evidence: %s", exc)
        return 2

    print("\nManual entry finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
