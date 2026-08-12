"""
input_loader.py — Load the university list from CSV or YAML into the internal
`university` dict shape the crawler expects.
"""

import csv
import re
from pathlib import Path
from urllib.parse import urlparse

from utils import normalize_url, get_logger

logger = get_logger("input_loader")

COUNTRY_TO_CODE = {
    "argentina": "AR", "bolivia": "BO", "brazil": "BR", "brasil": "BR",
    "chile": "CL", "colombia": "CO", "costa rica": "CR", "cuba": "CU",
    "dominican republic": "DO", "ecuador": "EC", "el salvador": "SV",
    "guatemala": "GT", "honduras": "HN", "mexico": "MX", "méxico": "MX",
    "nicaragua": "NI", "panama": "PA", "panamá": "PA", "paraguay": "PY",
    "peru": "PE", "perú": "PE", "puerto rico": "PR", "uruguay": "UY",
    "venezuela": "VE",
}

_SEED_COL_HINTS = ("url", "seed", "catalog", "catálogo", "dept", "department",
                   "departamento", "faculty", "facultad", "curricul", "malla",
                   "programa", "plan")
_SPLIT = re.compile(r"[;\|\n]+")


# CAMBIO
def _to_absolute_url(raw: str, base: str = "") -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    url = normalize_url(raw)
    if url:
        return url
    if "/" not in raw and "." in raw and not raw.startswith("."):
        url = normalize_url("https://" + raw)
        if url:
            return url
    if base:
        url = normalize_url(raw, base=base)
        if url:
            return url
    return ""


