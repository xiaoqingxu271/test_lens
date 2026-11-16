from __future__ import annotations

from pathlib import Path

from helpers import line_of, make_root_profile
from targets.models import OrderReason, RouteSkipReason
from targets.selection import select_targets

ROUTES_ROOT = Path(__file__).parent / "fixtures" / "routes_project"
APP = ROUTES_ROOT / "app.py"
FLASK_APP = ROUTES_ROOT / "flask_app.py"


def _target(report, name):
    return next(target for target in report.targets if target.name == name)


def _stable(report):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def test_select_targets_extracts_fastapi_and_flask_routes():
    report = select_targets(make_root_profile(ROUTES_ROOT))

    assert [target.name for target in report.targets] == [
        "read_item",
        "create_item",
        "multi",
        "flask_route",
        "two_paths",
        "hello",
        "bye",
        "kw",
        "kw_get",
    ]
    assert report.trace.functions_seen == 15
    assert report.trace.targets_selected == 9
    assert report.trace.files_scanned == 2
    assert [item.file for item in report.trace.files_skipped] == ["broken.py"]
    assert report.trace.files_skipped[0].reason.value == "syntax_error"
    assert report.trace.order_reason == OrderReason.STATIC

    read_item = _target(report, "read_item")
    assert read_item.symbol_id == "app.py::read_item"
    assert read_item.relative_file == "app.py"
    assert read_item.line_start == line_of(APP, "def read_item")
    assert read_item.line_end >= read_item.line_start
    assert read_item.routes[0].path == "/items/{item_id}"
    assert read_item.routes[0].methods == ["GET"]
    assert read_item.routes[0].decorator == "app.get"
    assert read_item.routes[0].line == line_of(APP, '@app.get("/items/{item_id}")')

    create_item = _target(report, "create_item")
    assert create_item.routes[0].path == "/items"
    assert create_item.routes[0].methods == ["POST"]
    assert create_item.routes[0].decorator == "router.post"

    multi = _target(report, "multi")
    assert multi.routes[0].path == "/multi"
    assert multi.routes[0].methods == ["GET", "POST"]
    assert multi.routes[0].decorator == "app.api_route"

    flask_route = _target(report, "flask_route")
    assert flask_route.routes[0].path == "/flask"
    assert flask_route.routes[0].methods == ["GET", "POST"]
    assert flask_route.routes[0].decorator == "app.route"

    two_paths = _target(report, "two_paths")
    assert [route.path for route in two_paths.routes] == ["/first", "/second"]
    assert all(route.methods == ["GET"] for route in two_paths.routes)

    hello = _target(report, "hello")
    assert hello.symbol_id == "flask_app.py::hello"
    assert hello.routes[0].path == "/hello"
    assert hello.routes[0].methods == ["GET"]
    assert hello.routes[0].decorator == "bp.route"

    assert _target(report, "kw").routes[0].path == "/kw"
    assert _target(report, "kw_get").routes[0].path == "/kw-get"
    assert _target(report, "kw_get").routes[0].decorator == "bp.get"

    assert all(target.name != "ignored" for target in report.targets)


def test_select_targets_records_skipped_routes_with_reasons():
    report = select_targets(make_root_profile(ROUTES_ROOT))

    skipped = {
        (route.decorator, route.reason): route
        for route in report.trace.skipped_routes
    }
    assert len(skipped) == 4

    dynamic_path = skipped[("app.get", RouteSkipReason.DYNAMIC_PATH)]
    assert dynamic_path.line == line_of(APP, "@app.get(DYNAMIC)")

    missing_path = skipped[("app.get", RouteSkipReason.MISSING_PATH)]
    assert missing_path.line == line_of(APP, "@app.get()")

    websocket = skipped[("app.websocket", RouteSkipReason.UNSUPPORTED_DECORATOR)]
    assert websocket.line == line_of(APP, '@app.websocket("/ws")')

    dynamic_methods = skipped[("app.route", RouteSkipReason.DYNAMIC_METHODS)]
    assert dynamic_methods.line == line_of(
        APP,
        '@app.route("/dynamic-methods"',
    )


def test_select_targets_is_deterministic():
    first = select_targets(make_root_profile(ROUTES_ROOT))
    second = select_targets(make_root_profile(ROUTES_ROOT))

    assert _stable(first) == _stable(second)


def test_coverage_rank_only_reorders_targets():
    profile = make_root_profile(ROUTES_ROOT)
    baseline = select_targets(profile)
    ranked = select_targets(
        profile,
        coverage_rank={
            "app.py::read_item": 100.0,
            "flask_app.py::hello": 50.0,
        },
    )

    assert ranked.trace.order_reason == OrderReason.COVERAGE
    assert [target.symbol_id for target in ranked.targets[:2]] == [
        "app.py::read_item",
        "flask_app.py::hello",
    ]
    assert {target.symbol_id for target in ranked.targets} == {
        target.symbol_id for target in baseline.targets
    }


def test_selected_target_converts_to_runner_symbol():
    report = select_targets(make_root_profile(ROUTES_ROOT))
    symbol = _target(report, "read_item").to_symbol()

    assert symbol.symbol_id == "app.py::read_item"
    assert symbol.name == "read_item"
    assert symbol.file == APP.resolve()
    assert symbol.line_start == line_of(APP, "def read_item")


def test_project_without_routes_reports_no_targets(tmp_path):
    (tmp_path / "helpers.py").write_text(
        "def helper():\n    return 42\n",
        encoding="utf-8",
    )

    report = select_targets(make_root_profile(tmp_path))

    assert report.targets == []
    assert report.trace.targets_selected == 0
    assert report.trace.warnings == ["no route handlers found"]
