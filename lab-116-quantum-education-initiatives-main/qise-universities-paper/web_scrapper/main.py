"""
main.py — Command-line entry point for the QISE-LatAm scraper.

Typical use:
    python main.py --input data/universities.csv --output data/qise_candidates.csv

Useful flags:
    --max-depth 2                  crawl depth per institution
    --max-pages-per-domain 100     hard page cap per institution
    --request-delay 1.5            seconds between requests
    --download-pdfs true|false     fetch and parse linked PDFs
    --country Peru                 filter by country name or ISO code
    --resume true|false            skip institutions already in the output
    --limit 200                    stop after N processed fragments
    --dry-run                      run without writing result files
    --fail-on-empty                return exit code 1 when no evidence is found
    --no-cache                     ignore the on-disk download cache
    --no-robots                    skip robots.txt checks (use responsibly)

Input may be CSV or YAML. If --input is omitted, the loader uses --sources
(default: sources.yaml).

Exit codes:
    0    completed successfully (an empty result is valid by default)
    1    completed but found no rows/seeds and --fail-on-empty was requested
    2    invalid input, configuration, or command-line arguments
    3    unexpected execution failure
    130  interrupted by the user
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Keep imports for the scraping stack lazy.  Besides making the module easier to
# test, this lets ``python main.py --help`` work before optional/runtime
# dependencies have been installed.
logger = logging.getLogger("main")

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_INPUT_SUFFIXES = {".csv", ".yaml", ".yml"}
_YAML_SUFFIXES = {".yaml", ".yml"}


def _load_runtime() -> type[Any]:
    """Import the runtime only after CLI parsing and configure project logging."""
    global logger

    from utils import get_logger
    from pipeline import Pipeline

    logger = get_logger("main")
    return Pipeline


def _report_missing_dependency(exc: ModuleNotFoundError) -> None:
    """Print a useful startup error even when project logging cannot import."""
    missing = exc.name or str(exc)
    print(
        f"Missing required Python dependency: {missing}.\n"
        "Install the project dependencies with:\n"
        "  python -m pip install -r requirements.txt",
        file=sys.stderr,
    )


def _str2bool(value: str | bool) -> bool:
    """Parse an explicit command-line boolean without silent fallbacks."""
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    choices = "true/false, yes/no, 1/0, on/off"
    raise argparse.ArgumentTypeError(
        f"invalid boolean value {value!r}; use one of: {choices}"
    )


def _non_negative_int(value: str) -> int:
    """Argparse type for integers greater than or equal to zero."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _positive_int(value: str) -> int:
    """Argparse type for integers greater than zero."""
    parsed = _non_negative_int(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_float(value: str) -> float:
    """Argparse type for finite floats greater than or equal to zero."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected a number") from exc
    if parsed < 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return parsed


def _validate_file(
    parser: argparse.ArgumentParser,
    value: str,
    option: str,
    allowed_suffixes: set[str],
) -> None:
    """Validate a required local input file and its extension."""
    path = Path(value)
    if not path.is_file():
        parser.error(f"{option} does not exist or is not a file: {value}")
    if path.suffix.lower() not in allowed_suffixes:
        expected = ", ".join(sorted(allowed_suffixes))
        parser.error(f"{option} must use one of these extensions: {expected}")


def _validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Validate paths and combinations that argparse cannot express alone."""
    _validate_file(parser, args.config, "--config", {".yaml", ".yml"})

    if args.input:
        _validate_file(parser, args.input, "--input", _INPUT_SUFFIXES)
    else:
        _validate_file(parser, args.sources, "--sources", _INPUT_SUFFIXES)

    if args.include_news or args.include_social:
        _validate_file(parser, args.sources, "--sources", _YAML_SUFFIXES)

    output = Path(args.output)
    if output.suffix.lower() != ".csv":
        parser.error("--output must be a .csv file")
    if output.exists() and output.is_dir():
        parser.error(f"--output points to a directory: {args.output}")
    if output.parent.exists() and not output.parent.is_dir():
        parser.error(f"--output parent is not a directory: {output.parent}")

    # The pipeline writes more than the primary CSV.  Prevent an input file
    # from being silently overwritten by any generated artifact.
    generated_paths = {
        "--output": output,
        "JSON output": output.with_suffix(".json"),
        "analytical output": output.with_suffix(".analytical.json"),
        "university summary": output.parent / "university_summary.csv",
        "country summary": output.parent / "country_summary.csv",
    }
    active_inputs = {"--config": Path(args.config)}
    if args.input:
        active_inputs["--input"] = Path(args.input)
        if args.include_news or args.include_social:
            active_inputs["--sources"] = Path(args.sources)
    else:
        active_inputs["--sources"] = Path(args.sources)

    for input_label, input_path in active_inputs.items():
        for output_label, generated_path in generated_paths.items():
            if input_path.resolve(strict=False) == generated_path.resolve(strict=False):
                parser.error(
                    f"{output_label} would overwrite {input_label}: {input_path}"
                )

    if args.country is not None:
        args.country = args.country.strip()
        if not args.country:
            parser.error("--country cannot be empty")

    if args.discover_seeds_only:
        ignored = []
        if args.resume:
            ignored.append("--resume")
        if args.limit is not None:
            ignored.append("--limit")
        if args.include_news:
            ignored.append("--include-news")
        if args.include_social:
            ignored.append("--include-social")
        if ignored:
            parser.error(
                "--discover-seeds-only cannot be combined with " + ", ".join(ignored)
            )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser separately so tests and integrations can reuse it."""
    parser = argparse.ArgumentParser(
        description=(
            "QISE-LatAm scraper: harvest auditable quantum-coursework "
            "evidence from Latin American universities."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        default=None,
        help="University list (.csv/.yaml/.yml). Falls back to --sources.",
    )
    parser.add_argument(
        "--output",
        default="data/qise_candidates.csv",
        help="Output .csv path; a .json sibling is also written.",
    )
    parser.add_argument("--config", default="config.yaml", help="Config YAML path.")
    parser.add_argument(
        "--sources",
        default="sources.yaml",
        help="Fallback university/source list when --input is omitted.",
    )
    parser.add_argument(
        "--max-depth",
        type=_non_negative_int,
        default=None,
        help="Maximum link depth per institution (0 means seed pages only).",
    )
    parser.add_argument(
        "--max-pages-per-domain",
        type=_positive_int,
        default=None,
        help="Maximum pages fetched per institution.",
    )
    parser.add_argument(
        "--request-delay",
        type=_non_negative_float,
        default=None,
        metavar="SECONDS",
        help="Minimum delay between requests; overrides config.yaml.",
    )
    parser.add_argument(
        "--download-pdfs",
        type=_str2bool,
        default=None,
        metavar="true|false",
    )
    parser.add_argument("--country", default=None, help="Country name or ISO code.")
    parser.add_argument(
        "--resume",
        type=_str2bool,
        default=False,
        metavar="true|false",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Stop after N processed fragments (smoke test).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing candidate, seed, or summary files.",
    )
    parser.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="Return exit code 1 when no candidates or seeds are found.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the on-disk download cache.",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Skip robots.txt checks (use responsibly).",
    )
    parser.add_argument(
        "--include-news",
        action="store_true",
        help="Also crawl news_sources from the sources file.",
    )
    parser.add_argument(
        "--include-social",
        action="store_true",
        help="Also query social_sources; API tokens may be required.",
    )
    parser.add_argument(
        "--discover-seeds-only",
        action="store_true",
        help="Discover seeds and exit without crawling.",
    )
    parser.add_argument(
        "--auto-discover",
        type=_str2bool,
        default=None,
        metavar="true|false",
        help="Toggle automatic discovery for institutions without manual seeds.",
    )
    parser.add_argument(
        "--force-discover",
        action="store_true",
        help="Discover even when manual seeds exist; discovered seeds replace them.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    return args


def _log_run(args: argparse.Namespace) -> None:
    """Log the effective high-level execution parameters."""
    logger.info("Input   : %s", args.input or args.sources)
    logger.info("Output  : %s", args.output)
    logger.info("Country : %s", args.country or "all")
    logger.info(
        "Resume  : %s | dry-run: %s | limit: %s",
        args.resume,
        args.dry_run,
        args.limit if args.limit is not None else "none",
    )


def _empty_result_exit_code(empty: bool, fail_on_empty: bool) -> int:
    """Treat an empty scientific result as success unless strict mode is set."""
    return 1 if empty and fail_on_empty else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = parse_args(argv)

    overrides = {
        "max_depth": args.max_depth,
        "max_pages_per_university": args.max_pages_per_domain,
        "request_delay_sec": args.request_delay,
        "download_pdfs": args.download_pdfs,
        "use_cache": False if args.no_cache else None,
        "respect_robots": False if args.no_robots else None,
        "auto_discover_seeds": args.auto_discover,
    }

    try:
        pipeline_class = _load_runtime()
        _log_run(args)

        pipeline = pipeline_class(
            config_path=args.config,
            input_path=args.input,
            sources_path=args.sources,
            overrides=overrides,
        )

        if args.discover_seeds_only:
            seeds = pipeline.discover_only(
                country=args.country,
                force=args.force_discover,
                dry_run=args.dry_run,
            )
            if seeds == 0:
                logger.warning("Seed discovery completed without new seeds.")
            return _empty_result_exit_code(seeds == 0, args.fail_on_empty)

        summary = pipeline.run(
            output_path=args.output,
            dry_run=args.dry_run,
            limit=args.limit,
            country=args.country,
            resume=args.resume,
            include_news=args.include_news,
            include_social=args.include_social,
            force_discover=args.force_discover,
        )

        if summary.candidate_rows == 0:
            logger.warning(
                "No candidate evidence found. Check reachability, crawl budgets, "
                "and the run summary before interpreting this as a confirmed zero."
            )
        return _empty_result_exit_code(
            summary.candidate_rows == 0,
            args.fail_on_empty,
        )

    except ModuleNotFoundError as exc:
        _report_missing_dependency(exc)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Invalid input or configuration: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130
    except Exception:  # final CLI boundary: record the full traceback
        logger.exception("Unexpected execution failure")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
