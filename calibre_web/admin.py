
#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2019 OzzieIsaacs, cervinko, jkrehm, bodybybuddha, ok11,
#                            andy29485, idalin, Kyosfonica, wuqi, Kennyl, lemmsh,
#                            falgh1, grunjol, csitko, ytils, xybydy, trasba, vrabe,
#                            ruben-herold, marblepebble, JackED42, SiphonSquirrel,
#                            apetresc, nanu-c, mutschler, GammaC0de, vuolter
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

import contextlib
import json
import operator
import os
import re
import string
import sys
import time
from datetime import datetime, timedelta
from datetime import time as datetime_time
from functools import wraps

from flask import Blueprint, Response, abort, flash, g, make_response, redirect, request, send_from_directory, url_for
from flask import session as flask_session
from flask_babel import format_datetime, format_time, format_timedelta, get_locale
from flask_babel import gettext as _
from flask_login import current_user, login_required, logout_user
from markupsafe import Markup
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError, InvalidRequestError, OperationalError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.expression import func, or_, text

from . import (
    constants,
    db,
    debug_info,
    helper,
    kobo_sync_status,
    logger,
    schedule,
    services,
    ub,
)
from .app import updater_thread, web_server
from .cli import cli_param
from .db import calibre_db
from .babel import get_available_locale, get_available_translations, get_user_locale_language
from .config_sql import CONFIG
from .helper import (
    check_email,
    check_username,
    check_valid_domain,
    generate_password_hash,
    get_calibre_binarypath,
    reset_password,
    send_test_mail,
    valid_email,
)
from .render_template import get_sidebar_config, render_title_template
from .services.background_scheduler import use_APScheduler
from .services.worker import WorkerThread

log = logger.create()

feature_support = {
    "kobo": bool(services.kobo),
    "updater": constants.UPDATER_AVAILABLE,
    "scheduler": use_APScheduler,
}

try:
    import rarfile  # pylint: disable=unused-import

    feature_support["rar"] = True
except (ImportError, SyntaxError):
    feature_support["rar"] = False

try:
    from .oauth_bb import oauth_check, oauthblueprints

    feature_support["oauth"] = True
except ImportError as err:
    log.debug("Cannot import Flask-Dance, login with Oauth will not work: %s", err)
    feature_support["oauth"] = False
    oauthblueprints = []
    oauth_check = {}

admi = Blueprint("admin", __name__)


def admin_required(f):
    """Checks if current_user.role == 1."""

    @wraps(f)
    def inner(*args, **kwargs):
        if current_user.role_admin():
            return f(*args, **kwargs)
        abort(403)
        return None

    return inner


@admi.before_app_request
def before_request():
    try:
        if not ub.check_user_session(current_user.id,
                                     flask_session.get("_id")) and "opds" not in request.path \
          and CONFIG.config_session == 1:
            logout_user()
    except AttributeError:
        pass    # ? fails on requesting /ajax/emailstat during restart ?
    g.constants = constants
    g.allow_registration = CONFIG.config_public_reg
    g.allow_anonymous = CONFIG.config_anonbrowse
    g.allow_upload = CONFIG.config_uploading
    g.current_theme = CONFIG.config_theme
    g.config_authors_max = CONFIG.config_authors_max
    if "/static/" not in request.path and not CONFIG.db_configured and \
        request.endpoint not in ("admin.ajax_db_config",
                                 "admin.simulatedbchange",
                                 "admin.db_configuration",
                                 "web.login",
                                 "web.login_post",
                                 "web.logout",
                                 "admin.load_dialogtexts",
                                 "admin.ajax_pathchooser"):
        return redirect(url_for("admin.db_configuration"))
    return None


@admi.route("/admin")
@login_required
def admin_forbidden() -> None:
    abort(403)


@admi.route("/shutdown", methods=["POST"])
@login_required
@admin_required
def shutdown():
    task = request.get_json().get("parameter", -1)
    show_text = {}
    if task in (0, 1):  # valid commandos received
        # close all database connections
        calibre_db.dispose()
        ub.dispose()

        if task == 0:
            show_text["text"] = _("Server restarted, please reload page.")
        else:
            show_text["text"] = _("Performing Server shutdown, please close window.")
        # stop gevent/tornado server
        web_server.stop(task == 0)
        return json.dumps(show_text)

    if task == 2:
        log.warning("reconnecting to calibre database")
        calibre_db.reconnect_db(config, ub.app_DB_path)
        show_text["text"] = _("Success! Database Reconnected")
        return json.dumps(show_text)

    show_text["text"] = _("Unknown command")
    return json.dumps(show_text), 400


@admi.route("/metadata_backup", methods=["POST"])
@login_required
@admin_required
def queue_metadata_backup():
    show_text = {}
    log.warning("Queuing all books for metadata backup")
    helper.set_all_metadata_dirty()
    show_text["text"] = _("Success! Books queued for Metadata Backup, please check Tasks for result")
    return json.dumps(show_text)


# method is available without login and not protected by CSRF to make it easy reachable, is per default switched off
# needed for docker applications, as changes on metadata.db from host are not visible to application
@admi.route("/reconnect", methods=["GET"])
def reconnect():
    if cli_param.reconnect_enable:
        calibre_db.reconnect_db(config, ub.app_DB_path)
        return json.dumps({})
    else:
        log.debug("'/reconnect' was accessed but is not enabled")
        abort(404)
        return None


@admi.route("/ajax/updateThumbnails", methods=["POST"])
@admin_required
@login_required
def update_thumbnails() -> str:
    content = CONFIG.get_scheduled_task_settings()
    if content["schedule_generate_book_covers"]:
        log.info("Update of Cover cache requested")
        helper.update_thumbnail_cache()
    return ""


