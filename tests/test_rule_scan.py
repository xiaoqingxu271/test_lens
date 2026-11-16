from __future__ import annotations

from pathlib import Path

from audit.models import FindingSource, RuleId
from audit.rule_scan import scan_rules
from audit.rules import ALL_RULES
from helpers import line_of, make_root_profile

AUDIT_ROOT = Path(__file__).parent / "fixtures" / "audit_project"


def _scan(rule: RuleId, subdir: str):
    root = AUDIT_ROOT / subdir
    return scan_rules(make_root_profile(root), rules=(rule,))


def _by_line(report):
    return {candidate.line_start: candidate for candidate in report.candidates}


def _stable(report):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def test_r001_flags_mutable_defaults_and_skips_safe_defaults():
    defects = AUDIT_ROOT / "r001" / "defects.py"
    report = _scan(RuleId.R001_MUTABLE_DEFAULT, "r001")

    assert report.trace.files_scanned == 2
    assert len(report.candidates) == 7
    assert all(candidate.rule_id == RuleId.R001_MUTABLE_DEFAULT for candidate in report.candidates)
    assert all(candidate.source == FindingSource.RULE for candidate in report.candidates)
    assert report.trace.candidates_by_rule[RuleId.R001_MUTABLE_DEFAULT] == 7
    assert report.trace.rules_run == [RuleId.R001_MUTABLE_DEFAULT]

    candidates = _by_line(report)
    markers = {
        "def list_default": "items",
        "def dict_default": "options",
        "def set_default": "values",
        "def bytearray_default": "buffer",
        "def list_constructor": "items",
        "def tuple_default": "pair",
        "def comprehension_default": "values",
    }
    for marker, parameter in markers.items():
        candidate = candidates[line_of(defects, marker)]
        assert parameter in candidate.message
        assert "Mutable default" in candidate.title
        assert candidate.snippet
        assert candidate.snippet_hash
        assert candidate.column >= 0

    safe_lines = {
        line_of(defects, "def safe_none"),
        line_of(defects, "def safe_tuple"),
        line_of(defects, "def safe_factory"),
    }
    assert safe_lines.isdisjoint(candidates)
    assert all(candidate.relative_file == "defects.py" for candidate in report.candidates)


def test_r001_ignores_shadowed_constructor_names():
    report = _scan(RuleId.R001_MUTABLE_DEFAULT, "r001")

    assert all(candidate.relative_file != "shadowed.py" for candidate in report.candidates)


def test_r003_flags_only_direct_unreachable_regions():
    defects = AUDIT_ROOT / "r003" / "defects.py"
    report = _scan(RuleId.R003_UNREACHABLE_AFTER_RETURN, "r003")

    assert len(report.candidates) == 4
    candidates = _by_line(report)

    after_return = candidates[line_of(defects, "value = 2")]
    assert after_return.line_end == line_of(defects, "print(value)")
    assert "after return" in after_return.message

    after_raise = candidates[line_of(defects, "value = 3")]
    assert "after raise" in after_raise.message

    in_branch = candidates[line_of(defects, "value = 5", after="def in_branch")]
    assert "after return" in in_branch.message

    in_try = candidates[line_of(defects, "value = 6", after="def in_try")]
    assert "after return" in in_try.message

    assert line_of(defects, "value = 4") not in candidates


def test_r004_flags_bare_except_and_distinguishes_swallowing():
    defects = AUDIT_ROOT / "r004" / "defects.py"
    report = _scan(RuleId.R004_BARE_EXCEPT_SWALLOW, "r004")

    assert len(report.candidates) == 4
    candidates = _by_line(report)

    bare_pass = candidates[line_of(defects, "except:", after="def bare_pass")]
    bare_log = candidates[line_of(defects, "except:", after="def bare_log")]
    bare_reraise = candidates[line_of(defects, "except:", after="def bare_reraise")]
    bare_ellipsis = candidates[line_of(defects, "except:", after="def bare_ellipsis")]

    assert "swallows" in bare_pass.message
    assert "swallows" in bare_ellipsis.message
    assert "swallows" not in bare_log.message
    assert "swallows" not in bare_reraise.message
    assert line_of(defects, "except Exception:", after="def narrow") not in candidates


def test_shared_scan_records_syntax_errors_and_skips_tests():
    report = scan_rules(make_root_profile(AUDIT_ROOT), rules=ALL_RULES)

    skipped = {
        item.file: item
        for item in report.trace.files_skipped
    }
    assert skipped["shared/broken.py"].reason.value == "syntax_error"
    assert all(
        "tests/" not in candidate.relative_file
        for candidate in report.candidates
    )
    assert all(candidate.source == FindingSource.RULE for candidate in report.candidates)
    assert all(
        candidate.source != FindingSource.LLM_INDEPENDENT
        for candidate in report.candidates
    )


def test_rule_scan_is_deterministic_and_ids_are_stable():
    first = _scan(RuleId.R001_MUTABLE_DEFAULT, "r001")
    second = _scan(RuleId.R001_MUTABLE_DEFAULT, "r001")

    assert _stable(first) == _stable(second)
    assert [candidate.finding_id for candidate in first.candidates] == [
        candidate.finding_id for candidate in second.candidates
    ]


