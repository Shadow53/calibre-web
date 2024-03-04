
#   This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#     Copyright (C) 2020 monkey
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

import os

from flask_babel import lazy_gettext as N_
from lxml import etree

from calibre_web import config, db, gdriveutils, logger
from calibre_web.services.worker import CalibreTask

from ..epub_helper import create_new_metadata_backup


class TaskBackupMetadata(CalibreTask):

    def __init__(self, export_language="en",
                 translated_title="Cover",
                 set_dirty=False,
                 task_message=N_("Backing up Metadata")) -> None:
        super().__init__(task_message)
        self.log = logger.create()
        self.calibre_db = db.CalibreDB(expire_on_commit=False, init=True)
        self.export_language = export_language
        self.translated_title = translated_title
        self.set_dirty = set_dirty

    def run(self, worker_thread) -> None:
        if self.set_dirty:
            self.set_all_books_dirty()
        else:
            self.backup_metadata()

    def set_all_books_dirty(self) -> None:
        try:
            books = self.calibre_db.session.query(db.Books).all()
            for book in books:
                self.calibre_db.set_metadata_dirty(book.id)
            self.calibre_db.session.commit()
            self._handleSuccess()
        except Exception as ex:
            self.log.debug("Error adding book for backup: " + str(ex))
            self._handleError("Error adding book for backup: " + str(ex))
            self.calibre_db.session.rollback()
        self.calibre_db.session.close()

    def backup_metadata(self) -> None:
        try:
            metadata_backup = self.calibre_db.session.query(db.Metadata_Dirtied).all()
            custom_columns = (self.calibre_db.session.query(db.CustomColumns)
                              .filter(db.CustomColumns.mark_for_delete == 0)
                              .filter(db.CustomColumns.datatype.notin_(db.cc_exceptions))
                              .order_by(db.CustomColumns.label).all())
            count = len(metadata_backup)
            i = 0
            for backup in metadata_backup:
                book = self.calibre_db.session.query(db.Books).filter(db.Books.id == backup.book).one_or_none()
                self.calibre_db.session.query(db.Metadata_Dirtied).filter(
                    db.Metadata_Dirtied.book == backup.book).delete()
                self.calibre_db.session.commit()
                if book:
                    self.open_metadata(book, custom_columns)
                else:
                    self.log.error(f"Book {backup.book} not found in database")
                i += 1
                self.progress = (1.0 / count) * i
            self._handleSuccess()
            self.calibre_db.session.close()

        except Exception as ex:
            b = "NaN" if not hasattr(book, "id") else book.id
            self.log.debug(f"Error creating metadata backup for book {b}: " + str(ex))
            self._handleError("Error creating metadata backup: " + str(ex))
            self.calibre_db.session.rollback()
            self.calibre_db.session.close()

    def open_metadata(self, book, custom_columns) -> None:
        # package = self.create_new_metadata_backup(book, custom_columns)
        package = create_new_metadata_backup(book, custom_columns, self.export_language, self.translated_title)
        if config.config_use_google_drive:
            if not gdriveutils.is_gdrive_ready():
                msg = "Google Drive is configured but not ready"
                raise Exception(msg)

            gdriveutils.uploadFileToEbooksFolder(os.path.join(book.path, "metadata.opf").replace("\\", "/"),
                                                 etree.tostring(package,
                                                                xml_declaration=True,
                                                                encoding="utf-8",
                                                                pretty_print=True).decode("utf-8"),
                                                 True)
        else:
            # TODO: Handle book folder not found or not readable
            book_metadata_filepath = os.path.join(config.get_book_path(), book.path, "metadata.opf")
            # prepare finalize everything and output
            doc = etree.ElementTree(package)
            try:
                with open(book_metadata_filepath, "wb") as f:
                    doc.write(f, xml_declaration=True, encoding="utf-8", pretty_print=True)
            except Exception as ex:
                msg = f"Writing Metadata failed with error: {ex} "
                raise Exception(msg)

    @property
    def name(self) -> str:
        return "Metadata backup"

    # needed for logging
    def __str__(self) -> str:
        if self.set_dirty:
            return "Queue all books for metadata backup"
        else:
            return "Perform metadata backup"

    @property
    def is_cancellable(self) -> bool:
        return True
