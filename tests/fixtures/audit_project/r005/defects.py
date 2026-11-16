import bz2
import codecs
import gzip
import io
import lzma
import socket
import sqlite3
import tarfile
import tempfile
import urllib.request
import zipfile
from io import open as io_open
from socket import create_connection
from sqlite3 import connect as sqlite_connect


def bare_open():
    handle = open("data.txt")
    return handle


def with_open():
    with open("data.txt") as handle:
        return handle.read()


def closing_open():
    with io.open("data.txt") as handle:
        return handle.read()


def nested_context():
    import contextlib
    with contextlib.closing(open("data.txt")) as handle:
        return handle.read()


def body_call():
    import contextlib
    with contextlib.suppress(Exception):
        handle = open("data.txt")
    return None


def sqlite_call():
    return sqlite3.connect("db.sqlite")


def sqlite_with():
    with sqlite3.connect("db.sqlite") as connection:
        return connection


def socket_call():
    return socket.socket()


def socket_create():
    return create_connection(("localhost", 80))


def urlopen_call():
    return urllib.request.urlopen("http://example.com")


def zip_call():
    return zipfile.ZipFile("archive.zip")


def temp_call():
    return tempfile.NamedTemporaryFile()


def gzip_call():
    return gzip.open("data.gz")


def codecs_call():
    return codecs.open("data.txt")


def bz2_call():
    return bz2.open("data.txt")


def lzma_call():
    return lzma.open("data.txt")


def tarfile_call():
    return tarfile.open("data.tar")


def alias_call():
    import sqlite3 as db
    return db.connect("db.sqlite")


def from_import_call():
    return sqlite_connect("db.sqlite")


def io_alias_call():
    return io_open("data.txt")


def shadowed_parameter(open):
    return open("data.txt")


def shadowed_local():
    open = object()
    return open("data.txt")


def shadowed_import():
    import sqlite3
    sqlite3 = object()
    return sqlite3.connect("db.sqlite")


def other_module_call():
    import json
    return json.dumps({})


def default_call(handle=open("data.txt")):
    return handle