def load_universities(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Input path does not exist or is not a file: {path}")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        unis = _load_yaml(p)
    elif suffix == ".csv":
        unis = _load_csv(p)
    else:
        raise ValueError(f"Unsupported input format '{suffix}'. Use .csv or .yaml")


    cleaned: list[dict] = []

    for position, raw_university in enumerate(unis, start=1):
        university = _finalize(raw_university)

        if university is None:
            if not isinstance(raw_university, dict):
                reason = "entry is not a dictionary"
            else:
                reason = "missing university name"

            logger.warning(
                f"Skipped entry {position} from {path}: {reason}"
            )  
            continue

        if not university.get("catalog_urls"):
            logger.warning(
                f"Skipped university '{university['name']}' from {path}: "
                "no valid base_url or catalog_urls"
            )
            continue

        cleaned.append(university)


    logger.info(f"Loaded {len(cleaned)} universities from {path}")
    return cleaned


def _load_yaml(p: Path) -> list[dict]:
    import yaml
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        data = data.get("universities", [])
    if not isinstance(data, list):
        raise ValueError("YAML must be a list, or a mapping with a 'universities' list")
    return data


def _load_csv(p: Path) -> list[dict]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = p.read_text(encoding="utf-8", errors="replace")

    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames or []
    name_col = _find_col(headers, ["institution", "university", "universidad", "institución", "name", "nombre"],)
    country_col = _find_col(headers, ["country", "país", "pais"])
    code_col = _find_col(headers, ["country_code", "code", "iso"])
    base_col = _find_col(headers, ["website", "domain", "base_url", "url oficial",
                                   "sitio", "official"])
    lang_col = _find_col(headers, ["language", "idioma", "lang"])
    depth_col = _find_col(headers, ["max_depth", "depth"])
    pages_col = _find_col(headers, ["max_pages", "pages"])
    seed_cols = [h for h in headers if any(k in h.lower() for k in _SEED_COL_HINTS)]

    if not name_col:
        raise ValueError(f"CSV needs an institution/name column. Found: {headers}")

    out: list[dict] = []
    for row_number, row in enumerate(reader, start=2):
        name = (row.get(name_col) or "").strip()
        if not name:
            logger.warning(
                f"Skipped CSV row {row_number} from {p}: "
                "missing university name"
            )
            continue
        

        seeds: list[str] = []

        raw_base = (row.get(base_col) or "").strip() if base_col else ""
        base = _to_absolute_url(raw_base)  # CAMBIO: antes normalize_url(raw_base) a secas

        for col in seed_cols:
            if col == base_col:
                continue

            for piece in _SPLIT.split(row.get(col) or ""):
                for sub in piece.split(","):
                    sub = sub.strip()

                    if not sub:
                        continue

                    url = _to_absolute_url(sub, base=base)  # CAMBIO: antes solo normalize_url(sub)

                    if url:
                        seeds.append(url)

        has_manual = bool(seeds)

        if base and base not in seeds:
            seeds.insert(0, base)
        
        
        out.append({
            "name": name,
            "country": (row.get(country_col) or "").strip() if country_col else "",
            "country_code": (row.get(code_col) or "").strip() if code_col else "",
            "base_url": base,
            "catalog_urls": seeds,
            "has_manual_seeds": has_manual,
            "language": (row.get(lang_col) or "").strip() if lang_col else "",
            "max_depth": _to_int(row.get(depth_col), minimum=0) if depth_col else None,
            "max_pages": _to_int(row.get(pages_col), minimum=1) if pages_col else None,
        })
    return out


def _finalize(u: dict) -> dict | None:
    if not isinstance(u, dict):
        return None

    name = (u.get("name") or "").strip()

    if not name:
        return None

    base = _to_absolute_url(str(u.get("base_url") or ""))  # CAMBIO: antes normalize_url(...) a secas

    raw_seeds = u.get("catalog_urls") or []

    if isinstance(raw_seeds, str):
        raw_seeds = [raw_seeds]

    if not base:
        for raw_seed in raw_seeds:
            candidate = _to_absolute_url(str(raw_seed))  # CAMBIO: idem

            if candidate:
                parsed = urlparse(candidate)
                base = normalize_url(
                    f"{parsed.scheme}://{parsed.netloc}"
                )
                break

    seeds: list[str] = []

    for raw_seed in raw_seeds:
        raw_seed = str(raw_seed).strip()

        if not raw_seed:
            continue

        url = _to_absolute_url(raw_seed, base=base)  # CAMBIO: antes normalize_url + fallback manual

        if url:
            seeds.append(url)

    seeds = list(dict.fromkeys(seeds))

    has_manual = bool(
        u.get("has_manual_seeds", bool(seeds))
    )

    if not seeds and base:
        seeds = [base]


    country = str(u.get("country") or "").strip()
    code = str(u.get("country_code") or "").strip().upper()

    inferred_code = COUNTRY_TO_CODE.get(country.casefold(), "")

    if inferred_code:
        code = inferred_code
    elif not re.fullmatch(r"[A-Z]{2}", code):
        code = ""
    

    return {
        "name": name,
        "country": country,
        "country_code": code,
        "base_url": base,
        "catalog_urls": seeds,
        "has_manual_seeds": has_manual,
        "type": u.get("type", "web"),
        "language": (u.get("language") or "").strip(),
        "max_depth": _to_int(u.get("max_depth"), minimum=0),
        "max_pages": _to_int(u.get("max_pages"), minimum=1),
    }



def _normalize_header(text: str) -> str:
    """Convierte encabezados distintos a una forma comparable."""
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    normalized_headers = [
        (original, _normalize_header(original))
        for original in headers
    ]

    for candidate in candidates:
        normalized_candidate = _normalize_header(candidate)

        for original, normalized_header in normalized_headers:
            if normalized_header == normalized_candidate:
                return original

    for candidate in candidates:
        normalized_candidate = _normalize_header(candidate)

        if normalized_candidate in {"name", "nombre"}:
            continue

        for original, normalized_header in normalized_headers:
            words = normalized_header.split()

            if normalized_candidate in words:
                return original

    return None


def _to_int(value, minimum: int = 1) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None

    if number < minimum:
        return None

    return number