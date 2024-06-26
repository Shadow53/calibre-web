#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2020 pwr
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

from datetime import datetime

from flask_babel import lazy_gettext as N_

from calibre_web.services.worker import STAT_FINISH_SUCCESS, CalibreTask


class TaskUpload(CalibreTask):
    def __init__(self, task_message, book_title) -> None:
        super().__init__(task_message)
        self.start_time = self.end_time = datetime.now()
        super().stat = STAT_FINISH_SUCCESS
        super().progress = 1
        self.book_title = book_title

    def run(self, worker_thread) -> None:
        """Upload task doesn't have anything to do, it's simply a way to add information to the task list."""

    def name(self):
        return N_("Upload")

    def __str__(self) -> str:
        return f"Upload {self.book_title}"

    def is_cancellable(self) -> bool:
        return False
