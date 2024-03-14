import mimetypes
import os
import sys
from pathlib import Path

from flask import Flask, request
from flask_principal import Principal

from . import cache_buster, config_sql, db, logger, ub
from .about import about
from .admin import admi
from .babel import babel, get_locale
from .cli import cli_param
from .config_sql import CONFIG
from .db import calibre_db
from .editbooks import editbook
from .error_handler import init_errorhandler
from .jinjia import jinjia
from .MyLoginManager import MyLoginManager
from .opds import opds
from .remotelogin import remotelogin
from .reverseproxy import ReverseProxied
from .search import search
from .search_metadata import meta
from .server import web_server
from .shelf import shelf
from .tasks_status import tasks
from .updater import updater_thread
from .web import web

try:
    from flask_limiter import Limiter

    limiter_present = True
except ImportError:
    limiter_present = False
try:
    from flask_wtf.csrf import CSRFProtect

    wtf_present = True
except ImportError:
    wtf_present = False


mimetypes.init()
mimetypes.add_type("application/xhtml+xml", ".xhtml")
mimetypes.add_type("application/epub+zip", ".epub")
mimetypes.add_type("application/fb2+zip", ".fb2")
mimetypes.add_type("application/x-mobipocket-ebook", ".mobi")
mimetypes.add_type("application/x-mobipocket-ebook", ".prc")
mimetypes.add_type("application/vnd.amazon.ebook", ".azw")
mimetypes.add_type("application/x-mobi8-ebook", ".azw3")
mimetypes.add_type("application/x-cbr", ".cbr")
mimetypes.add_type("application/x-cbz", ".cbz")
mimetypes.add_type("application/x-cbt", ".cbt")
mimetypes.add_type("application/x-cb7", ".cb7")
mimetypes.add_type("image/vnd.djv", ".djv")
mimetypes.add_type("application/mpeg", ".mpeg")
mimetypes.add_type("application/mpeg", ".mp3")
mimetypes.add_type("application/mp4", ".m4a")
mimetypes.add_type("application/mp4", ".m4b")
mimetypes.add_type("application/ogg", ".ogg")
mimetypes.add_type("application/ogg", ".oga")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript; charset=UTF-8", ".js")

log = logger.create()

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SAMESITE="Lax",  # will be available in flask-login 0.5.1 earliest
    WTF_CSRF_SSL_STRICT=False,
)

lm = MyLoginManager()

csrf = CSRFProtect() if wtf_present else None

if limiter_present:
    limiter = Limiter(key_func=True, headers_enabled=True, auto_check=False, swallow_errors=True)
else:
    limiter = None


def create_app():
    if csrf:
        csrf.init_app(app)

    cli_param.init()

    ub.init_db(cli_param.settings_path)
    settings_dir = Path(cli_param.settings_path).parent
    encrypt_key, error = config_sql.get_encryption_key(settings_dir)

    config_sql.load_configuration(ub.session, encrypt_key)
    CONFIG.init_config(ub.session, encrypt_key, cli_param)

    if error:
        log.error(error)

    ub.password_change(cli_param.user_credentials)

    if not limiter:
        log.info(
            '*** "flask-limiter" is needed for calibre-web to run. '
            'Please install it using pip: "pip install flask-limiter" ***'
        )
        print(
            '*** "flask-limiter" is needed for calibre-web to run. '
            'Please install it using pip: "pip install flask-limiter" ***'
        )
        web_server.stop(restart=True)
        sys.exit(8)
    if not wtf_present:
        log.info(
            '*** "flask-WTF" is needed for calibre-web to run. '
            'Please install it using pip: "pip install flask-WTF" ***'
        )
        print(
            '*** "flask-WTF" is needed for calibre-web to run. '
            'Please install it using pip: "pip install flask-WTF" ***'
        )
        web_server.stop(restart=True)
        sys.exit(7)

    lm.login_view = "web.login"
    lm.anonymous_user = ub.Anonymous
    lm.session_protection = "strong" if CONFIG.config_session == 1 else "basic"

    db.CalibreDB.update_config(CONFIG)
    db.CalibreDB.setup_db(CONFIG.config_calibre_dir, cli_param.settings_path)
    calibre_db.init_db()

    updater_thread.init_updater(CONFIG, web_server)
    # Perform dry run of updater and exit afterwards
    if cli_param.dry_run:
        updater_thread.dry_run()
        sys.exit(0)
    updater_thread.start()

    app.wsgi_app = ReverseProxied(app.wsgi_app)

    if os.environ.get("FLASK_DEBUG"):
        cache_buster.init_cache_busting(app)
    log.info("Starting Calibre Web...")
    Principal(app)
    lm.init_app(app)
    app.secret_key = os.getenv("SECRET_KEY", config_sql.get_flask_session_key(ub.session))

    web_server.init_app(app, CONFIG)
    if hasattr(babel, "localeselector"):
        babel.init_app(app)
        babel.localeselector(get_locale)
    else:
        babel.init_app(app, locale_selector=get_locale)

    CONFIG.store_calibre_uuid(calibre_db, db.Library_Id)
    # Configure rate limiter
    app.config.update(RATELIMIT_ENABLED=CONFIG.config_ratelimiter)
    limiter.init_app(app)

    # Register scheduled tasks
    from .schedule import register_scheduled_tasks, register_startup_tasks

    register_scheduled_tasks(CONFIG.schedule_reconnect)
    register_startup_tasks()

    return app


def request_username():
    return request.authorization.username


def main():
    app = create_app()
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
