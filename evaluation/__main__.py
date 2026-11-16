"""Command-line entry point: ``python -m evaluation``."""

from __future__ import annotations

import argparse
from pathlib import Path

from .run import DEFAULT_FIXTURES, run_evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic TestLens evaluation baseline."
    )
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help="fixture project directory (default: all under eval/fixtures)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="report root (default: eval/reports)",
    )
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="run and print metrics without writing a report",
    )
    args = parser.parse_args(argv)

    report = run_evaluation(
        fixtures=args.fixture,
        output_root=args.output,
        threshold=args.threshold,
        seed=args.seed,
        write=not args.no_write,
    )
    if report.output_dir:
        print(report.output_dir)
    else:
        print("evaluation completed; report not written")
    for fixture in report.fixtures:
        test = fixture.test
        audit = fixture.audit
        print(
            f"{fixture.project}: kept={test.kept} "
            f"discarded={test.discarded_by_reason} "
            f"findings={audit.findings_emitted} "
            f"precision={audit.precision}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
