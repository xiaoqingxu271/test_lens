"""Models for deterministic target selection."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from project.files import SkippedFile
from sandbox.runner import TargetSymbol


class OrderReason(str, Enum):
    STATIC = "static"
    COVERAGE = "coverage"


class RouteSkipReason(str, Enum):
    DYNAMIC_PATH = "dynamic_path"
    DYNAMIC_METHODS = "dynamic_methods"
    MISSING_PATH = "missing_path"
    UNSUPPORTED_DECORATOR = "unsupported_decorator"


class Route(BaseModel):
    methods: list[str]
    path: str
    decorator: str
    line: int


class SkippedRoute(BaseModel):
    file: str
    line: int
    decorator: str
    reason: RouteSkipReason
    detail: str | None = None


class SelectedTarget(BaseModel):
    symbol_id: str
    name: str
    file: str
    relative_file: str
    line_start: int
    line_end: int
    routes: list[Route] = Field(default_factory=list)

    def to_symbol(self) -> TargetSymbol:
        return TargetSymbol(
            symbol_id=self.symbol_id,
            name=self.name,
            file=Path(self.file),
            line_start=self.line_start,
            line_end=self.line_end,
        )


class TargetSelectionTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    files_scanned: int
    files_skipped: list[SkippedFile] = Field(default_factory=list)
    functions_seen: int = 0
    targets_selected: int = 0
    skipped_routes: list[SkippedRoute] = Field(default_factory=list)
    order_reason: OrderReason = OrderReason.STATIC
    warnings: list[str] = Field(default_factory=list)


class TargetSelectionReport(BaseModel):
    targets: list[SelectedTarget] = Field(default_factory=list)
    trace: TargetSelectionTrace
