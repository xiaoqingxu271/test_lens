"""Vendored Flask-style fixture with known defects and false positives.

The decorators use a local stub so the deterministic evaluation baseline runs
offline without installing a framework. The route shape mirrors Flask.
"""

import sqlite3
from contextlib import ExitStack


class Stub:
    def route(self, rule=None, **kwargs):
        return lambda function: function


bp = Stub()


@bp.route("/score")
def score(value):
    if value >= 10 and value:
        return 2
    return 0


def register(events=[]):
    return events


def lookup():
    record = None
    return record["value"]


def choose():
    return "ok"
    return "never"


def swallow():
    try:
        int("not a number")
    except:
        pass


def read_settings():
    settings_handle = open("settings.ini", encoding="utf-8")
    return settings_handle.read()


def read_with_finally():
    finally_handle = open("settings.ini", encoding="utf-8")
    try:
        return finally_handle.read()
    finally:
        finally_handle.close()


def read_with_stack():
    with ExitStack() as stack:
        stack_handle = stack.enter_context(open("settings.ini", encoding="utf-8"))
        return stack_handle.read()


def read_rows():
    with sqlite3.connect(":memory:") as connection:
        return connection.execute("select 1").fetchall()
