"""TestLens CLI.

The CLI is an entry point, not a new pipeline. It calls the same stage
functions as the library. The deterministic fake is the default; the model
source is selected through ``llm/options.py`` (fake, replay, or real).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from audit.grade import grade_and_emit
from audit.independent import scan_independent
from audit.repro import run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from evaluation.labels import FixtureError, load_proposals_file
from evaluation.run import run_evaluation
from generation.generate import SandboxUnavailableError, generate_tests
from llm.errors import LLMError
from llm.options import (
    LLMMode,
    add_llm_arguments,
    build_llm,
    build_llm_factory,
    options_from_args,
)
from pipeline.run import run_test_pipeline
from review.cross_check import (
    cross_check,
    subjects_from_candidates,
    subjects_from_independent_claims,
)
from review.fake import DeterministicReviewer
from sandbox import runner
from sandbox.container import (
    ContainerConfig,
    ContainerRuntime,
    SandboxBackend,
)
from sandbox.runner import ProjectProfile, SandboxConfig
from storage.db import (
    open_database,
    save_audit_report,
    save_evaluation_report,
    save_test_report,
)
from targets.selection import select_targets

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SANDBOX = 3
EXIT_MODEL = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testlens",
        description=(
            "LLM-proposed tests and audit findings, decided by a sandbox, "
            "mutation scoring, and an independent cross-check."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tests = sub.add_parser("tests", help="generate, execute, and score tests")
    tests.add_argument("project", help="target project directory")
    tests.add_argument("--threshold", type=float, default=0.5)
    tests.add_argument("--seed", type=int, default=0)
    tests.add_argument(
        "--proposals",
        default=None,
        help="JSON file mapping deterministic request ids to fake responses",
    )
    tests.add_argument("--output", default=None, help="report output root")
    tests.add_argument("--db", default=None, help="SQLite metadata path")
    tests.add_argument("--json", action="store_true", dest="as_json")
    _add_sandbox_args(tests)
    add_llm_arguments(tests)

    audit = sub.add_parser("audit", help="scan rules and grade findings")
    audit.add_argument("project", help="target project directory")
    audit.add_argument("--output", default=None, help="report output root")
    audit.add_argument("--db", default=None, help="SQLite metadata path")
    audit.add_argument("--json", action="store_true", dest="as_json")
    audit.add_argument(
        "--independent",
        action="store_true",
        help="scan for LLM-independent semantic claims",
    )
    audit.add_argument(
        "--proposals",
        default=None,
        help="JSON file mapping deterministic request ids to fake responses",
    )
    _add_sandbox_args(audit)
    add_llm_arguments(audit)

    evaluate = sub.add_parser("eval", help="run the fixture evaluation baseline")
    evaluate.add_argument("--output", default=None, help="report output root")
    evaluate.add_argument("--threshold", type=float, default=0.5)
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--db", default=None, help="SQLite metadata path")
    evaluate.add_argument("--json", action="store_true", dest="as_json")
    evaluate.add_argument(
        "--independent",
        action="store_true",
        help="enable LLM-independent claims (requires fixture labels)",
    )
    _add_sandbox_args(evaluate)
    add_llm_arguments(evaluate)

    return parser


def _add_sandbox_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sandbox-backend",
        choices=[item.value for item in SandboxBackend],
        default=SandboxBackend.SUBPROCESS.value,
    )
    parser.add_argument(
        "--sandbox-image",
        default=None,
        help="container image; required for --sandbox-backend container",
    )
    parser.add_argument(
        "--sandbox-runtime",
        choices=[item.value for item in ContainerRuntime],
        default=ContainerRuntime.DOCKER.value,
    )


def _project(raw: str) -> Path:
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    return root


def _profile(root: Path) -> ProjectProfile:
    return ProjectProfile(
        profile_id=root.name,
        root=root,
        test_paths=[],
        target_symbols=[],
    )


def _sandbox_config(args) -> SandboxConfig:
    backend = SandboxBackend(args.sandbox_backend)
    container = None
    if backend == SandboxBackend.CONTAINER:
        if not args.sandbox_image:
            raise ValueError(
                "--sandbox-image is required when --sandbox-backend is container"
            )
        container = ContainerConfig(
            image=args.sandbox_image,
            runtime=ContainerRuntime(args.sandbox_runtime),
        )
    elif args.sandbox_image:
        raise ValueError(
            "--sandbox-image requires --sandbox-backend container"
        )
    return SandboxConfig(
        double_run=True,
        backend=backend,
        container=container,
    )


def _write_report(report, output_root: str, kind: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = Path(output_root).expanduser().resolve() / stamp
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / f"{kind}_report.json"
    path.write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return path


def _emit(report, args, kind: str, summary) -> None:
    if args.output:
        path = _write_report(report, args.output, kind)
        print(f"report: {path}", file=sys.stderr)
    if args.as_json:
        print(report.model_dump_json(indent=2))
    else:
        summary()


def _tests(args) -> int:
    root = _project(args.project)
    profile = _profile(root)
    config = _sandbox_config(args)
    selection = select_targets(profile)
    responses = (
        load_proposals_file(Path(args.proposals).expanduser().resolve())
        if args.proposals
        else None
    )
    options = options_from_args(args)
    if args.proposals and options.mode is not LLMMode.FAKE:
        raise ValueError("--proposals requires --llm fake")
    llm = build_llm(options, responses)
    generation = generate_tests(
        profile,
        selection,
        llm,
        sandbox_config=config,
    )
    report = run_test_pipeline(
        profile,
        selection,
        generation,
        kill_threshold=args.threshold,
        seed=args.seed,
        sandbox_config=config,
    )

    if args.db:
        with open_database(args.db) as conn:
            save_test_report(
                conn,
                root.name,
                report,
                model_kind=llm.model_kind.value,
                model_id=llm.model_id,
            )

    def summary() -> None:
        print(
            f"kept={len(report.kept)} "
            f"flaky={len(report.flaky)} "
            f"discarded={dict(report.trace.discarded_by_reason)}"
        )
        print(f"threshold={report.trace.kill_threshold} seed={report.trace.seed}")

    _emit(report, args, "test", summary)
    return EXIT_OK


def _audit(args) -> int:
    root = _project(args.project)
    profile = _profile(root)
    options = options_from_args(args)
    responses = (
        load_proposals_file(Path(args.proposals).expanduser().resolve())
        if args.proposals
        else None
    )
    if args.proposals and options.mode is not LLMMode.FAKE:
        raise ValueError("--proposals requires --llm fake")
    llm = build_llm(options, responses)
    scan = scan_rules(profile)
    independent = scan_independent(profile, scan, llm) if args.independent else None
    triage = llm_triage(profile, scan, llm)
    subjects = subjects_from_candidates(scan, triage)
    if independent is not None:
        subjects = subjects + subjects_from_independent_claims(independent)
    review = cross_check(
        profile,
        scan,
        subjects,
        DeterministicReviewer(),
    )
    config = runner.resolve_config(_sandbox_config(args))
    with tempfile.TemporaryDirectory(prefix="testlens_cli_repro_") as temporary:
        repro = run_repro(
            profile,
            scan,
            review,
            sandbox_config=config,
            artifact_root=Path(temporary),
        )
    report = grade_and_emit(
        scan, triage, review, repro, independent_report=independent
    )

    if args.db:
        with open_database(args.db) as conn:
            save_audit_report(
                conn,
                root.name,
                report,
                platform=config.platform,
                enforcement_mode=config.enforcement_mode.value,
                double_run=config.double_run,
                model_kind=llm.model_kind.value,
                model_id=llm.model_id,
            )

    def summary() -> None:
        print(
            f"findings={len(report.findings)} "
            f"retracted={len(report.retracted)} "
            f"folded_suspicious={len(report.folded_suspicious)}"
        )
        print(
            "evidence="
            + json.dumps(
                {
                    level.value: count
                    for level, count in report.trace.evidence_level_counts.items()
                },
                sort_keys=True,
            )
        )

    _emit(report, args, "audit", summary)
    return EXIT_OK


def _evaluation(args) -> int:
    options = options_from_args(args)
    report = run_evaluation(
        output_root=args.output,
        threshold=args.threshold,
        seed=args.seed,
        write=True,
        independent=args.independent,
        sandbox_config=_sandbox_config(args),
        llm_factory=build_llm_factory(options),
    )
    if args.db:
        with open_database(args.db) as conn:
            save_evaluation_report(conn, report)

    def summary() -> None:
        if report.output_dir:
            print(f"report: {report.output_dir}")
        for fixture in report.fixtures:
            print(
                f"{fixture.project}: kept={fixture.test.kept} "
                f"discarded={fixture.test.discarded_by_reason} "
                f"findings={fixture.audit.findings_emitted} "
                f"precision={fixture.audit.precision}"
            )

    if args.as_json:
        print(report.model_dump_json(indent=2))
    else:
        summary()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "tests": _tests,
        "audit": _audit,
        "eval": _evaluation,
    }[args.command]
    try:
        return handler(args)
    except SandboxUnavailableError as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return EXIT_SANDBOX
    except LLMError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return EXIT_MODEL
    except (FixtureError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
