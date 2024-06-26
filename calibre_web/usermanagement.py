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

from functools import wraps

from flask import Response, request
from flask_login import login_required, login_user
from sqlalchemy.sql.expression import func
from werkzeug.security import check_password_hash

from . import lm, logger, ub
from .app import limiter
from .config_sql import CONFIG

log = logger.create()


def login_required_if_no_ano(func):
    @wraps(func)
    def decorated_view(*args, **kwargs):
        if CONFIG.config_anonbrowse == 1:
            return func(*args, **kwargs)
        return login_required(func)(*args, **kwargs)

    return decorated_view


def requires_basic_auth_if_no_ano(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.type != "basic":
            if CONFIG.config_anonbrowse != 1:
                user = load_user_from_reverse_proxy_header(request)
                if user:
                    return f(*args, **kwargs)
                return _authenticate()
            else:
                return f(*args, **kwargs)
        user = _load_user_from_auth_header(auth.username, auth.password)
        if not user:
            return _authenticate()
        return f(*args, **kwargs)

    return decorated


def _load_user_from_auth_header(username, password):
    limiter.check()
    user = _fetch_user_by_name(username)
    if bool(user and check_password_hash(str(user.password), password)) and user.name != "Guest":
        [limiter.limiter.storage.clear(k.key) for k in limiter.current_limits]
        login_user(user)
        return user
    else:
        ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        log.warning('OPDS Login failed for user "%s" IP-address: %s', username, ip_address)
        return None


def _authenticate():
    return Response(
        "Could not verify your access level for that URL.\n" "You have to login with proper credentials",
        401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'},
    )


def _fetch_user_by_name(username):
    return ub.session.query(ub.User).filter(func.lower(ub.User.name) == username.lower()).first()


@lm.user_loader
def load_user(user_id):
    return ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()


@lm.request_loader
def load_user_from_reverse_proxy_header(req):
    if CONFIG.config_allow_reverse_proxy_header_login:
        rp_header_name = CONFIG.config_reverse_proxy_login_header_name
        if rp_header_name:
            rp_header_username = req.headers.get(rp_header_name)
            if rp_header_username:
                user = _fetch_user_by_name(rp_header_username)
                if user:
                    [limiter.limiter.storage.clear(k.key) for k in limiter.current_limits]
                    login_user(user)
                    return user
    return None
