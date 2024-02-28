
#   This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#     Copyright (C) 2023 OzzieIsaacs
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


from calibre_web import file_helper, logger
from calibre_web.services.worker import CalibreTask
from flask_babel import lazy_gettext as N_


class TaskDeleteTempFolder(CalibreTask):
    def __init__(self, task_message=N_("Delete temp folder contents")) -> None:
        super().__init__(task_message)
        self.log = logger.create()

    def run(self, worker_thread) -> None:
        try:
            file_helper.del_temp_dir()
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as e:
            self.log.exception(f"Error deleting temp folder: {e}")
        self._handleSuccess()

    @property
    def name(self) -> str:
        return "Delete Temp Folder"

    @property
    def is_cancellable(self) -> bool:
        return False
