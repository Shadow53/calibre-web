import sys
import sqlite3

import jinja2
from calibre_web import constants
from calibre_web.about import collect_stats


def test_collect_stats():
    versions = collect_stats()
    assert versions["Calibre Web"].startswith(constants.VERSION_STRING)
    assert versions["Python"] == sys.version
    assert versions["Jinja2"] == jinja2.__version__
    assert versions["pySqlite"] == sqlite3.version
    assert versions["SQLite"] == sqlite3.sqlite_version