@admi.route("/admin/view")
@login_required
@admin_required
def admin():
    version = updater_thread.get_current_version_info()
    if version is False:
        commit = _("Unknown")
    elif "datetime" in version:
        commit = version["datetime"]

        tz = timedelta(seconds=time.timezone if (time.localtime().tm_isdst == 0) else time.altzone)
        form_date = datetime.strptime(commit[:19], "%Y-%m-%dT%H:%M:%S")
        if len(commit) > 19:  # check if string has timezone
            if commit[19] == "+":
                form_date -= timedelta(hours=int(commit[20:22]), minutes=int(commit[23:]))
            elif commit[19] == "-":
                form_date += timedelta(hours=int(commit[20:22]), minutes=int(commit[23:]))
        commit = format_datetime(form_date - tz, format="short")
    else:
        commit = version["version"].replace("b", " Beta")

    all_user = ub.session.query(ub.User).all()
    # email_settings = mail_config.get_mail_settings()
    schedule_time = format_time(datetime_time(hour=config.schedule_start_time), format="short")
    t = timedelta(hours=config.schedule_duration // 60, minutes=config.schedule_duration % 60)
    schedule_duration = format_timedelta(t, threshold=.99)

    return render_title_template("admin.html", allUser=all_user, config=config, commit=commit,
                                 feature_support=feature_support, schedule_time=schedule_time,
                                 schedule_duration=schedule_duration,
                                 title=_("Admin page"), page="admin")


@admi.route("/admin/dbconfig", methods=["GET", "POST"])
@login_required
@admin_required
def db_configuration():
    if request.method == "POST":
        return _db_configuration_update_helper()
    return _db_configuration_result()


@admi.route("/admin/config", methods=["GET"])
@login_required
@admin_required
def configuration():
    return render_title_template("config_edit.html",
                                 config=config,
                                 provider=oauthblueprints,
                                 feature_support=feature_support,
                                 title=_("Basic Configuration"), page="config")


@admi.route("/admin/ajaxconfig", methods=["POST"])
@login_required
@admin_required
def ajax_config():
    return _configuration_update_helper()


@admi.route("/admin/ajaxdbconfig", methods=["POST"])
@login_required
@admin_required
def ajax_db_config():
    return _db_configuration_update_helper()


@admi.route("/admin/alive", methods=["GET"])
@login_required
@admin_required
def calibreweb_alive():
    return "", 200


@admi.route("/admin/viewconfig")
@login_required
@admin_required
def view_configuration():
    read_column = calibre_db.session.query(db.CustomColumns) \
        .filter(and_(db.CustomColumns.datatype == "bool", db.CustomColumns.mark_for_delete == 0)).all()
    restrict_columns = calibre_db.session.query(db.CustomColumns) \
        .filter(and_(db.CustomColumns.datatype == "text", db.CustomColumns.mark_for_delete == 0)).all()
    languages = calibre_db.speaking_language()
    translations = get_available_locale()
    return render_title_template("config_view_edit.html", conf=config, readColumns=read_column,
                                 restrictColumns=restrict_columns,
                                 languages=languages,
                                 translations=translations,
                                 title=_("UI Configuration"), page="uiconfig")


@admi.route("/admin/usertable")
@login_required
@admin_required
def edit_user_table():
    visibility = current_user.view_settings.get("useredit", {})
    languages = calibre_db.speaking_language()
    translations = get_available_locale()
    all_user = ub.session.query(ub.User)
    tags = calibre_db.session.query(db.Tags) \
        .join(db.books_tags_link) \
        .join(db.Books) \
        .filter(calibre_db.common_filters()) \
        .group_by(text("books_tags_link.tag")) \
        .order_by(db.Tags.name).all()
    if CONFIG.config_restricted_column:
        custom_values = calibre_db.session.query(db.cc_classes[config.config_restricted_column]).all()
    else:
        custom_values = []
    if not CONFIG.config_anonbrowse:
        all_user = all_user.filter(ub.User.role.op("&")(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)
    kobo_support = feature_support["kobo"] and CONFIG.config_kobo_sync
    return render_title_template("user_table.html",
                                 users=all_user.all(),
                                 tags=tags,
                                 custom_values=custom_values,
                                 translations=translations,
                                 languages=languages,
                                 visiblility=visibility,
                                 all_roles=constants.ALL_ROLES,
                                 kobo_support=kobo_support,
                                 sidebar_settings=constants.sidebar_settings,
                                 title=_("Edit Users"),
                                 page="usertable")


@admi.route("/ajax/listusers")
@login_required
@admin_required
def list_users():
    off = int(request.args.get("offset") or 0)
    limit = int(request.args.get("limit") or 10)
    search = request.args.get("search")
    sort = request.args.get("sort", "id")
    state = None
    if sort == "state":
        state = json.loads(request.args.get("state", "[]"))
    elif sort not in ub.User.__table__.columns:
        sort = "id"
    order = request.args.get("order", "").lower()

    if sort != "state" and order:
        order = text(sort + " " + order)
    elif not state:
        order = ub.User.id.asc()

    all_user = ub.session.query(ub.User)
    if not CONFIG.config_anonbrowse:
        all_user = all_user.filter(ub.User.role.op("&")(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)

    total_count = filtered_count = all_user.count()

    if search:
        all_user = all_user.filter(or_(func.lower(ub.User.name).ilike("%" + search + "%"),
                                       func.lower(ub.User.kindle_mail).ilike("%" + search + "%"),
                                       func.lower(ub.User.email).ilike("%" + search + "%")))
    if state:
        users = calibre_db.get_checkbox_sorted(all_user.all(), state, off, limit, request.args.get("order", "").lower())
    else:
        users = all_user.order_by(order).offset(off).limit(limit).all()
    if search:
        filtered_count = len(users)

    for user in users:
        if user.default_language == "all":
            user.default = _("All")
        else:
            user.default = get_user_locale_language(user.default_language)

    table_entries = {"totalNotFiltered": total_count, "total": filtered_count, "rows": users}
    js_list = json.dumps(table_entries, cls=db.AlchemyEncoder)
    response = make_response(js_list)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@admi.route("/ajax/deleteuser", methods=["POST"])
@login_required
@admin_required
def delete_user():
    user_ids = request.form.to_dict(flat=False)
    users = None
    message = ""
    if "userid[]" in user_ids:
        users = ub.session.query(ub.User).filter(ub.User.id.in_(user_ids["userid[]"])).all()
    elif "userid" in user_ids:
        users = ub.session.query(ub.User).filter(ub.User.id == user_ids["userid"][0]).all()
    count = 0
    errors = []
    success = []
    if not users:
        log.error("User not found")
        return Response(json.dumps({"type": "danger", "message": _("User not found")}), mimetype="application/json")
    for user in users:
        try:
            message = _delete_user(user)
            count += 1
        except Exception as ex:
            log.exception(ex)
            errors.append({"type": "danger", "message": str(ex)})

    if count == 1:
        log.info(f"User {user_ids} deleted")
        success = [{"type": "success", "message": message}]
    elif count > 1:
        log.info(f"Users {user_ids} deleted")
        success = [{"type": "success", "message": _("{} users deleted successfully").format(count)}]
    success.extend(errors)
    return Response(json.dumps(success), mimetype="application/json")


@admi.route("/ajax/getlocale")
@login_required
@admin_required
def table_get_locale():
    locale = get_available_locale()
    ret = []
    current_locale = get_locale()
    for loc in locale:
        ret.append({"value": str(loc), "text": loc.get_language_name(current_locale)})
    return json.dumps(ret)


@admi.route("/ajax/getdefaultlanguage")
@login_required
@admin_required
def table_get_default_lang():
    languages = calibre_db.speaking_language()
    ret = []
    ret.append({"value": "all", "text": _("Show All")})
    for lang in languages:
        ret.append({"value": lang.lang_code, "text": lang.name})
    return json.dumps(ret)


@admi.route("/ajax/editlistusers/<param>", methods=["POST"])
@login_required
@admin_required
def edit_list_user(param):
    vals = request.form.to_dict(flat=False)
    all_user = ub.session.query(ub.User)
    if not CONFIG.config_anonbrowse:
        all_user = all_user.filter(ub.User.role.op("&")(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)
    # only one user is posted
    if "pk" in vals:
        users = [all_user.filter(ub.User.id == vals["pk"][0]).one_or_none()]
    elif "pk[]" in vals:
        users = all_user.filter(ub.User.id.in_(vals["pk[]"])).all()
    else:
        return _("Malformed request"), 400
    if "field_index" in vals:
        vals["field_index"] = vals["field_index"][0]
    if "value" in vals:
        vals["value"] = vals["value"][0]
    elif "value[]" not in vals:
        return _("Malformed request"), 400
    for user in users:
        try:
            if param in ["denied_tags", "allowed_tags", "allowed_column_value", "denied_column_value"]:
                if "value[]" in vals:
                    setattr(user, param, prepare_tags(user, vals["action"][0], param, vals["value[]"]))
                else:
                    setattr(user, param, vals["value"].strip())
            else:
                vals["value"] = vals["value"].strip()
                if param == "name":
                    if user.name == "Guest":
                        raise Exception(_("Guest Name can't be changed"))
                    user.name = check_username(vals["value"])
                elif param == "email":
                    user.email = check_email(vals["value"])
                elif param == "kobo_only_shelves_sync":
                    user.kobo_only_shelves_sync = int(vals["value"] == "true")
                elif param == "kindle_mail":
                    user.kindle_mail = valid_email(vals["value"]) if vals["value"] else ""
                elif param.endswith("role"):
                    value = int(vals["field_index"])
                    if user.name == "Guest" and value in \
                        [constants.ROLE_ADMIN, constants.ROLE_PASSWD, constants.ROLE_EDIT_SHELFS]:
                        raise Exception(_("Guest can't have this role"))
                    # check for valid value, last on checks for power of 2 value
                    if value > 0 and value <= constants.ROLE_VIEWER and (value & value - 1 == 0 or value == 1):
                        if vals["value"] == "true":
                            user.role |= value
                        elif vals["value"] == "false":
                            if value == constants.ROLE_ADMIN:
                                if not ub.session.query(ub.User). \
                                    filter(ub.User.role.op("&")(constants.ROLE_ADMIN) == constants.ROLE_ADMIN,
                                           ub.User.id != user.id).count():
                                    return Response(
                                        json.dumps([{"type": "danger",
                                                     "message": _("No admin user remaining, can't remove admin role",
                                                                  nick=user.name)}]), mimetype="application/json")
                            user.role &= ~value
                        else:
                            raise Exception(_("Value has to be true or false"))
                    else:
                        raise Exception(_("Invalid role"))
                elif param.startswith("sidebar"):
                    value = int(vals["field_index"])
                    if user.name == "Guest" and value == constants.SIDEBAR_READ_AND_UNREAD:
                        raise Exception(_("Guest can't have this view"))
                    # check for valid value, last on checks for power of 2 value
                    if value > 0 and value <= constants.SIDEBAR_LIST and (value & value - 1 == 0 or value == 1):
                        if vals["value"] == "true":
                            user.sidebar_view |= value
                        elif vals["value"] == "false":
                            user.sidebar_view &= ~value
                        else:
                            raise Exception(_("Value has to be true or false"))
                    else:
                        raise Exception(_("Invalid view"))
                elif param == "locale":
                    if user.name == "Guest":
                        raise Exception(_("Guest's Locale is determined automatically and can't be set"))
                    if vals["value"] in get_available_translations():
                        user.locale = vals["value"]
                    else:
                        raise Exception(_("No Valid Locale Given"))
                elif param == "default_language":
                    languages = calibre_db.session.query(db.Languages) \
                        .join(db.books_languages_link) \
                        .join(db.Books) \
                        .filter(calibre_db.common_filters()) \
                        .group_by(text("books_languages_link.lang_code")).all()
                    lang_codes = [lang.lang_code for lang in languages] + ["all"]
                    if vals["value"] in lang_codes:
                        user.default_language = vals["value"]
                    else:
                        raise Exception(_("No Valid Book Language Given"))
                else:
                    return _("Parameter not found"), 400
        except Exception as ex:
            log.error_or_exception(ex)
            return str(ex), 400
    ub.session_commit()
    return ""


@admi.route("/ajax/user_table_settings", methods=["POST"])
@login_required
@admin_required
def update_table_settings():
    current_user.view_settings["useredit"] = json.loads(request.data)
    try:
        with contextlib.suppress(AttributeError):
            flag_modified(current_user, "view_settings")
        ub.session.commit()
    except (InvalidRequestError, OperationalError):
        log.exception(f"Invalid request received: {request}")
        return "Invalid request", 400
    return ""


@admi.route("/admin/viewconfig", methods=["POST"])
@login_required
@admin_required
def update_view_configuration():
    to_save = request.form.to_dict()

    _config_string(to_save, "config_calibre_web_title")
    _config_string(to_save, "config_columns_to_ignore")
    if _config_string(to_save, "config_title_regex"):
        calibre_db.update_title_sort(config)

    if not check_valid_read_column(to_save.get("config_read_column", "0")):
        flash(_("Invalid Read Column"), category="error")
        log.debug("Invalid Read column")
        return view_configuration()
    _config_int(to_save, "config_read_column")

    if not check_valid_restricted_column(to_save.get("config_restricted_column", "0")):
        flash(_("Invalid Restricted Column"), category="error")
        log.debug("Invalid Restricted Column")
        return view_configuration()
    _config_int(to_save, "config_restricted_column")

    _config_int(to_save, "config_theme")
    _config_int(to_save, "config_random_books")
    _config_int(to_save, "config_books_per_page")
    _config_int(to_save, "config_authors_max")
    _config_string(to_save, "config_default_language")
    _config_string(to_save, "config_default_locale")

    CONFIG.config_default_role = constants.selected_roles(to_save)
    CONFIG.config_default_role &= ~constants.ROLE_ANONYMOUS

    CONFIG.config_default_show = sum(int(k[5:]) for k in to_save if k.startswith("show_"))
    if "Show_detail_random" in to_save:
        CONFIG.config_default_show |= constants.DETAIL_RANDOM

    CONFIG.save()
    flash(_("Calibre-Web configuration updated"), category="success")
    log.debug("Calibre-Web configuration updated")
    before_request()

    return view_configuration()


@admi.route("/ajax/loaddialogtexts/<element_id>", methods=["POST"])
@login_required
def load_dialogtexts(element_id):
    texts = {"header": "", "main": "", "valid": 1}
    if element_id == "config_delete_kobo_token":
        texts["main"] = _("Do you really want to delete the Kobo Token?")
    elif element_id == "btndeletedomain":
        texts["main"] = _("Do you really want to delete this domain?")
    elif element_id == "btndeluser":
        texts["main"] = _("Do you really want to delete this user?")
    elif element_id == "delete_shelf":
        texts["main"] = _("Are you sure you want to delete this shelf?")
    elif element_id == "select_locale":
        texts["main"] = _("Are you sure you want to change locales of selected user(s)?")
    elif element_id == "select_default_language":
        texts["main"] = _("Are you sure you want to change visible book languages for selected user(s)?")
    elif element_id == "role":
        texts["main"] = _("Are you sure you want to change the selected role for the selected user(s)?")
    elif element_id == "restrictions":
        texts["main"] = _("Are you sure you want to change the selected restrictions for the selected user(s)?")
    elif element_id == "sidebar_view":
        texts["main"] = _("Are you sure you want to change the selected visibility restrictions "
                          "for the selected user(s)?")
    elif element_id == "kobo_only_shelves_sync":
        texts["main"] = _("Are you sure you want to change shelf sync behavior for the selected user(s)?")
    elif element_id == "db_submit":
        texts["main"] = _("Are you sure you want to change Calibre library location?")
    elif element_id == "admin_refresh_cover_cache":
        texts["main"] = _("Calibre-Web will search for updated Covers "
                          "and update Cover Thumbnails, this may take a while?")
    elif element_id == "btnfullsync":
        texts["main"] = _("Are you sure you want delete Calibre-Web's sync database "
                          "to force a full sync with your Kobo Reader?")
    return json.dumps(texts)


@admi.route("/ajax/editdomain/<int:allow>", methods=["POST"])
@login_required
@admin_required
def edit_domain(allow):
    # POST /post
    # name:  'username',  //name of field (column in db)
    # pk:    1            //primary key (record id)
    # value: 'superuser!' //new value
    vals = request.form.to_dict()
    answer = ub.session.query(ub.Registration).filter(ub.Registration.id == vals["pk"]).first()
    answer.domain = vals["value"].replace("*", "%").replace("?", "_").lower()
    return ub.session_commit(f"Registering Domains edited {answer.domain}")


@admi.route("/ajax/adddomain/<int:allow>", methods=["POST"])
@login_required
@admin_required
def add_domain(allow) -> str:
    domain_name = request.form.to_dict()["domainname"].replace("*", "%").replace("?", "_").lower()
    check = ub.session.query(ub.Registration).filter(ub.Registration.domain == domain_name) \
        .filter(ub.Registration.allow == allow).first()
    if not check:
        new_domain = ub.Registration(domain=domain_name, allow=allow)
        ub.session.add(new_domain)
        ub.session_commit(f"Registering Domains added {domain_name}")
    return ""


@admi.route("/ajax/deletedomain", methods=["POST"])
@login_required
@admin_required
def delete_domain() -> str:
    try:
        domain_id = request.form.to_dict()["domainid"].replace("*", "%").replace("?", "_").lower()
        ub.session.query(ub.Registration).filter(ub.Registration.id == domain_id).delete()
        ub.session_commit(f"Registering Domains deleted {domain_id}")
        # If last domain was deleted, add all domains by default
        if not ub.session.query(ub.Registration).filter(ub.Registration.allow == 1).count():
            new_domain = ub.Registration(domain="%.%", allow=1)
            ub.session.add(new_domain)
            ub.session_commit("Last Registering Domain deleted, added *.* as default")
    except KeyError:
        pass
    return ""


@admi.route("/ajax/domainlist/<int:allow>")
@login_required
@admin_required
def list_domain(allow):
    answer = ub.session.query(ub.Registration).filter(ub.Registration.allow == allow).all()
    json_dumps = json.dumps([{"domain": r.domain.replace("%", "*").replace("_", "?"), "id": r.id} for r in answer])
    js = json.dumps(json_dumps.replace('"', "'")).lstrip('"').strip('"')
    response = make_response(js.replace("'", '"'))
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@admi.route("/ajax/editrestriction/<int:res_type>", defaults={"user_id": 0}, methods=["POST"])
@admi.route("/ajax/editrestriction/<int:res_type>/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def edit_restriction(res_type, user_id) -> str:
    element = request.form.to_dict()
    if element["id"].startswith("a"):
        if res_type == 0:  # Tags as template
            elementlist = CONFIG.list_allowed_tags()
            elementlist[int(element["id"][1:])] = element["Element"]
            CONFIG.config_allowed_tags = ",".join(elementlist)
            CONFIG.save()
        if res_type == 1:  # CustomC
            elementlist = CONFIG.list_allowed_column_values()
            elementlist[int(element["id"][1:])] = element["Element"]
            CONFIG.config_allowed_column_value = ",".join(elementlist)
            CONFIG.save()
        if res_type == 2:  # Tags per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_allowed_tags()
            elementlist[int(element["id"][1:])] = element["Element"]
            usr.allowed_tags = ",".join(elementlist)
            ub.session_commit(f"Changed allowed tags of user {usr.name} to {usr.allowed_tags}")
        if res_type == 3:  # CColumn per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_allowed_column_values()
            elementlist[int(element["id"][1:])] = element["Element"]
            usr.allowed_column_value = ",".join(elementlist)
            ub.session_commit(f"Changed allowed columns of user {usr.name} to {usr.allowed_column_value}")
    if element["id"].startswith("d"):
        if res_type == 0:  # Tags as template
            elementlist = CONFIG.list_denied_tags()
            elementlist[int(element["id"][1:])] = element["Element"]
            CONFIG.config_denied_tags = ",".join(elementlist)
            CONFIG.save()
        if res_type == 1:  # CustomC
            elementlist = CONFIG.list_denied_column_values()
            elementlist[int(element["id"][1:])] = element["Element"]
            CONFIG.config_denied_column_value = ",".join(elementlist)
            CONFIG.save()
        if res_type == 2:  # Tags per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_denied_tags()
            elementlist[int(element["id"][1:])] = element["Element"]
            usr.denied_tags = ",".join(elementlist)
            ub.session_commit(f"Changed denied tags of user {usr.name} to {usr.denied_tags}")
        if res_type == 3:  # CColumn per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_denied_column_values()
            elementlist[int(element["id"][1:])] = element["Element"]
            usr.denied_column_value = ",".join(elementlist)
            ub.session_commit(f"Changed denied columns of user {usr.name} to {usr.denied_column_value}")
    return ""


@admi.route("/ajax/addrestriction/<int:res_type>", methods=["POST"])
@login_required
@admin_required
def add_user_0_restriction(res_type):
    return add_restriction(res_type, 0)


@admi.route("/ajax/addrestriction/<int:res_type>/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def add_restriction(res_type, user_id) -> str:
    element = request.form.to_dict()
    if res_type == 0:  # Tags as template
        if "submit_allow" in element:
            CONFIG.config_allowed_tags = restriction_addition(element, CONFIG.list_allowed_tags)
            CONFIG.save()
        elif "submit_deny" in element:
            CONFIG.config_denied_tags = restriction_addition(element, CONFIG.list_denied_tags)
            CONFIG.save()
    if res_type == 1:  # CCustom as template
        if "submit_allow" in element:
            CONFIG.config_allowed_column_value = restriction_addition(element, CONFIG.list_denied_column_values)
            CONFIG.save()
        elif "submit_deny" in element:
            CONFIG.config_denied_column_value = restriction_addition(element, CONFIG.list_allowed_column_values)
            CONFIG.save()
    if res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if "submit_allow" in element:
            usr.allowed_tags = restriction_addition(element, usr.list_allowed_tags)
            ub.session_commit(f"Changed allowed tags of user {usr.name} to {usr.list_allowed_tags()}")
        elif "submit_deny" in element:
            usr.denied_tags = restriction_addition(element, usr.list_denied_tags)
            ub.session_commit(f"Changed denied tags of user {usr.name} to {usr.list_denied_tags()}")
    if res_type == 3:  # CustomC per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if "submit_allow" in element:
            usr.allowed_column_value = restriction_addition(element, usr.list_allowed_column_values)
            ub.session_commit(f"Changed allowed columns of user {usr.name} to {usr.list_allowed_column_values()}")
        elif "submit_deny" in element:
            usr.denied_column_value = restriction_addition(element, usr.list_denied_column_values)
            ub.session_commit(f"Changed denied columns of user {usr.name} to {usr.list_denied_column_values()}")
    return ""


@admi.route("/ajax/deleterestriction/<int:res_type>", methods=["POST"])
@login_required
@admin_required
def delete_user_0_restriction(res_type):
    return delete_restriction(res_type, 0)


@admi.route("/ajax/deleterestriction/<int:res_type>/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def delete_restriction(res_type, user_id) -> str:
    element = request.form.to_dict()
    if res_type == 0:  # Tags as template
        if element["id"].startswith("a"):
            CONFIG.config_allowed_tags = restriction_deletion(element, CONFIG.list_allowed_tags)
            CONFIG.save()
        elif element["id"].startswith("d"):
            CONFIG.config_denied_tags = restriction_deletion(element, CONFIG.list_denied_tags)
            CONFIG.save()
    elif res_type == 1:  # CustomC as template
        if element["id"].startswith("a"):
            CONFIG.config_allowed_column_value = restriction_deletion(element, CONFIG.list_allowed_column_values)
            CONFIG.save()
        elif element["id"].startswith("d"):
            CONFIG.config_denied_column_value = restriction_deletion(element, CONFIG.list_denied_column_values)
            CONFIG.save()
    elif res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if element["id"].startswith("a"):
            usr.allowed_tags = restriction_deletion(element, usr.list_allowed_tags)
            ub.session_commit("Deleted allowed tags of user {}: {}".format(usr.name, element["Element"]))
        elif element["id"].startswith("d"):
            usr.denied_tags = restriction_deletion(element, usr.list_denied_tags)
            ub.session_commit("Deleted denied tag of user {}: {}".format(usr.name, element["Element"]))
    elif res_type == 3:  # Columns per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if element["id"].startswith("a"):
            usr.allowed_column_value = restriction_deletion(element, usr.list_allowed_column_values)
            ub.session_commit(f"Deleted allowed columns of user {usr.name}: {usr.list_allowed_column_values()}")

        elif element["id"].startswith("d"):
            usr.denied_column_value = restriction_deletion(element, usr.list_denied_column_values)
            ub.session_commit(f"Deleted denied columns of user {usr.name}: {usr.list_denied_column_values()}")
    return ""


@admi.route("/ajax/listrestriction/<int:res_type>", defaults={"user_id": 0})
@admi.route("/ajax/listrestriction/<int:res_type>/<int:user_id>")
@login_required
@admin_required
def list_restriction(res_type, user_id):
    if res_type == 0:  # Tags as template
        restrict = [{"Element": x, "type": _("Deny"), "id": "d" + str(i)}
                    for i, x in enumerate(config.list_denied_tags()) if x != ""]
        allow = [{"Element": x, "type": _("Allow"), "id": "a" + str(i)}
                 for i, x in enumerate(config.list_allowed_tags()) if x != ""]
        json_dumps = restrict + allow
    elif res_type == 1:  # CustomC as template
        restrict = [{"Element": x, "type": _("Deny"), "id": "d" + str(i)}
                    for i, x in enumerate(config.list_denied_column_values()) if x != ""]
        allow = [{"Element": x, "type": _("Allow"), "id": "a" + str(i)}
                 for i, x in enumerate(config.list_allowed_column_values()) if x != ""]
        json_dumps = restrict + allow
    elif res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
        else:
            usr = current_user
        restrict = [{"Element": x, "type": _("Deny"), "id": "d" + str(i)}
                    for i, x in enumerate(usr.list_denied_tags()) if x != ""]
        allow = [{"Element": x, "type": _("Allow"), "id": "a" + str(i)}
                 for i, x in enumerate(usr.list_allowed_tags()) if x != ""]
        json_dumps = restrict + allow
    elif res_type == 3:  # CustomC per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
        else:
            usr = current_user
        restrict = [{"Element": x, "type": _("Deny"), "id": "d" + str(i)}
                    for i, x in enumerate(usr.list_denied_column_values()) if x != ""]
        allow = [{"Element": x, "type": _("Allow"), "id": "a" + str(i)}
                 for i, x in enumerate(usr.list_allowed_column_values()) if x != ""]
        json_dumps = restrict + allow
    else:
        json_dumps = ""
    js = json.dumps(json_dumps)
    response = make_response(js)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@admi.route("/ajax/fullsync", methods=["POST"])
@login_required
def ajax_self_fullsync():
    return do_full_kobo_sync(current_user.id)


@admi.route("/ajax/fullsync/<int:userid>", methods=["POST"])
@login_required
@admin_required
def ajax_fullsync(userid):
    return do_full_kobo_sync(userid)


@admi.route("/ajax/pathchooser/")
@login_required
@admin_required
def ajax_pathchooser():
    return pathchooser()


def do_full_kobo_sync(userid):
    count = ub.session.query(ub.KoboSyncedBooks).filter(userid == ub.KoboSyncedBooks.user_id).delete()
    message = _("{} sync entries deleted").format(count)
    ub.session_commit(message)
    return Response(json.dumps([{"type": "success", "message": message}]), mimetype="application/json")


def check_valid_read_column(column) -> bool:
    if column != "0" and not calibre_db.session.query(db.CustomColumns).filter(db.CustomColumns.id == column) \
            .filter(and_(db.CustomColumns.datatype == "bool", db.CustomColumns.mark_for_delete == 0)).all():
        return False
    return True


def check_valid_restricted_column(column) -> bool:
    if column != "0" and not calibre_db.session.query(db.CustomColumns).filter(db.CustomColumns.id == column) \
            .filter(and_(db.CustomColumns.datatype == "text", db.CustomColumns.mark_for_delete == 0)).all():
        return False
    return True


def restriction_addition(element, list_func):
    elementlist = list_func()
    if elementlist == [""]:
        elementlist = []
    if element["add_element"] not in elementlist:
        elementlist += [element["add_element"]]
    return ",".join(elementlist)


def restriction_deletion(element, list_func):
    elementlist = list_func()
    if element["Element"] in elementlist:
        elementlist.remove(element["Element"])
    return ",".join(elementlist)


def prepare_tags(user, action, tags_name, id_list):
    if "tags" in tags_name:
        tags = calibre_db.session.query(db.Tags).filter(db.Tags.id.in_(id_list)).all()
        if not tags:
            raise Exception(_("Tag not found"))
        new_tags_list = [x.name for x in tags]
    else:
        tags = calibre_db.session.query(db.cc_classes[config.config_restricted_column]) \
            .filter(db.cc_classes[config.config_restricted_column].id.in_(id_list)).all()
        new_tags_list = [x.value for x in tags]
    saved_tags_list = user.__dict__[tags_name].split(",") if len(user.__dict__[tags_name]) else []
    if action == "remove":
        saved_tags_list = [x for x in saved_tags_list if x not in new_tags_list]
    elif action == "add":
        saved_tags_list.extend(x for x in new_tags_list if x not in saved_tags_list)
    else:
        raise Exception(_("Invalid Action"))
    return ",".join(saved_tags_list)


def get_drives(current):
    drive_letters = []
    for d in string.ascii_uppercase:
        if os.path.exists(f"{d}:") and current[0].lower() != d.lower():
            drive = f"{d}:\\"
            data = {"name": drive, "fullpath": drive}
            data["sort"] = "_" + data["fullpath"].lower()
            data["type"] = "dir"
            data["size"] = ""
            drive_letters.append(data)
    return drive_letters


def pathchooser():
    browse_for = "folder"
    folder_only = request.args.get("folder", False) == "true"
    file_filter = request.args.get("filter", "")
    path = os.path.normpath(request.args.get("path", ""))

    if os.path.isfile(path):
        old_file = path
        path = os.path.dirname(path)
    else:
        old_file = ""

    absolute = False

    if os.path.isdir(path):
        cwd = os.path.realpath(path)
        absolute = True
    else:
        cwd = os.getcwd()

    cwd = os.path.normpath(os.path.realpath(cwd))
    parent_dir = os.path.dirname(cwd)
    if not absolute:
        if os.path.realpath(cwd) == os.path.realpath("/"):
            cwd = os.path.relpath(cwd)
        else:
            cwd = os.path.relpath(cwd) + os.path.sep
        parent_dir = os.path.relpath(parent_dir) + os.path.sep

    files = []
    if os.path.realpath(cwd) == os.path.realpath("/") \
            or (sys.platform == "win32" and os.path.realpath(cwd)[1:] == os.path.realpath("/")[1:]):
        # we are in root
        parent_dir = ""
        if sys.platform == "win32":
            files = get_drives(cwd)

    try:
        folders = os.listdir(cwd)
    except Exception:
        folders = []

    for f in folders:
        try:
            sanitized_f = str(Markup.escape(f))
            data = {"name": sanitized_f, "fullpath": os.path.join(cwd, sanitized_f)}
            data["sort"] = data["fullpath"].lower()
        except Exception:
            continue

        if os.path.isfile(os.path.join(cwd, f)):
            if folder_only:
                continue
            if file_filter not in ("", f):
                continue
            data["type"] = "file"
            data["size"] = os.path.getsize(os.path.join(cwd, f))

            power = 0
            while (data["size"] >> 10) > 0.3:
                power += 1
                data["size"] >>= 10
            units = ("", "K", "M", "G", "T")
            data["size"] = str(data["size"]) + " " + units[power] + "Byte"
        else:
            data["type"] = "dir"
            data["size"] = ""

        files.append(data)

    files = sorted(files, key=operator.itemgetter("type", "sort"))

    context = {
        "cwd": cwd,
        "files": files,
        "parentdir": parent_dir,
        "type": browse_for,
        "oldfile": old_file,
        "absolute": absolute,
    }
    return json.dumps(context)


def _config_int(to_save, x, func=int):
    return CONFIG.set_from_dictionary(to_save, x, func)


def _config_checkbox(to_save, x):
    return CONFIG.set_from_dictionary(to_save, x, lambda y: y == "on", False)


def _config_checkbox_int(to_save, x):
    return CONFIG.set_from_dictionary(to_save, x, lambda y: 1 if (y == "on") else 0, 0)


def _config_string(to_save, x):
    return CONFIG.set_from_dictionary(to_save, x, lambda y: y.strip().strip("\u200B\u200C\u200D\ufeff") if y else y)


def _configuration_oauth_helper(to_save):
    active_oauths = 0
    reboot_required = False
    for element in oauthblueprints:
        if to_save["config_" + str(element["id"]) + "_oauth_client_id"] != element["oauth_client_id"] \
            or to_save["config_" + str(element["id"]) + "_oauth_client_secret"] != element["oauth_client_secret"]:
            reboot_required = True
            element["oauth_client_id"] = to_save["config_" + str(element["id"]) + "_oauth_client_id"]
            element["oauth_client_secret"] = to_save["config_" + str(element["id"]) + "_oauth_client_secret"]
        if to_save["config_" + str(element["id"]) + "_oauth_client_id"] \
            and to_save["config_" + str(element["id"]) + "_oauth_client_secret"]:
            active_oauths += 1
            element["active"] = 1
        else:
            element["active"] = 0
        ub.session.query(ub.OAuthProvider).filter(ub.OAuthProvider.id == element["id"]).update(
            {"oauth_client_id": to_save["config_" + str(element["id"]) + "_oauth_client_id"],
             "oauth_client_secret": to_save["config_" + str(element["id"]) + "_oauth_client_secret"],
             "active": element["active"]})
        if element["id"] == 3:
            ub.session.query(ub.OAuthProvider).filter(ub.OAuthProvider.id == element["id"]).update({
             "oauth_base_url": to_save["config_" + str(element["id"]) + "_oauth_base_url"],
             "oauth_auth_url": to_save["config_" + str(element["id"]) + "_oauth_auth_url"],
             "oauth_token_url": to_save["config_" + str(element["id"]) + "_oauth_token_url"],
             "oauth_userinfo_url": to_save["config_" + str(element["id"]) + "_oauth_userinfo_url"],
             "username_mapper": to_save["config_" + str(element["id"]) + "_username_mapper"],
             "email_mapper": to_save["config_" + str(element["id"]) + "_email_mapper"],
             "login_button": to_save["config_" + str(element["id"]) + "_login_button"],
             "scope": to_save["config_" + str(element["id"]) + "_scope"],
            })

    return reboot_required


def _configuration_logfile_helper(to_save):
    reboot_required = False
    reboot_required |= _config_int(to_save, "config_log_level")
    reboot_required |= _config_string(to_save, "config_logfile")
    if not logger.is_valid_logfile(config.config_logfile):
        return reboot_required, \
               _configuration_result(_("Logfile Location is not Valid, Please Enter Correct Path"))

    reboot_required |= _config_checkbox_int(to_save, "config_access_log")
    reboot_required |= _config_string(to_save, "config_access_logfile")
    if not logger.is_valid_logfile(config.config_access_logfile):
        return reboot_required, \
               _configuration_result(_("Access Logfile Location is not Valid, Please Enter Correct Path"))
    return reboot_required, None


@admi.route("/ajax/simulatedbchange", methods=["POST"])
@login_required
@admin_required
def simulatedbchange():
    db_change, db_valid = _db_simulate_change()
    return Response(json.dumps({"change": db_change, "valid": db_valid}), mimetype="application/json")


@admi.route("/admin/user/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_user():
    content = ub.User()
    languages = calibre_db.speaking_language()
    translations = get_available_locale()
    kobo_support = feature_support["kobo"] and CONFIG.config_kobo_sync
    if request.method == "POST":
        to_save = request.form.to_dict()
        _handle_new_user(to_save, content, languages, translations, kobo_support)
    else:
        content.role = CONFIG.config_default_role
        content.sidebar_view = CONFIG.config_default_show
        content.locale = CONFIG.config_default_locale
        content.default_language = CONFIG.config_default_language
    return render_title_template("user_edit.html", new_user=1, content=content,
                                 config=config, translations=translations,
                                 languages=languages, title=_("Add New User"), page="newuser",
                                 kobo_support=kobo_support, registered_oauth=oauth_check)


@admi.route("/admin/mailsettings", methods=["GET"])
@login_required
@admin_required
def edit_mailsettings():
    content = CONFIG.get_mail_settings()
    return render_title_template("email_edit.html", content=content, title=_("Edit Email Server Settings"),
                                 page="mailset", feature_support=feature_support)


@admi.route("/admin/mailsettings", methods=["POST"])
@login_required
@admin_required
def update_mailsettings():
    to_save = request.form.to_dict()
    _config_int(to_save, "mail_server_type")
    _config_int(to_save, "mail_port")
    _config_int(to_save, "mail_use_ssl")
    if to_save.get("mail_password_e", ""):
        _config_string(to_save, "mail_password_e")
    _config_int(to_save, "mail_size", lambda y: int(y) * 1024 * 1024)
    CONFIG.mail_server = to_save.get("mail_server", "").strip()
    CONFIG.mail_from = to_save.get("mail_from", "").strip()
    CONFIG.mail_login = to_save.get("mail_login", "").strip()
    try:
        CONFIG.save()
    except (OperationalError, InvalidRequestError) as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
        return edit_mailsettings()
    except Exception as e:
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
        return edit_mailsettings()

    if to_save.get("test"):
        if current_user.email:
            result = send_test_mail(current_user.email, current_user.name)
            if result is None:
                flash(_("Test e-mail queued for sending to %(email)s, please check Tasks for result",
                        email=current_user.email), category="info")
            else:
                flash(_("There was an error sending the Test e-mail: %(res)s", res=result), category="error")
        else:
            flash(_("Please configure your e-mail address first..."), category="error")
    else:
        flash(_("Email Server Settings updated"), category="success")

    return edit_mailsettings()


@admi.route("/admin/scheduledtasks")
@login_required
@admin_required
def edit_scheduledtasks():
    content = CONFIG.get_scheduled_task_settings()
    time_field = []
    duration_field = []

    for n in range(24):
        time_field.append((n, format_time(datetime_time(hour=n), format="short", )))
    for n in range(5, 65, 5):
        t = timedelta(hours=n // 60, minutes=n % 60)
        duration_field.append((n, format_timedelta(t, threshold=.97)))

    return render_title_template("schedule_edit.html",
                                 config=content,
                                 starttime=time_field,
                                 duration=duration_field,
                                 title=_("Edit Scheduled Tasks Settings"))


@admi.route("/admin/scheduledtasks", methods=["POST"])
@login_required
@admin_required
def update_scheduledtasks():
    error = False
    to_save = request.form.to_dict()
    if 0 <= int(to_save.get("schedule_start_time")) <= 23:
        _config_int( to_save, "schedule_start_time")
    else:
        flash(_("Invalid start time for task specified"), category="error")
        error = True
    if 0 < int(to_save.get("schedule_duration")) <= 60:
        _config_int(to_save, "schedule_duration")
    else:
        flash(_("Invalid duration for task specified"), category="error")
        error = True
    _config_checkbox(to_save, "schedule_generate_book_covers")
    _config_checkbox(to_save, "schedule_generate_series_covers")
    _config_checkbox(to_save, "schedule_metadata_backup")
    _config_checkbox(to_save, "schedule_reconnect")

    if not error:
        try:
            CONFIG.save()
            flash(_("Scheduled tasks settings updated"), category="success")

            # Cancel any running tasks
            schedule.end_scheduled_tasks()

            # Re-register tasks with new settings
            schedule.register_scheduled_tasks(config.schedule_reconnect)
        except IntegrityError:
            ub.session.rollback()
            log.exception("An unknown error occurred while saving scheduled tasks settings")
            flash(_("Oops! An unknown error occurred. Please try again later."), category="error")
        except OperationalError:
            ub.session.rollback()
            log.exception("Settings DB is not Writeable")
            flash(_("Settings DB is not Writeable"), category="error")

    return edit_scheduledtasks()


@admi.route("/admin/user/<int:user_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id):
    content = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()  # type: ub.User
    if not content or (not CONFIG.config_anonbrowse and content.name == "Guest"):
        flash(_("User not found"), category="error")
        return redirect(url_for("admin.admin"))
    languages = calibre_db.speaking_language(return_all_languages=True)
    translations = get_available_locale()
    kobo_support = feature_support["kobo"] and CONFIG.config_kobo_sync
    if request.method == "POST":
        to_save = request.form.to_dict()
        resp = _handle_edit_user(to_save, content, languages, translations, kobo_support)
        if resp:
            return resp
    return render_title_template("user_edit.html",
                                 translations=translations,
                                 languages=languages,
                                 new_user=0,
                                 content=content,
                                 config=config,
                                 registered_oauth=oauth_check,
                                 mail_configured=config.get_mail_server_configured(),
                                 kobo_support=kobo_support,
                                 title=_("Edit User %(nick)s", nick=content.name),
                                 page="edituser")


@admi.route("/admin/resetpassword/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def reset_user_password(user_id):
    if current_user is not None and current_user.is_authenticated:
        ret, message = reset_password(user_id)
        if ret == 1:
            log.debug("Password for user %s reset", message)
            flash(_("Success! Password for user %(user)s reset", user=message), category="success")
        elif ret == 0:
            log.error("An unknown error occurred. Please try again later.")
            flash(_("Oops! An unknown error occurred. Please try again later."), category="error")
        else:
            log.error("Please configure the SMTP mail settings.")
            flash(_("Oops! Please configure the SMTP mail settings."), category="error")
    return redirect(url_for("admin.admin"))


@admi.route("/admin/logfile")
@login_required
@admin_required
def view_logfile():
    logfiles = {0: logger.get_logfile(config.config_logfile),
                1: logger.get_accesslogfile(config.config_access_logfile)}
    return render_title_template("logviewer.html",
                                 title=_("Logfile viewer"),
                                 accesslog_enable=config.config_access_log,
                                 log_enable=bool(config.config_logfile != logger.LOG_TO_STDOUT),
                                 logfiles=logfiles,
                                 page="logfile")


@admi.route("/ajax/log/<int:logtype>")
@login_required
@admin_required
def send_logfile(logtype):
    if logtype == 1:
        logfile = logger.get_accesslogfile(config.config_access_logfile)
        return send_from_directory(os.path.dirname(logfile),
                                   os.path.basename(logfile))
    if logtype == 0:
        logfile = logger.get_logfile(config.config_logfile)
        return send_from_directory(os.path.dirname(logfile),
                                   os.path.basename(logfile))
    else:
        return ""


@admi.route("/admin/logdownload/<int:logtype>")
@login_required
@admin_required
def download_log(logtype):
    if logtype == 0:
        file_name = logger.get_logfile(config.config_logfile)
    elif logtype == 1:
        file_name = logger.get_accesslogfile(config.config_access_logfile)
    else:
        abort(404)
    if logger.is_valid_logfile(file_name):
        return debug_info.assemble_logfiles(file_name)
    abort(404)
    return None


@admi.route("/admin/debug")
@login_required
@admin_required
def download_debug():
    return debug_info.send_debug()


@admi.route("/get_update_status", methods=["GET"])
@login_required
@admin_required
def get_update_status():
    if feature_support["updater"]:
        log.info("Update status requested")
        return updater_thread.get_available_updates(request.method)
    else:
        return ""


@admi.route("/get_updater_status", methods=["GET", "POST"])
@login_required
@admin_required
def get_updater_status():
    status = {}
    if feature_support["updater"]:
        if request.method == "POST":
            commit = request.form.to_dict()
            if "start" in commit and commit["start"] == "True":
                txt = {
                    "1": _("Requesting update package"),
                    "2": _("Downloading update package"),
                    "3": _("Unzipping update package"),
                    "4": _("Replacing files"),
                    "5": _("Database connections are closed"),
                    "6": _("Stopping server"),
                    "7": _("Update finished, please press okay and reload page"),
                    "8": _("Update failed:") + " " + _("HTTP Error"),
                    "9": _("Update failed:") + " " + _("Connection error"),
                    "10": _("Update failed:") + " " + _("Timeout while establishing connection"),
                    "11": _("Update failed:") + " " + _("General error"),
                    "12": _("Update failed:") + " " + _("Update file could not be saved in temp dir"),
                    "13": _("Update failed:") + " " + _("Files could not be replaced during update")
                }
                status["text"] = txt
                updater_thread.status = 0
                updater_thread.resume()
                status["status"] = updater_thread.get_update_status()
        elif request.method == "GET":
            try:
                status["status"] = updater_thread.get_update_status()
                if status["status"] == -1:
                    status["status"] = 7
            except Exception:
                status["status"] = 11
        return json.dumps(status)
    return ""


@admi.route("/ajax/canceltask", methods=["POST"])
@login_required
@admin_required
def cancel_task() -> str:
    task_id = request.get_json().get("task_id", None)
    worker = WorkerThread.get_instance()
    worker.end_task(task_id)
    return ""


def _db_simulate_change():
    param = request.form.to_dict()
    to_save = {}
    to_save["config_calibre_dir"] = re.sub(r"[\\/]metadata\.db$",
                                           "",
                                           param["config_calibre_dir"],
                                           flags=re.IGNORECASE).strip()
    db_valid, db_change = calibre_db.check_valid_db(to_save["config_calibre_dir"],
                                                    ub.app_DB_path,
                                                    CONFIG.config_calibre_uuid)
    db_change = bool(db_change and CONFIG.config_calibre_dir)
    return db_change, db_valid


def _db_configuration_update_helper():
    db_change = False
    to_save = request.form.to_dict()

    to_save["config_calibre_dir"] = re.sub(r"[\\/]metadata\.db$",
                                           "",
                                           to_save["config_calibre_dir"],
                                           flags=re.IGNORECASE)
    db_valid = False
    try:
        db_change, db_valid = _db_simulate_change()
    except (OperationalError, InvalidRequestError) as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        _db_configuration_result(_("Oops! Database Error: %(error)s.", error=e.orig))
    try:
        metadata_db = os.path.join(to_save["config_calibre_dir"], "metadata.db")
    except Exception as ex:
        return _db_configuration_result(f"{ex}")

    if db_change or not db_valid or not CONFIG.db_configured \
       or CONFIG.config_calibre_dir != to_save["config_calibre_dir"]:
        if not os.path.exists(metadata_db) or not to_save["config_calibre_dir"]:
            return _db_configuration_result(_("DB Location is not Valid, Please Enter Correct Path"))
        else:
            calibre_db.setup_db(to_save["config_calibre_dir"], ub.app_DB_path)
        CONFIG.store_calibre_uuid(calibre_db, db.Library_Id)
        # if db changed -> delete shelfs, delete download books, delete read books, kobo sync...
        if db_change:
            log.info("Calibre Database changed, all Calibre-Web info related to old Database gets deleted")
            ub.session.query(ub.Downloads).delete()
            ub.session.query(ub.ArchivedBook).delete()
            ub.session.query(ub.ReadBook).delete()
            ub.session.query(ub.BookShelf).delete()
            ub.session.query(ub.Bookmark).delete()
            ub.session.query(ub.KoboReadingState).delete()
            ub.session.query(ub.KoboStatistics).delete()
            ub.session.query(ub.KoboSyncedBooks).delete()
            helper.delete_thumbnail_cache()
            ub.session_commit()
        _config_string(to_save, "config_calibre_dir")
        calibre_db.update_config(config)
        if not os.access(os.path.join(config.config_calibre_dir, "metadata.db"), os.W_OK):
            flash(_("DB is not Writeable"), category="warning")
    _config_string(to_save, "config_calibre_split_dir")
    CONFIG.config_calibre_split = to_save.get("config_calibre_split", 0) == "on"
    calibre_db.update_config(config)
    CONFIG.save()
    return _db_configuration_result(None)


def _configuration_update_helper():
    reboot_required = False
    to_save = request.form.to_dict()
    try:
        reboot_required |= _config_int(to_save, "config_port")
        reboot_required |= _config_string(to_save, "config_trustedhosts")
        reboot_required |= _config_string(to_save, "config_keyfile")
        if CONFIG.config_keyfile and not os.path.isfile(config.config_keyfile):
            return _configuration_result(_("Keyfile Location is not Valid, Please Enter Correct Path"))

        reboot_required |= _config_string(to_save, "config_certfile")
        if CONFIG.config_certfile and not os.path.isfile(config.config_certfile):
            return _configuration_result(_("Certfile Location is not Valid, Please Enter Correct Path"))

        _config_checkbox_int(to_save, "config_uploading")
        _config_checkbox_int(to_save, "config_unicode_filename")
        _config_checkbox_int(to_save, "config_embed_metadata")
        _config_checkbox_int(to_save, "config_public_reg")
        _config_checkbox_int(to_save, "config_register_email")
        reboot_required |= _config_checkbox_int(to_save, "config_kobo_sync")
        _config_int(to_save, "config_external_port")
        _config_checkbox_int(to_save, "config_kobo_proxy")

        if "config_upload_formats" in to_save:
            to_save["config_upload_formats"] = ",".join(
                helper.uniq([x.lstrip().rstrip().lower() for x in to_save["config_upload_formats"].split(",")]))
            _config_string(to_save, "config_upload_formats")
            constants.EXTENSIONS_UPLOAD = CONFIG.config_upload_formats.split(",")

        _config_string(to_save, "config_calibre")
        _config_string(to_save, "config_binariesdir")
        _config_string(to_save, "config_kepubifypath")
        if "config_binariesdir" in to_save:
            calibre_status = helper.check_calibre(config.config_binariesdir)
            if calibre_status:
                return _configuration_result(calibre_status)
            to_save["config_converterpath"] = get_calibre_binarypath("ebook-convert")
            _config_string(to_save, "config_converterpath")

        reboot_required |= _config_int(to_save, "config_login_type")

        # Remote login configuration
        _config_checkbox(to_save, "config_remote_login")
        if not CONFIG.config_remote_login:
            ub.session.query(ub.RemoteAuthToken).filter(ub.RemoteAuthToken.token_type == 0).delete()

        _config_int(to_save, "config_updatechannel")

        # Reverse proxy login configuration
        _config_checkbox(to_save, "config_allow_reverse_proxy_header_login")
        _config_string(to_save, "config_reverse_proxy_login_header_name")

        # OAuth configuration
        if CONFIG.config_login_type == constants.LOGIN_OAUTH:
            reboot_required |= _configuration_oauth_helper(to_save)

        # logfile configuration
        reboot, message = _configuration_logfile_helper(to_save)
        if message:
            return message
        reboot_required |= reboot

        # security configuration
        _config_checkbox(to_save, "config_password_policy")
        _config_checkbox(to_save, "config_password_number")
        _config_checkbox(to_save, "config_password_lower")
        _config_checkbox(to_save, "config_password_upper")
        _config_checkbox(to_save, "config_password_special")
        if 0 < int(to_save.get("config_password_min_length", "0")) < 41:
            _config_int(to_save, "config_password_min_length")
        else:
            return _configuration_result(_("Password length has to be between 1 and 40"))
        reboot_required |= _config_int(to_save, "config_session")
        reboot_required |= _config_checkbox(to_save, "config_ratelimiter")

        # Rarfile Content configuration
        _config_string(to_save, "config_rarfile_location")
        if "config_rarfile_location" in to_save:
            unrar_status = helper.check_unrar(config.config_rarfile_location)
            if unrar_status:
                return _configuration_result(unrar_status)
    except (OperationalError, InvalidRequestError) as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        _configuration_result(_("Oops! Database Error: %(error)s.", error=e.orig))

    CONFIG.save()
    if reboot_required:
        web_server.stop(True)

    return _configuration_result(None, reboot_required)


def _configuration_result(error_flash=None, reboot=False):
    resp = {}
    if error_flash:
        log.error(error_flash)
        CONFIG.load()
        resp["result"] = [{"type": "danger", "message": error_flash}]
    else:
        resp["result"] = [{"type": "success", "message": _("Calibre-Web configuration updated")}]
    resp["reboot"] = reboot
    resp["config_upload"] = CONFIG.config_upload_formats
    return Response(json.dumps(resp), mimetype="application/json")


def _db_configuration_result(error_flash=None):
    if error_flash:
        log.error(error_flash)
        CONFIG.load()
        flash(error_flash, category="error")
    elif request.method == "POST":
        flash(_("Database Settings updated"), category="success")

    return render_title_template("config_db.html",
                                 config=config,
                                 feature_support=feature_support,
                                 title=_("Database Configuration"), page="dbconfig")


def _handle_new_user(to_save, content, languages, translations, kobo_support):
    content.default_language = to_save["default_language"]
    content.locale = to_save.get("locale", content.locale)

    content.sidebar_view = sum(int(key[5:]) for key in to_save if key.startswith("show_"))
    if "show_detail_random" in to_save:
        content.sidebar_view |= constants.DETAIL_RANDOM

    content.role = constants.selected_roles(to_save)
    try:
        if not to_save["name"] or not to_save["email"] or not to_save["password"]:
            log.info("Missing entries on new user")
            raise Exception(_("Oops! Please complete all fields."))
        content.password = generate_password_hash(helper.valid_password(to_save.get("password", "")))
        content.email = check_email(to_save["email"])
        # Query username, if not existing, change
        content.name = check_username(to_save["name"])
        if to_save.get("kindle_mail"):
            content.kindle_mail = valid_email(to_save["kindle_mail"])
        if CONFIG.config_public_reg and not check_valid_domain(content.email):
            log.info(f"E-mail: {content.email} for new user is not from valid domain")
            raise Exception(_("E-mail is not from valid domain"))
    except Exception as ex:
        flash(str(ex), category="error")
        return render_title_template("user_edit.html", new_user=1, content=content,
                                     config=config,
                                     translations=translations,
                                     languages=languages, title=_("Add new user"), page="newuser",
                                     kobo_support=kobo_support, registered_oauth=oauth_check)
    try:
        content.allowed_tags = CONFIG.config_allowed_tags
        content.denied_tags = CONFIG.config_denied_tags
        content.allowed_column_value = CONFIG.config_allowed_column_value
        content.denied_column_value = CONFIG.config_denied_column_value
        # No default value for kobo sync shelf setting
        content.kobo_only_shelves_sync = to_save.get("kobo_only_shelves_sync", 0) == "on"
        ub.session.add(content)
        ub.session.commit()
        flash(_("User '%(user)s' created", user=content.name), category="success")
        log.debug(f"User {content.name} created")
        return redirect(url_for("admin.admin"))
    except IntegrityError:
        ub.session.rollback()
        log.exception(f"Found an existing account for {content.name} or {content.email}")
        flash(_("Oops! An account already exists for this Email. or name."), category="error")
    except OperationalError as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")


def _delete_user(content):
    if ub.session.query(ub.User).filter(ub.User.role.op("&")(constants.ROLE_ADMIN) == constants.ROLE_ADMIN,
                                        ub.User.id != content.id).count():
        if content.name != "Guest":
            # Delete all books in shelfs belonging to user, all shelfs of user, downloadstat of user, read status
            # and user itself
            ub.session.query(ub.ReadBook).filter(content.id == ub.ReadBook.user_id).delete()
            ub.session.query(ub.Downloads).filter(content.id == ub.Downloads.user_id).delete()
            for us in ub.session.query(ub.Shelf).filter(content.id == ub.Shelf.user_id):
                ub.session.query(ub.BookShelf).filter(us.id == ub.BookShelf.shelf).delete()
            ub.session.query(ub.Shelf).filter(content.id == ub.Shelf.user_id).delete()
            ub.session.query(ub.Bookmark).filter(content.id == ub.Bookmark.user_id).delete()
            ub.session.query(ub.User).filter(ub.User.id == content.id).delete()
            ub.session.query(ub.ArchivedBook).filter(ub.ArchivedBook.user_id == content.id).delete()
            ub.session.query(ub.RemoteAuthToken).filter(ub.RemoteAuthToken.user_id == content.id).delete()
            ub.session.query(ub.User_Sessions).filter(ub.User_Sessions.user_id == content.id).delete()
            ub.session.query(ub.KoboSyncedBooks).filter(ub.KoboSyncedBooks.user_id == content.id).delete()
            # delete KoboReadingState and all it's children
            kobo_entries = ub.session.query(ub.KoboReadingState).filter(ub.KoboReadingState.user_id == content.id).all()
            for kobo_entry in kobo_entries:
                ub.session.delete(kobo_entry)
            ub.session_commit()
            log.info(f"User {content.name} deleted")
            return _("User '%(nick)s' deleted", nick=content.name)
        else:
            # log.warning(_("Can't delete Guest User"))
            raise Exception(_("Can't delete Guest User"))
    else:
        # log.warning("No admin user remaining, can't delete user")
        raise Exception(_("No admin user remaining, can't delete user"))


def _handle_edit_user(to_save, content, languages, translations, kobo_support):
    if to_save.get("delete"):
        try:
            flash(_delete_user(content), category="success")
        except Exception as ex:
            log.exception(ex)
            flash(str(ex), category="error")
        return redirect(url_for("admin.admin"))
    else:
        if not ub.session.query(ub.User).filter(ub.User.role.op("&")(constants.ROLE_ADMIN) == constants.ROLE_ADMIN,
                                                ub.User.id != content.id).count() and "admin_role" not in to_save:
            log.warning(f"No admin user remaining, can't remove admin role from {content.name}")
            flash(_("No admin user remaining, can't remove admin role"), category="error")
            return redirect(url_for("admin.admin"))

        val = [int(k[5:]) for k in to_save if k.startswith("show_")]
        sidebar, __ = get_sidebar_config()
        for element in sidebar:
            value = element["visibility"]
            if value in val and not content.check_visibility(value):
                content.sidebar_view |= value
            elif value not in val and content.check_visibility(value):
                content.sidebar_view &= ~value

        if to_save.get("Show_detail_random"):
            content.sidebar_view |= constants.DETAIL_RANDOM
        else:
            content.sidebar_view &= ~constants.DETAIL_RANDOM

        old_state = content.kobo_only_shelves_sync
        content.kobo_only_shelves_sync = int(to_save.get("kobo_only_shelves_sync") == "on") or 0
        # 1 -> 0: nothing has to be done
        # 0 -> 1: all synced books have to be added to archived books, + currently synced shelfs
        # which don't have to be synced have to be removed (added to Shelf archive)
        if old_state == 0 and content.kobo_only_shelves_sync == 1:
            kobo_sync_status.update_on_sync_shelfs(content.id)
        if to_save.get("default_language"):
            content.default_language = to_save["default_language"]
        if to_save.get("locale"):
            content.locale = to_save["locale"]
        try:
            anonymous = content.is_anonymous
            content.role = constants.selected_roles(to_save)
            if anonymous:
                content.role |= constants.ROLE_ANONYMOUS
            else:
                content.role &= ~constants.ROLE_ANONYMOUS
                if to_save.get("password", ""):
                    content.password = generate_password_hash(helper.valid_password(to_save.get("password", "")))

            new_email = valid_email(to_save.get("email", content.email))
            if not new_email:
                raise Exception(_("Email can't be empty and has to be a valid Email"))
            if new_email != content.email:
                content.email = check_email(new_email)
            # Query username, if not existing, change
            if to_save.get("name", content.name) != content.name:
                if to_save.get("name") == "Guest":
                    raise Exception(_("Guest Name can't be changed"))
                content.name = check_username(to_save["name"])
            if to_save.get("kindle_mail") != content.kindle_mail:
                content.kindle_mail = valid_email(to_save["kindle_mail"]) if to_save["kindle_mail"] else ""
        except Exception as ex:
            log.exception(ex)
            flash(str(ex), category="error")
            return render_title_template("user_edit.html",
                                         translations=translations,
                                         languages=languages,
                                         mail_configured=config.get_mail_server_configured(),
                                         kobo_support=kobo_support,
                                         new_user=0,
                                         content=content,
                                         config=config,
                                         registered_oauth=oauth_check,
                                         title=_("Edit User %(nick)s", nick=content.name),
                                         page="edituser")
    try:
        ub.session_commit()
        flash(_("User '%(nick)s' updated", nick=content.name), category="success")
    except IntegrityError as ex:
        ub.session.rollback()
        log.exception(f"An unknown error occurred while changing user: {ex!s}")
        flash(_("Oops! An unknown error occurred. Please try again later."), category="error")
    except OperationalError as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
    return ""
