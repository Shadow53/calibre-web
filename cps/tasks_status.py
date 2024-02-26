#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2022 OzzieIsaacs
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

from flask import Blueprint, jsonify
from flask_babel import format_datetime
from flask_babel import gettext as _
from flask_login import current_user, login_required
from markupsafe import escape

from babel.units import format_unit

from . import logger
from .render_template import render_title_template
from .services.worker import (
    STAT_CANCELLED,
    STAT_ENDED,
    STAT_FAIL,
    STAT_FINISH_SUCCESS,
    STAT_STARTED,
    STAT_WAITING,
    WorkerThread,
)

tasks = Blueprint("tasks", __name__)

log = logger.create()


@tasks.route("/ajax/emailstat")
@login_required
def get_email_status_json():
    tasks = WorkerThread.get_instance().tasks
    return jsonify(render_task_status(tasks))


@tasks.route("/tasks")
@login_required
def get_tasks_status():
    # if current user admin, show all email, otherwise only own emails
    return render_title_template("tasks.html", title=_("Tasks"), page="tasks")


# helper function to apply localize status information in tasklist entries
def render_task_status(tasklist):
    rendered_tasklist = list()
    for __, user, __, task, __ in tasklist:
        if user == current_user.name or current_user.role_admin():
            ret = {}
            if task.start_time:
                ret["starttime"] = format_datetime(task.start_time, format="short")
                ret["runtime"] = format_runtime(task.runtime)

            # localize the task status
            if isinstance(task.stat, int):
                if task.stat == STAT_WAITING:
                    ret["status"] = _("Waiting")
                elif task.stat == STAT_FAIL:
                    ret["status"] = _("Failed")
                elif task.stat == STAT_STARTED:
                    ret["status"] = _("Started")
                elif task.stat == STAT_FINISH_SUCCESS:
                    ret["status"] = _("Finished")
                elif task.stat == STAT_ENDED:
                    ret["status"] = _("Ended")
                elif task.stat == STAT_CANCELLED:
                    ret["status"] = _("Cancelled")
                else:
                    ret["status"] = _("Unknown Status")

            ret["taskMessage"] = f"{task.name}: {task.message}" if task.message else task.name
            ret["progress"] = f"{int(task.progress * 100)} %"
            ret["user"] = escape(user)  # prevent xss

            # Hidden fields
            ret["task_id"] = task.id
            ret["stat"] = task.stat
            ret["is_cancellable"] = task.is_cancellable

            rendered_tasklist.append(ret)

    return rendered_tasklist


# helper function for displaying the runtime of tasks
def format_runtime(runtime):
    ret_val = ""
    if runtime.days:
        ret_val = format_unit(runtime.days, "duration-day", length="long") + ", "
    minutes, seconds = divmod(runtime.seconds, 60)
    hours, minutes = divmod(minutes, 60)
    # TODO: locale.number_symbols._data['timeSeparator'] -> localize time separator ?
    if hours:
        ret_val += f"{hours:d}:{minutes:02d}:{seconds:02d}s"
    elif minutes:
        ret_val += f"{minutes:2d}:{seconds:02d}s"
    else:
        ret_val += f"{seconds:2d}s"
    return ret_val
