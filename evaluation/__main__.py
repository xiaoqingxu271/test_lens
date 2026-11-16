"""Command-line entry point: ``python -m evaluation``."""

from __future__ import annotations

import argparse

from sandbox.container import (
    ContainerConfig,
    ContainerRuntime,
    SandboxBackend,
)
from sandbox.runner import SandboxConfig

from .run import run_evaluation


def _sandbox_config(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> SandboxConfig:
    backend = SandboxBackend(args.sandbox_backend)
    container = None
    if backend is SandboxBackend.CONTAINER:
        if not args.sandbox_image:
            parser.error(
                "--sandbox-image is required when --sandbox-backend is container"
            )
        container = ContainerConfig(
            image=args.sandbox_image,
            runtime=ContainerRuntime(args.sandbox_runtime),
        )
    elif args.sandbox_image:
        parser.error("--sandbox-image requires --sandbox-backend container")
    return SandboxConfig(
        double_run=True,
        backend=backend,
        container=container,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic TestLens evaluation baseline."
    )
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help=(
            "fixture project directory "
            "(default: all packaged under evaluation/fixtures)"
        ),
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
    args = parser.parse_args(argv)
    config = _sandbox_config(parser, args)

    report = run_evaluation(
        fixtures=args.fixture,
        output_root=args.output,
        threshold=args.threshold,
        seed=args.seed,
        write=not args.no_write,
        sandbox_config=config,
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
