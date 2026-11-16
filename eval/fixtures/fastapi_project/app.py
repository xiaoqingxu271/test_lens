"""Vendored FastAPI-style fixture with known defects and false positives.

The decorators use a local stub so the deterministic evaluation baseline runs
offline without installing a framework. The route shape mirrors FastAPI.
"""

import sqlite3
from contextlib import ExitStack


class Stub:
    def get(self, path, **kwargs):
        return lambda function: function


app = Stub()


@app.get("/classify")
def classify(value):
    if value > 0:
        return 1
    return 0


def collect(items=[]):
    return items


def load_state():
    state = None
    return state.value


def early_return(flag):
    if flag:
        return "done"
        unused = True
    return "pending"


def swallow():
    try:
        int("not a number")
    except:
        pass


def read_config():
    config_handle = open("config.ini", encoding="utf-8")
    return config_handle.read()


def read_with_finally():
    finally_handle = open("data.txt", encoding="utf-8")
    try:
        return finally_handle.read()
    finally:
        finally_handle.close()


def read_with_stack():
    with ExitStack() as stack:
        stack_handle = stack.enter_context(open("data.txt", encoding="utf-8"))
        return stack_handle.read()


def read_rows():
    with sqlite3.connect(":memory:") as connection:
        return connection.execute("select 1").fetchall()
