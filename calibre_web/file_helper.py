
#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2023 OzzieIsaacs
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

import os
import shutil
from tempfile import gettempdir


def get_temp_dir():
    tmp_dir = os.path.join(gettempdir(), "calibre-web")
    if not os.path.isdir(tmp_dir):
        os.mkdir(tmp_dir)
    return tmp_dir


def del_temp_dir() -> None:
    tmp_dir = os.path.join(gettempdir(), "calibre-web")
    shutil.rmtree(tmp_dir)