def test_r002_flags_direct_none_deref_and_skips_guarded_paths():
    defects = AUDIT_ROOT / "r002" / "defects.py"
    report = _scan(RuleId.R002_NONE_DEREF_NO_GUARD, "r002")

    assert len(report.candidates) == 7
    assert report.trace.candidates_by_rule[RuleId.R002_NONE_DEREF_NO_GUARD] == 7
    assert all(candidate.source == FindingSource.RULE for candidate in report.candidates)
    candidates = _by_line(report)

    hit_markers = {
        "simple_deref": ("def simple_deref", "return x.value"),
        "falsy_guard": ("def falsy_guard", "return x.value"),
        "attr_store": ("def attr_store", "x.value = 1"),
        "subscript": ("def subscript", "return x[0]"),
        "called": ("def called", "return x()"),
        "conditional_none": ("def conditional_none", "return x.value if x is None"),
        "bool_and": ("def bool_and", "return x is None and x.value"),
    }
    for after, marker in hit_markers.values():
        candidate = candidates[line_of(defects, marker, after=after)]
        assert "assigned None on line" in candidate.message
        assert candidate.line_start == candidate.line_end

    safe_markers = {
        "guarded_if": ("def guarded_if", "return x.value"),
        "truthy_guard": ("def truthy_guard", "return x.value"),
        "reassigned": ("def reassigned", "return x.value"),
        "guarded_return": ("def guarded_return", "return x.value"),
        "branch_assign": ("def branch_assign", "return x.value"),
        "pass_through": ("def pass_through", "consume(x)"),
        "guarded_assert": ("def guarded_assert", "return x.value"),
        "guarded_else": ("def guarded_else", "return x.value"),
        "conditional_expr": ("def conditional_expr", "return x.value if x else None"),
        "safe_conditional_expr": (
            "def safe_conditional_expr",
            "return x.value if x is not None else None",
        ),
        "bool_or": ("def bool_or", "return x is None or x.value"),
        "cross_function": ("def inner", "return x.value"),
    }
    for after, marker in safe_markers.values():
        assert line_of(defects, marker, after=after) not in candidates


def test_r005_flags_unmanaged_resources_and_respects_with_and_shadowing():
    defects = AUDIT_ROOT / "r005" / "defects.py"
    report = _scan(RuleId.R005_RESOURCE_NO_WITH, "r005")

    assert len(report.candidates) == 17
    assert report.trace.candidates_by_rule[RuleId.R005_RESOURCE_NO_WITH] == 17
    assert all(candidate.source == FindingSource.RULE for candidate in report.candidates)
    candidates = _by_line(report)

    hit_markers = {
        "bare_open": ("def bare_open", 'handle = open("data.txt")'),
        "body_call": ("def body_call", 'handle = open("data.txt")'),
        "sqlite_call": ("def sqlite_call", 'return sqlite3.connect("db.sqlite")'),
        "socket_call": ("def socket_call", "return socket.socket()"),
        "socket_create": ("def socket_create", "return create_connection("),
        "urlopen_call": ("def urlopen_call", "return urllib.request.urlopen("),
        "zip_call": ("def zip_call", 'return zipfile.ZipFile("archive.zip")'),
        "temp_call": ("def temp_call", "return tempfile.NamedTemporaryFile()"),
        "gzip_call": ("def gzip_call", 'return gzip.open("data.gz")'),
        "codecs_call": ("def codecs_call", 'return codecs.open("data.txt")'),
        "bz2_call": ("def bz2_call", 'return bz2.open("data.txt")'),
        "lzma_call": ("def lzma_call", 'return lzma.open("data.txt")'),
        "tarfile_call": ("def tarfile_call", 'return tarfile.open("data.tar")'),
        "alias_call": ("def alias_call", 'return db.connect("db.sqlite")'),
        "from_import_call": (
            "def from_import_call",
            'return sqlite_connect("db.sqlite")',
        ),
        "io_alias_call": ("def io_alias_call", 'return io_open("data.txt")'),
    }
    for after, marker in hit_markers.values():
        candidate = candidates[line_of(defects, marker, after=after)]
        assert "not managed by a with block" in candidate.message

    default_line = line_of(defects, 'def default_call(handle=open("data.txt"))')
    assert default_line in candidates
    assert "builtins.open" in candidates[default_line].message

    safe_markers = {
        "with_open": ("def with_open", 'with open("data.txt") as handle:'),
        "closing_open": ("def closing_open", 'with io.open("data.txt") as handle:'),
        "nested_context": (
            "def nested_context",
            'with contextlib.closing(open("data.txt")) as handle:',
        ),
        "sqlite_with": (
            "def sqlite_with",
            'with sqlite3.connect("db.sqlite") as connection:',
        ),
        "shadowed_parameter": ("def shadowed_parameter", 'return open("data.txt")'),
        "shadowed_local": ("def shadowed_local", 'return open("data.txt")'),
        "shadowed_import": (
            "def shadowed_import",
            'return sqlite3.connect("db.sqlite")',
        ),
        "other_module_call": ("def other_module_call", "return json.dumps({})"),
    }
    for after, marker in safe_markers.values():
        assert line_of(defects, marker, after=after) not in candidates

    messages = "\n".join(candidate.message for candidate in report.candidates)
    for qualified in (
        "builtins.open",
        "io.open",
        "sqlite3.connect",
        "socket.socket",
        "socket.create_connection",
        "urllib.request.urlopen",
        "zipfile.ZipFile",
        "tempfile.NamedTemporaryFile",
        "gzip.open",
        "codecs.open",
        "bz2.open",
        "lzma.open",
        "tarfile.open",
    ):
        assert qualified in messages
