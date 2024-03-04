
#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2019 OzzieIsaacs, cervinko, jkrehm, bodybybuddha, ok11,
#                            andy29485, idalin, Kyosfonica, wuqi, Kennyl, lemmsh,
#                            falgh1, grunjol, csitko, ytils, xybydy, trasba, vrabe,
#                            ruben-herold, marblepebble, JackED42, SiphonSquirrel,
#                            apetresc, nanu-c, mutschler
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

import platform
import sqlite3
import sys
from collections import OrderedDict
from importlib import metadata

import flask
import flask_login
import jinja2
from flask_babel import gettext as _

from . import calibre_db, constants, converter, db, uploader
from .render_template import render_title_template

about = flask.Blueprint("about", __name__)

modules = OrderedDict()
for dep in (s.split(" ")[0] for s in (metadata.requires(__package__) or [])):
    modules[dep] = metadata.version(dep)
modules["Jinja2"] = jinja2.__version__
modules["pySqlite"] = sqlite3.version
modules["SQLite"] = sqlite3.sqlite_version


def collect_stats():
    calibre_web_version = constants.VERSION_STRING

    if getattr(sys, "frozen", False):
        calibre_web_version += " - Exe-Version"
    elif constants.HOME_CONFIG:
        calibre_web_version += " - pyPi"

    _VERSIONS = {"Calibre Web": calibre_web_version}
    _VERSIONS.update(OrderedDict(
        Python=sys.version,
        Platform="{0[0]} {0[2]} {0[3]} {0[4]} {0[5]}".format(platform.uname()),
    ))
    _VERSIONS.update(uploader.get_magick_version())
    _VERSIONS["Unrar"] = converter.get_unrar_version()
    _VERSIONS["Ebook converter"] = converter.get_calibre_version()
    _VERSIONS["Kepubify"] = converter.get_kepubify_version()
    _VERSIONS.update(modules)
    return _VERSIONS


@about.route("/stats")
@flask_login.login_required
def stats():
    counter = calibre_db.session.query(db.Books).count()
    authors = calibre_db.session.query(db.Authors).count()
    categories = calibre_db.session.query(db.Tags).count()
    series = calibre_db.session.query(db.Series).count()
    return render_title_template("stats.html", bookcounter=counter, authorcounter=authors, versions=collect_stats(),
                                 categorycounter=categories, seriecounter=series, title=_("Statistics"), page="stat")
