#!/use/bin/env python3
import sys

from flask import request

from . import create_app, limiter
from .jinjia import jinjia
from .remotelogin import remotelogin


def request_username():
    return request.authorization.username


def main():
    app = create_app()

    from .about import about
    from .admin import admi
    from .editbooks import editbook
    from .error_handler import init_errorhandler
    from .opds import opds
    from .search import search
    from .search_metadata import meta
    from .shelf import shelf
    from .tasks_status import tasks
    from .web import web
    try:
        from flask_limiter.util import get_remote_address

        from .kobo import get_kobo_activated, kobo
        from .kobo_auth import kobo_auth
        kobo_available = get_kobo_activated()
    except (ImportError, AttributeError):  # Catch also error for not installed flask-WTF (missing csrf decorator)
        kobo_available = False

    try:
        from .oauth_bb import oauth
        oauth_available = True
    except ImportError:
        oauth_available = False

    from . import web_server
    init_errorhandler()

    app.register_blueprint(search)
    app.register_blueprint(tasks)
    app.register_blueprint(web)
    app.register_blueprint(opds)
    limiter.limit("3/minute", key_func=request_username)(opds)
    app.register_blueprint(jinjia)
    app.register_blueprint(about)
    app.register_blueprint(shelf)
    app.register_blueprint(admi)
    app.register_blueprint(remotelogin)
    app.register_blueprint(meta)
    app.register_blueprint(editbook)
    if kobo_available:
        app.register_blueprint(kobo)
        app.register_blueprint(kobo_auth)
        limiter.limit("3/minute", key_func=get_remote_address)(kobo)
    if oauth_available:
        app.register_blueprint(oauth)
    success = web_server.start()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
