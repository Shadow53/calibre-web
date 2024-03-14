
#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2020 OzzieIsaacs
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

import traceback

from flask import render_template
from werkzeug.exceptions import default_exceptions

try:
    from werkzeug.exceptions import FailedDependency
except ImportError:
    pass

from . import app, logger
from .config_sql import CONFIG

log = logger.create()

# custom error page
def error_http(error):
    return render_template("http_error.html",
                           error_code=f"Error {error.code}",
                           error_name=error.name,
                           issue=False,
                           unconfigured=not CONFIG.db_configured,
                           instance=CONFIG.config_calibre_web_title
                           ), error.code


def internal_error(error):
    return render_template("http_error.html",
                           error_code="500 Internal Server Error",
                           error_name="The server encountered an internal error and was unable to complete your "
                                      "request. There is an error in the application.",
                           issue=True,
                           unconfigured=False,
                           error_stack=traceback.format_exc().split("\n"),
                           instance=CONFIG.config_calibre_web_title
                           ), 500

def init_errorhandler() -> None:
    # http error handling
    for ex in default_exceptions:
        if ex < 500:
            app.register_error_handler(ex, error_http)
        elif ex == 500:
            app.register_error_handler(ex, internal_error)

