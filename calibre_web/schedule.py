
#   This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#     Copyright (C) 2020 mmonkey
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program. If not, see <http://www.gnu.org/licenses/>.

import datetime

from . import constants
from .config_sql import CONFIG
from .services.background_scheduler import BackgroundScheduler, CronTrigger
from .services.worker import WorkerThread
from .tasks.database import TaskReconnectDatabase
from .tasks.metadata_backup import TaskBackupMetadata
from .tasks.tempFolder import TaskDeleteTempFolder
from .tasks.thumbnail import TaskClearCoverThumbnailCache, TaskGenerateCoverThumbnails, TaskGenerateSeriesThumbnails


def get_scheduled_tasks(reconnect=True):
    tasks = []
    # Reconnect Calibre database (metadata.db) based on CONFIG.schedule_reconnect
    if reconnect:
        tasks.append([lambda: TaskReconnectDatabase(), "reconnect", False])

    # Delete temp folder
    tasks.append([lambda: TaskDeleteTempFolder(), "delete temp", True])

    # Generate metadata.opf file for each changed book
    if CONFIG.schedule_metadata_backup:
        tasks.append([lambda: TaskBackupMetadata("en"), "backup metadata", False])

    # Generate all missing book cover thumbnails
    if CONFIG.schedule_generate_book_covers:
        tasks.append([lambda: TaskClearCoverThumbnailCache(0), "delete superfluous book covers", True])
        tasks.append([lambda: TaskGenerateCoverThumbnails(), "generate book covers", False])

    # Generate all missing series thumbnails
    if CONFIG.schedule_generate_series_covers:
        tasks.append([lambda: TaskGenerateSeriesThumbnails(), "generate book covers", False])

    return tasks


def end_scheduled_tasks() -> None:
    worker = WorkerThread.get_instance()
    for __, __, __, task, __ in worker.tasks:
        if task.scheduled and task.is_cancellable:
            worker.end_task(task.id)


def register_scheduled_tasks(reconnect=True) -> None:
    scheduler = BackgroundScheduler()

    if scheduler:
        # Remove all existing jobs
        scheduler.remove_all_jobs()

        start = CONFIG.schedule_start_time
        duration = CONFIG.schedule_duration

        # Register scheduled tasks
        scheduler.schedule_tasks(tasks=get_scheduled_tasks(reconnect), trigger=CronTrigger(hour=start))
        end_time = calclulate_end_time(start, duration)
        scheduler.schedule(func=end_scheduled_tasks, trigger=CronTrigger(hour=end_time.hour, minute=end_time.minute),
                           name="end scheduled task")

        # Kick-off tasks, if they should currently be running
        if should_task_be_running(start, duration):
            scheduler.schedule_tasks_immediately(tasks=get_scheduled_tasks(reconnect))


def register_startup_tasks() -> None:
    scheduler = BackgroundScheduler()

    if scheduler:
        start = CONFIG.schedule_start_time
        duration = CONFIG.schedule_duration

        # Run scheduled tasks immediately for development and testing
        # Ignore tasks that should currently be running, as these will be added when registering scheduled tasks
        if constants.APP_MODE in ["development", "test"] and not should_task_be_running(start, duration):
            scheduler.schedule_tasks_immediately(tasks=get_scheduled_tasks(False))
        else:
            scheduler.schedule_tasks_immediately(tasks=[[lambda: TaskDeleteTempFolder(), "delete temp", True]])


def should_task_be_running(start, duration):
    now = datetime.datetime.now()
    start_time = datetime.datetime.now().replace(hour=start, minute=0, second=0, microsecond=0)
    end_time = start_time + datetime.timedelta(hours=duration // 60, minutes=duration % 60)
    return start_time < now < end_time


def calclulate_end_time(start, duration):
    start_time = datetime.datetime.now().replace(hour=start, minute=0)
    return start_time + datetime.timedelta(hours=duration // 60, minutes=duration % 60)

