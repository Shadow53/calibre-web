#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2012-2019 cervinko, idalin, SiphonSquirrel, ouzklcn, akushsky,
#                            OzzieIsaacs, bodybybuddha, jkrehm, matthazinski, janeczku
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

import io
import mimetypes
import os
import random
import re
import shutil
import socket
from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import requests
import unidecode
from flask import abort, make_response, send_from_directory, url_for
from flask_babel import get_locale
from flask_babel import gettext as _
from flask_babel import lazy_gettext as N_
from flask_login import current_user
from markupsafe import escape
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.sql.expression import and_, false, func, or_, text, true
from werkzeug.datastructures import Headers
from werkzeug.security import generate_password_hash

from . import db, fs, logger, ub
from .config_sql import CONFIG
from .constants import CACHE_TYPE_THUMBNAILS, SUPPORTED_CALIBRE_BINARIES, THUMBNAIL_TYPE_COVER, THUMBNAIL_TYPE_SERIES
from .constants import STATIC_DIR as _STATIC_DIR
from .db import calibre_db
from .epub_helper import create_new_metadata_backup, get_content_opf, replace_metadata, updateEpub
from .file_helper import get_temp_dir
from .services.worker import WorkerThread
from .subproc_wrapper import process_open, process_wait
from .tasks.convert import TaskConvert
from .tasks.mail import TaskEmail
from .tasks.metadata_backup import TaskBackupMetadata
from .tasks.thumbnail import TaskClearCoverThumbnailCache, TaskGenerateCoverThumbnails

log = logger.create()

try:
    from wand.exceptions import BlobError, MissingDelegateError
    from wand.image import Image

    use_IM = True
except (ImportError, RuntimeError) as e:
    log.debug("Cannot import Image, generating covers from non jpg files will not work: %s", e)
    use_IM = False
    MissingDelegateError = BaseException


# Convert existing book entry to new format
def convert_book_format(book_id, calibre_path, old_book_format, new_book_format, user_id, ereader_mail=None):
    book = calibre_db.get_book(book_id)
    data = calibre_db.get_book_format(book.id, old_book_format)
    if not data:
        error_message = _("%(format)s format not found for book id: %(book)d", format=old_book_format, book=book_id)
        log.error("convert_book_format: %s", error_message)
        return error_message
    file_path = os.path.join(calibre_path, book.path, data.name)
    if not os.path.exists(file_path + "." + old_book_format.lower()):
        return _("%(format)s not found: %(fn)s", format=old_book_format, fn=data.name + "." + old_book_format.lower())
    # read settings and append converter task to queue
    if ereader_mail:
        settings = CONFIG.get_mail_settings()
        settings["subject"] = _("Send to eReader")  # pretranslate Subject for Email
        settings["body"] = _("This Email has been sent via Calibre-Web.")
    else:
        settings = {}
    link = '<a href="{}">{}</a>'.format(url_for("web.show_book", book_id=book.id), escape(book.title))  # prevent xss
    txt = f"{old_book_format.upper()} -> {new_book_format.upper()}: {link}"
    settings["old_book_format"] = old_book_format
    settings["new_book_format"] = new_book_format
    WorkerThread.add(user_id, TaskConvert(file_path, book.id, txt, settings, ereader_mail, user_id))
    return None


# Texts are not lazy translated as they are supposed to get send out as is
def send_test_mail(ereader_mail, user_name) -> None:
    WorkerThread.add(
        user_name,
        TaskEmail(
            _("Calibre-Web Test Email"),
            None,
            None,
            CONFIG.get_mail_settings(),
            ereader_mail,
            N_("Test Email"),
            _("This Email has been sent via Calibre-Web."),
        ),
    )


# Send registration email or password reset email, depending on parameter resend (False means welcome email)
def send_registration_mail(e_mail, user_name, default_password, resend=False) -> None:
    txt = "Hi %s!\r\n" % user_name
    if not resend:
        txt += "Your account at Calibre-Web has been created.\r\n"
    txt += "Please log in using the following information:\r\n"
    txt += "Username: %s\r\n" % user_name
    txt += "Password: %s\r\n" % default_password
    txt += "Don't forget to change your password after your first login.\r\n"
    txt += "Regards,\r\n\r\n"
    txt += "Calibre-Web"
    WorkerThread.add(
        None,
        TaskEmail(
            subject=_("Get Started with Calibre-Web"),
            filepath=None,
            attachment=None,
            settings=CONFIG.get_mail_settings(),
            recipient=e_mail,
            task_message=N_("Registration Email for user: %(name)s", name=user_name),
            text=txt,
        ),
    )


def check_send_to_ereader_with_converter(formats):
    book_formats = []
    if "MOBI" in formats and "EPUB" not in formats:
        book_formats.append(
            {
                "format": "Epub",
                "convert": 1,
                "text": _("Convert %(orig)s to %(format)s and send to eReader", orig="Mobi", format="Epub"),
            }
        )
    if "AZW3" in formats and "EPUB" not in formats:
        book_formats.append(
            {
                "format": "Epub",
                "convert": 2,
                "text": _("Convert %(orig)s to %(format)s and send to eReader", orig="Azw3", format="Epub"),
            }
        )
    return book_formats


def check_send_to_ereader(entry):
    """Returns all available book formats for sending to eReader."""
    formats = []
    book_formats = []
    if len(entry.data):
        for ele in iter(entry.data):
            if ele.uncompressed_size < CONFIG.mail_size:
                formats.append(ele.format)
        if "EPUB" in formats:
            book_formats.append(
                {"format": "Epub", "convert": 0, "text": _("Send %(format)s to eReader", format="Epub")}
            )
        if "PDF" in formats:
            book_formats.append({"format": "Pdf", "convert": 0, "text": _("Send %(format)s to eReader", format="Pdf")})
        if "AZW" in formats:
            book_formats.append({"format": "Azw", "convert": 0, "text": _("Send %(format)s to eReader", format="Azw")})
        if CONFIG.config_converterpath:
            book_formats.extend(check_send_to_ereader_with_converter(formats))
        return book_formats
    else:
        log.error("Cannot find book entry %d", entry.id)
        return None


# Check if a reader is existing for any of the book formats, if not, return empty list, otherwise return
# list with supported formats
def check_read_formats(entry):
    extensions_reader = {"TXT", "PDF", "EPUB", "CBZ", "CBT", "CBR", "DJVU", "DJV"}
    book_formats = []
    if len(entry.data):
        for ele in iter(entry.data):
            if ele.format.upper() in extensions_reader:
                book_formats.append(ele.format.lower())
    return book_formats


# Files are processed in the following order/priority:
# 1: If epub file is existing, it's directly send to eReader email,
# 2: If mobi file is existing, it's converted and send to eReader email,
# 3: If Pdf file is existing, it's directly send to eReader email
def send_mail(book_id, book_format, convert, ereader_mail, calibrepath, user_id):
    """Send email with attachments."""
    book = calibre_db.get_book(book_id)

    if convert == 1:
        # returns None if success, otherwise errormessage
        return convert_book_format(book_id, calibrepath, "mobi", book_format.lower(), user_id, ereader_mail)
    if convert == 2:
        # returns None if success, otherwise errormessage
        return convert_book_format(book_id, calibrepath, "azw3", book_format.lower(), user_id, ereader_mail)

    for entry in iter(book.data):
        if entry.format.upper() == book_format.upper():
            converted_file_name = entry.name + "." + book_format.lower()
            link = '<a href="{}">{}</a>'.format(url_for("web.show_book", book_id=book_id), escape(book.title))
            email_text = N_("%(book)s send to eReader", book=link)
            WorkerThread.add(
                user_id,
                TaskEmail(
                    _("Send to eReader"),
                    book.path,
                    converted_file_name,
                    CONFIG.get_mail_settings(),
                    ereader_mail,
                    email_text,
                    _("This Email has been sent via Calibre-Web."),
                ),
            )
            return None
    return _("The requested file could not be read. Maybe wrong permissions?")


def get_valid_filename(value, replace_whitespace=True, chars=128):
    """Returns the given string converted to a string that can be used for a clean
    filename. Limits num characters to 128 max.
    """
    if value[-1:] == ".":
        value = value[:-1] + "_"
    value = value.replace("/", "_").replace(":", "_").strip("\0")
    if CONFIG.config_unicode_filename:
        value = unidecode.unidecode(value)
    if replace_whitespace:
        #  *+:\"/<>? are replaced by _
        value = re.sub(r"[*+:\\\"/<>?]+", "_", value, flags=re.U)
        # pipe has to be replaced with comma
        value = re.sub(r"[|]+", ",", value, flags=re.U)

    value = value.encode("utf-8")[:chars].decode("utf-8", errors="ignore").strip()

    if not value:
        msg = "Filename cannot be empty"
        raise ValueError(msg)
    return value


def split_authors(values):
    authors_list = []
    for value in values:
        authors = re.split("[&;]", value)
        for author in authors:
            commas = author.count(",")
            if commas == 1:
                author_split = author.split(",")
                authors_list.append(author_split[1].strip() + " " + author_split[0].strip())
            elif commas > 1:
                authors_list.extend([x.strip() for x in author.split(",")])
            else:
                authors_list.append(author.strip())
    return authors_list


def get_sorted_author(value):
    value2 = None
    try:
        if "," not in value:
            regexes = [r"^(JR|SR)\.?$", r"^I{1,3}\.?$", r"^IV\.?$"]
            combined = "(" + ")|(".join(regexes) + ")"
            value = value.split(" ")
            if re.match(combined, value[-1].upper()):
                value2 = value[-2] + ", " + " ".join(value[:-2]) + " " + value[-1] if len(value) > 1 else value[0]
            elif len(value) == 1:
                value2 = value[0]
            else:
                value2 = value[-1] + ", " + " ".join(value[:-1])
        else:
            value2 = value
    except Exception as ex:
        log.exception("Sorting author %s failed: %s", value, ex)
        value2 = value[0] if isinstance(list, value2) else value
    return value2


def edit_book_read_status(book_id, read_status=None):
    if not CONFIG.config_read_column:
        book = (
            ub.session.query(ub.ReadBook)
            .filter(and_(ub.ReadBook.user_id == int(current_user.id), ub.ReadBook.book_id == book_id))
            .first()
        )
        if book:
            if read_status is None:
                if book.read_status == ub.ReadBook.STATUS_FINISHED:
                    book.read_status = ub.ReadBook.STATUS_UNREAD
                else:
                    book.read_status = ub.ReadBook.STATUS_FINISHED
            else:
                book.read_status = ub.ReadBook.STATUS_FINISHED if read_status else ub.ReadBook.STATUS_UNREAD
        else:
            read_book = ub.ReadBook(user_id=current_user.id, book_id=book_id)
            read_book.read_status = ub.ReadBook.STATUS_FINISHED
            book = read_book
        if not book.kobo_reading_state:
            kobo_reading_state = ub.KoboReadingState(user_id=current_user.id, book_id=book_id)
            kobo_reading_state.current_bookmark = ub.KoboBookmark()
            kobo_reading_state.statistics = ub.KoboStatistics()
            book.kobo_reading_state = kobo_reading_state
        ub.session.merge(book)
        ub.session_commit(f"Book {book_id} readbit toggled")
    else:
        try:
            calibre_db.update_title_sort(CONFIG)
            book = calibre_db.get_filtered_book(book_id)
            book_read_status = getattr(book, "custom_column_" + str(CONFIG.config_read_column))
            if len(book_read_status):
                if read_status is None:
                    book_read_status[0].value = not book_read_status[0].value
                else:
                    book_read_status[0].value = read_status is True
                calibre_db.session.commit()
            else:
                cc_class = db.cc_classes[CONFIG.config_read_column]
                new_cc = cc_class(value=read_status or 1, book=book_id)
                calibre_db.session.add(new_cc)
                calibre_db.session.commit()
        except (KeyError, AttributeError, IndexError):
            log.exception(f"Custom Column No.{CONFIG.config_read_column} does not exist in calibre database")
            return f"Custom Column No.{CONFIG.config_read_column} does not exist in calibre database"
        except (OperationalError, InvalidRequestError) as ex:
            calibre_db.session.rollback()
            log.exception(f"Read status could not set: {ex}")
            return _(f"Read status could not set: {ex.orig}")
    return ""


# Deletes a book from the local filestorage, returns True if deleting is successful, otherwise false
def delete_book_file(book, calibrepath, book_format=None):
    # check that path is 2 elements deep, check that target path has no sub folders
    if book.path.count("/") == 1:
        path = os.path.join(calibrepath, book.path)
        if book_format:
            for file in os.listdir(path):
                if file.upper().endswith("." + book_format):
                    os.remove(os.path.join(path, file))
            return True, None
        elif os.path.isdir(path):
            try:
                for root, folders, files in os.walk(path):
                    for f in files:
                        os.unlink(os.path.join(root, f))
                    if len(folders):
                        log.warning(f"Deleting book {book.id} failed, path {book.path} has subfolders: {folders}")
                        return True, _(
                            "Deleting bookfolder for book %(id)s failed, path has subfolders: %(path)s",
                            id=book.id,
                            path=book.path,
                        )
                shutil.rmtree(path)
            except OSError as ex:
                log.exception("Deleting book %s failed: %s", book.id, ex)
                return False, _("Deleting book %(id)s failed: %(message)s", id=book.id, message=ex)
            authorpath = os.path.join(calibrepath, os.path.split(book.path)[0])
            if not os.listdir(authorpath):
                try:
                    shutil.rmtree(authorpath)
                except OSError as ex:
                    log.exception("Deleting authorpath for book %s failed: %s", book.id, ex)
            return True, None

    log.error("Deleting book %s from database only, book path in database not valid: %s", book.id, book.path)
    return True, _(
        "Deleting book %(id)s from database only, book path in database not valid: %(path)s", id=book.id, path=book.path
    )


def clean_author_database(renamed_author, calibre_path="", local_book=None) -> None:
    valid_filename_authors = [get_valid_filename(r, chars=96) for r in renamed_author]
    for r in renamed_author:
        if local_book:
            all_books = [local_book]
        else:
            all_books = calibre_db.session.query(db.Books).filter(db.Books.authors.any(db.Authors.name == r)).all()
        for book in all_books:
            book_author_path = book.path.split("/")[0]
            if book_author_path in valid_filename_authors or local_book:
                new_author = calibre_db.session.query(db.Authors).filter(db.Authors.name == r).first()
                all_new_authordir = get_valid_filename(new_author.name, chars=96)
                all_titledir = book.path.split("/")[1]
                all_new_path = os.path.join(calibre_path, all_new_authordir, all_titledir)
                all_new_name = (
                    get_valid_filename(book.title, chars=42) + " - " + get_valid_filename(new_author.name, chars=42)
                )
                # change location in database to new author/title path
                book.path = os.path.join(all_new_authordir, all_titledir).replace("\\", "/")
                for file_format in book.data:
                    shutil.move(
                        os.path.normcase(
                            os.path.join(all_new_path, file_format.name + "." + file_format.format.lower())
                        ),
                        os.path.normcase(os.path.join(all_new_path, all_new_name + "." + file_format.format.lower())),
                    )
                    file_format.name = all_new_name


def rename_all_authors(first_author, renamed_author, calibre_path="", localbook=None):
    # Create new_author_dir from parameter or from database
    # Create new title_dir from database and add id
    if first_author:
        new_authordir = get_valid_filename(first_author, chars=96)
        for r in renamed_author:
            new_author = calibre_db.session.query(db.Authors).filter(db.Authors.name == r).first()
            old_author_dir = get_valid_filename(r, chars=96)
            new_author_rename_dir = get_valid_filename(new_author.name, chars=96)
            if os.path.isdir(os.path.join(calibre_path, old_author_dir)):
                try:
                    old_author_path = os.path.join(calibre_path, old_author_dir)
                    new_author_path = os.path.join(calibre_path, new_author_rename_dir)
                    shutil.move(os.path.normcase(old_author_path), os.path.normcase(new_author_path))
                except OSError as ex:
                    log.exception("Rename author from: %s to %s: %s", old_author_path, new_author_path, ex)
                    log.debug(ex, exc_info=True)
                    return _(
                        "Rename author from: '%(src)s' to '%(dest)s' failed with error: %(error)s",
                        src=old_author_path,
                        dest=new_author_path,
                        error=str(ex),
                    )
    else:
        new_authordir = get_valid_filename(localbook.authors[0].name, chars=96)
    return new_authordir


# Moves files in file storage during author/title rename, or from temp dir to file storage
def update_dir_structure_file(book_id, calibre_path, first_author, original_filepath, db_filename, renamed_author):
    # get book database entry from id, if original path overwrite source with original_filepath
    local_book = calibre_db.get_book(book_id)
    path = original_filepath if original_filepath else os.path.join(calibre_path, local_book.path)

    # Create (current) author_dir and title_dir from database
    author_dir = local_book.path.split("/")[0]
    title_dir = local_book.path.split("/")[1]

    # Create new_author_dir from parameter or from database
    # Create new title_dir from database and add id
    new_author_dir = rename_all_authors(first_author, renamed_author, calibre_path, local_book)
    if first_author and first_author.lower() in [r.lower() for r in renamed_author]:
        if os.path.isdir(os.path.join(calibre_path, new_author_dir)):
            path = os.path.join(calibre_path, new_author_dir, title_dir)

    new_title_dir = get_valid_filename(local_book.title, chars=96) + " (" + str(book_id) + ")"

    if title_dir != new_title_dir or author_dir != new_author_dir or original_filepath:
        error = move_files_on_change(
            calibre_path, new_author_dir, new_title_dir, local_book, db_filename, original_filepath, path
        )
        if error:
            return error

    # Rename all files from old names to new names
    return rename_files_on_change(first_author, renamed_author, local_book, original_filepath, path, calibre_path)


def move_files_on_change(calibre_path, new_authordir, new_titledir, localbook, db_filename, original_filepath, path):
    new_path = os.path.join(calibre_path, new_authordir, new_titledir)
    new_name = get_valid_filename(localbook.title, chars=96) + " - " + new_authordir
    try:
        if original_filepath:
            if not os.path.isdir(new_path):
                os.makedirs(new_path)
            shutil.move(os.path.normcase(original_filepath), os.path.normcase(os.path.join(new_path, db_filename)))
            log.debug("Moving title: %s to %s/%s", original_filepath, new_path, new_name)
        elif not os.path.exists(new_path):
            # move original path to new path
            log.debug("Moving title: %s to %s", path, new_path)
            shutil.move(os.path.normcase(path), os.path.normcase(new_path))
        else:  # path is valid copy only files to new location (merge)
            log.info("Moving title: %s into existing: %s", path, new_path)
            # Take all files and subfolder from old path (strange command)
            for dir_name, __, file_list in os.walk(path):
                for file in file_list:
                    shutil.move(
                        os.path.normcase(os.path.join(dir_name, file)),
                        os.path.normcase(os.path.join(new_path + dir_name[len(path) :], file)),
                    )
        # change location in database to new author/title path
        localbook.path = os.path.join(new_authordir, new_titledir).replace("\\", "/")
    except OSError as ex:
        log.error_or_exception(f"Rename title from {path} to {new_path} failed with error: {ex}")
        return _(
            "Rename title from: '%(src)s' to '%(dest)s' failed with error: %(error)s",
            src=path,
            dest=new_path,
            error=str(ex),
        )
    return False


def rename_files_on_change(first_author, renamed_author, local_book, original_filepath="", path="", calibre_path=""):
    # Rename all files from old names to new names
    try:
        clean_author_database(renamed_author, calibre_path)
        if first_author and first_author not in renamed_author:
            clean_author_database([first_author], calibre_path, local_book)
        if not renamed_author and not original_filepath and len(os.listdir(os.path.dirname(path))) == 0:
            shutil.rmtree(os.path.dirname(path))
    except (OSError, FileNotFoundError) as ex:
        log.error_or_exception(f"Error in rename file in path {ex}")
        return _(f"Error in rename file in path: {ex!s}")
    return False


def reset_password(user_id):
    existing_user = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
    if not existing_user:
        return 0, None
    if not CONFIG.get_mail_server_configured():
        return 2, None
    try:
        password = generate_random_password(CONFIG.config_password_min_length)
        existing_user.password = generate_password_hash(password)
        ub.session.commit()
        send_registration_mail(existing_user.email, existing_user.name, password, True)
        return 1, existing_user.name
    except Exception:
        ub.session.rollback()
        return 0, None


def generate_random_password(min_length):
    min_length = max(8, min_length) - 4
    random_source = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%&*()?"
    # select 1 lowercase
    s = "abcdefghijklmnopqrstuvwxyz"
    password = [s[c % len(s)] for c in os.urandom(1)]
    # select 1 uppercase
    s = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    password.extend([s[c % len(s)] for c in os.urandom(1)])
    # select 1 digit
    s = "01234567890"
    password.extend([s[c % len(s)] for c in os.urandom(1)])
    # select 1 special symbol
    s = "!@#$%&*()?"
    password.extend([s[c % len(s)] for c in os.urandom(1)])

    # generate other characters
    password.extend([random_source[c % len(random_source)] for c in os.urandom(min_length)])

    # password_list = list(password)
    # shuffle all characters
    random.SystemRandom().shuffle(password)
    return "".join(password)


"""def generate_random_password(min_length):
    s = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%&*()?"
    passlen = min_length
    return "".join(s[c % len(s)] for c in os.urandom(passlen))"""


def uniq(inpt):
    output = []
    inpt = [" ".join(inp.split()) for inp in inpt]
    for x in inpt:
        if x not in output:
            output.append(x)
    return output


def check_email(email):
    email = valid_email(email)
    if ub.session.query(ub.User).filter(func.lower(ub.User.email) == email.lower()).first():
        log.error("Found an existing account for this Email address")
        raise Exception(_("Found an existing account for this Email address"))
    return email


def check_username(username):
    username = username.strip()
    if ub.session.query(ub.User).filter(func.lower(ub.User.name) == username.lower()).scalar():
        log.error("This username is already taken")
        raise Exception(_("This username is already taken"))
    return username


def valid_email(email):
    email = email.strip()
    # if email is not deleted
    if email:
        # Regex according to https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/email#validation
        if not re.search(
            r"^[\w.!#$%&'*+\\/=?^_`{|}~-]+@[\w](?:[\w-]{0,61}[\w])?(?:\.[\w](?:[\w-]{0,61}[\w])?)*$", email
        ):
            log.error("Invalid Email address format")
            raise Exception(_("Invalid Email address format"))
    return email


def valid_password(check_password):
    if CONFIG.config_password_policy:
        verify = ""
        if CONFIG.config_password_min_length > 0:
            verify += "^(?=.{" + str(CONFIG.config_password_min_length) + ",}$)"
        if CONFIG.config_password_number:
            verify += r"(?=.*?\d)"
        if CONFIG.config_password_lower:
            verify += "(?=.*?[a-z])"
        if CONFIG.config_password_upper:
            verify += "(?=.*?[A-Z])"
        if CONFIG.config_password_special:
            verify += r"(?=.*?[^A-Za-z\s0-9])"
        match = re.match(verify, check_password)
        if not match:
            raise Exception(_("Password doesn't comply with password validation rules"))
    return check_password


# ################################# External interface #################################


def update_dir_structure(
    book_id,
    calibre_path,
    first_author=None,  # change author of book to this author
    original_filepath=None,
    db_filename=None,
    renamed_author=None,
):
    renamed_author = renamed_author or []
    return update_dir_structure_file(
        book_id, calibre_path, first_author, original_filepath, db_filename, renamed_author
    )


def delete_book(book, calibrepath, book_format):
    if not book_format:
        clear_cover_thumbnail_cache(book.id)  ## here it breaks
        calibre_db.delete_dirty_metadata(book.id)
    return delete_book_file(book, calibrepath, book_format)


def get_cover_on_failure():
    try:
        return send_from_directory(_STATIC_DIR, "generic_cover.jpg")
    except PermissionError:
        log.exception("No permission to access generic_cover.jpg file.")
        abort(403)


def get_book_cover(book_id, resolution=None):
    book = calibre_db.get_filtered_book(book_id, allow_show_archived=True)
    return get_book_cover_internal(book, resolution=resolution)


def get_book_cover_with_uuid(book_uuid, resolution=None):
    book = calibre_db.get_book_by_uuid(book_uuid)
    if not book:
        return None  # allows kobo.HandleCoverImageRequest to proxy request
    return get_book_cover_internal(book, resolution=resolution)


def get_book_cover_internal(book, resolution=None):
    if book and book.has_cover:
        # Send the book cover thumbnail if it exists in cache
        if resolution:
            thumbnail = get_book_cover_thumbnail(book, resolution)
            if thumbnail:
                cache = fs.FileSystem()
                if cache.get_cache_file_exists(thumbnail.filename, CACHE_TYPE_THUMBNAILS):
                    return send_from_directory(
                        cache.get_cache_file_dir(thumbnail.filename, CACHE_TYPE_THUMBNAILS), thumbnail.filename
                    )

        # Send the book cover from the Calibre directory
        cover_file_path = os.path.join(CONFIG.get_book_path(), book.path)
        if os.path.isfile(os.path.join(cover_file_path, "cover.jpg")):
            return send_from_directory(cover_file_path, "cover.jpg")
        else:
            return get_cover_on_failure()
    else:
        return get_cover_on_failure()


def get_book_cover_thumbnail(book, resolution):
    if book and book.has_cover:
        return (
            ub.session.query(ub.Thumbnail)
            .filter(ub.Thumbnail.type == THUMBNAIL_TYPE_COVER)
            .filter(ub.Thumbnail.entity_id == book.id)
            .filter(ub.Thumbnail.resolution == resolution)
            .filter(or_(ub.Thumbnail.expiration.is_(None), ub.Thumbnail.expiration > datetime.utcnow()))
            .first()
        )
    return None


def get_series_thumbnail_on_failure(series_id, resolution):
    book = (
        calibre_db.session.query(db.Books)
        .join(db.books_series_link)
        .join(db.Series)
        .filter(db.Series.id == series_id)
        .filter(db.Books.has_cover == 1)
        .first()
    )

    return get_book_cover_internal(book, resolution=resolution)


def get_series_cover_thumbnail(series_id, resolution=None):
    return get_series_cover_internal(series_id, resolution)


def get_series_cover_internal(series_id, resolution=None):
    # Send the series thumbnail if it exists in cache
    if resolution:
        thumbnail = get_series_thumbnail(series_id, resolution)
        if thumbnail:
            cache = fs.FileSystem()
            if cache.get_cache_file_exists(thumbnail.filename, CACHE_TYPE_THUMBNAILS):
                return send_from_directory(
                    cache.get_cache_file_dir(thumbnail.filename, CACHE_TYPE_THUMBNAILS), thumbnail.filename
                )

    return get_series_thumbnail_on_failure(series_id, resolution)


def get_series_thumbnail(series_id, resolution):
    return (
        ub.session.query(ub.Thumbnail)
        .filter(ub.Thumbnail.type == THUMBNAIL_TYPE_SERIES)
        .filter(ub.Thumbnail.entity_id == series_id)
        .filter(ub.Thumbnail.resolution == resolution)
        .filter(or_(ub.Thumbnail.expiration.is_(None), ub.Thumbnail.expiration > datetime.utcnow()))
        .first()
    )


# saves book cover from url
def save_cover_from_url(url, book_path):
    try:
        # TODO: this used to use `advocate` to avoid SSRF attacks
        img = requests.get(url, timeout=(10, 200), allow_redirects=False)  # TODO: Error Handling
        img.raise_for_status()
        return save_cover(img, book_path)
    except (
        socket.gaierror,
        requests.exceptions.HTTPError,
        requests.exceptions.InvalidURL,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as ex:
        # "Invalid host" can be the result of a redirect response
        log.exception("Cover Download Error %s", ex)
        return False, _("Error Downloading Cover")
    except MissingDelegateError as ex:
        log.info("File Format Error %s", ex)
        return False, _("Cover Format Error")


def save_cover_from_filestorage(filepath, saved_filename, img):
    # check if file path exists, otherwise create it, copy file to calibre path and delete temp file
    if not os.path.exists(filepath):
        try:
            os.makedirs(filepath)
        except OSError:
            log.exception("Failed to create path for cover")
            return False, _("Failed to create path for cover")
    try:
        # upload of jgp file without wand
        if isinstance(img, requests.Response):
            with open(os.path.join(filepath, saved_filename), "wb") as f:
                f.write(img.content)
        elif hasattr(img, "metadata"):
            # upload of jpg/png... via url
            img.save(filename=os.path.join(filepath, saved_filename))
            img.close()
        else:
            # upload of jpg/png... from hdd
            img.save(os.path.join(filepath, saved_filename))
    except OSError:
        log.exception("Cover-file is not a valid image file, or could not be stored")
        return False, _("Cover-file is not a valid image file, or could not be stored")
    return True, None


def save_cover(img, book_path):
    content_type = img.headers.get("content-type")

    if use_IM:
        if content_type not in ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp"):
            log.error("Only jpg/jpeg/png/webp/bmp files are supported as coverfile")
            return False, _("Only jpg/jpeg/png/webp/bmp files are supported as coverfile")
        # convert to jpg because calibre only supports jpg
        try:
            imgc = Image(blob=img.stream) if hasattr(img, "stream") else Image(blob=io.BytesIO(img.content))
            imgc.format = "jpeg"
            imgc.transform_colorspace("rgb")
            img = imgc
        except (BlobError, MissingDelegateError):
            log.exception("Invalid cover file content")
            return False, _("Invalid cover file content")
    elif content_type not in ["image/jpeg", "image/jpg"]:
        log.error("Only jpg/jpeg files are supported as coverfile")
        return False, _("Only jpg/jpeg files are supported as coverfile")

    return save_cover_from_filestorage(os.path.join(CONFIG.get_book_path(), book_path), "cover.jpg", img)


def do_download_file(book, book_format, client, data, headers):
    book_name = data.name
    filename = os.path.join(CONFIG.get_book_path(), book.path)
    if not os.path.isfile(os.path.join(filename, book_name + "." + book_format)):
        # TODO: improve error handling
        log.error("File not found: %s", os.path.join(filename, book_name + "." + book_format))

    if client == "kobo" and book_format == "kepub":
        headers["Content-Disposition"] = headers["Content-Disposition"].replace(".kepub", ".kepub.epub")

    if book_format == "kepub" and CONFIG.config_kepubifypath and CONFIG.config_embed_metadata:
        filename, download_name = do_kepubify_metadata_replace(
            book, os.path.join(filename, book_name + "." + book_format)
        )
    elif book_format != "kepub" and CONFIG.config_binariesdir and CONFIG.config_embed_metadata:
        filename, download_name = do_calibre_export(book.id, book_format)
    else:
        download_name = book_name

    response = make_response(send_from_directory(filename, download_name + "." + book_format))
    # TODO Check headers parameter
    for element in headers:
        response.headers[element[0]] = element[1]
    log.info("Downloading file: {}".format(os.path.join(filename, book_name + "." + book_format)))
    return response


def do_kepubify_metadata_replace(book, file_path):
    custom_columns = (
        calibre_db.session.query(db.CustomColumns)
        .filter(db.CustomColumns.mark_for_delete == 0)
        .filter(db.CustomColumns.datatype.notin_(db.cc_exceptions))
        .order_by(db.CustomColumns.label)
        .all()
    )

    tree, cf_name = get_content_opf(file_path)
    package = create_new_metadata_backup(book, custom_columns, current_user.locale, _("Cover"), lang_type=2)
    content = replace_metadata(tree, package)
    tmp_dir = get_temp_dir()
    temp_file_name = str(uuid4())
    # open zipfile and replace metadata block in content.opf
    updateEpub(file_path, os.path.join(tmp_dir, temp_file_name + ".kepub"), cf_name, content)
    return tmp_dir, temp_file_name


def do_calibre_export(
    book_id,
    book_format,
):
    try:
        quotes = [3, 5, 7, 9]
        tmp_dir = get_temp_dir()
        calibredb_binarypath = get_calibre_binarypath("calibredb")
        temp_file_name = str(uuid4())
        my_env = os.environ.copy()
        if CONFIG.config_calibre_split:
            my_env["CALIBRE_OVERRIDE_DATABASE_PATH"] = os.path.join(CONFIG.config_calibre_dir, "metadata.db")
            library_path = CONFIG.config_calibre_split_dir
        else:
            library_path = CONFIG.config_calibre_dir
        opf_command = [
            calibredb_binarypath,
            "export",
            "--dont-write-opf",
            "--with-library",
            library_path,
            "--to-dir",
            tmp_dir,
            "--formats",
            book_format,
            "--template",
            f"{temp_file_name}",
            str(book_id),
        ]
        # CALIBRE_OVERRIDE_DATABASE_PATH
        p = process_open(opf_command, quotes, my_env)
        _, err = p.communicate()
        if err:
            log.error("Metadata embedder encountered an error: %s", err)
        return tmp_dir, temp_file_name
    except OSError as ex:
        # TODO real error handling
        log.error_or_exception(ex)
        return None, None


##################################


def check_unrar(unrar_location):
    if not unrar_location:
        return None

    if not os.path.exists(unrar_location):
        return _("Unrar binary file not found")

    try:
        unrar_location = [unrar_location]
        value = process_wait(unrar_location, pattern="UNRAR (.*) freeware")
        if value:
            version = value.group(1)
            log.debug("unrar version %s", version)

    except (OSError, UnicodeDecodeError) as err:
        log.error_or_exception(err)
        return _("Error executing UnRar")


def check_calibre(calibre_location):
    if not calibre_location:
        return None

    if not os.path.exists(calibre_location):
        return _("Could not find the specified directory")

    if not os.path.isdir(calibre_location):
        return _("Please specify a directory, not a file")

    try:
        supported_binary_paths = [
            os.path.join(calibre_location, binary) for binary in SUPPORTED_CALIBRE_BINARIES.values()
        ]
        binaries_available = [os.path.isfile(binary_path) for binary_path in supported_binary_paths]
        binaries_executable = [os.access(binary_path, os.X_OK) for binary_path in supported_binary_paths]
        if all(binaries_available) and all(binaries_executable):
            values = [
                process_wait([binary_path, "--version"], pattern=r"\(calibre (.*)\)")
                for binary_path in supported_binary_paths
            ]
            if all(values):
                version = values[0].group(1)
                log.debug("calibre version %s", version)
            else:
                return _("Calibre binaries not viable")
        else:
            ret_val = []
            missing_binaries = [
                path
                for path, available in zip(SUPPORTED_CALIBRE_BINARIES.values(), binaries_available)
                if not available
            ]

            missing_perms = [
                path
                for path, available in zip(SUPPORTED_CALIBRE_BINARIES.values(), binaries_executable)
                if not available
            ]
            if missing_binaries:
                ret_val.append(_("Missing calibre binaries: %(missing)s", missing=", ".join(missing_binaries)))
            if missing_perms:
                ret_val.append(_("Missing executable permissions: %(missing)s", missing=", ".join(missing_perms)))
            return ", ".join(ret_val)

    except (OSError, UnicodeDecodeError) as err:
        log.error_or_exception(err)
        return _("Error excecuting Calibre")


def json_serial(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return {
            "__type__": "timedelta",
            "days": obj.days,
            "seconds": obj.seconds,
            "microseconds": obj.microseconds,
        }
    raise TypeError("Type %s not serializable" % type(obj))


def tags_filters():
    negtags_list = current_user.list_denied_tags()
    postags_list = current_user.list_allowed_tags()
    neg_content_tags_filter = false() if negtags_list == [""] else db.Tags.name.in_(negtags_list)
    pos_content_tags_filter = true() if postags_list == [""] else db.Tags.name.in_(postags_list)
    return and_(pos_content_tags_filter, ~neg_content_tags_filter)


# checks if domain is in database (including wildcards)
# example SELECT * FROM @TABLE WHERE 'abcdefg' LIKE Name;
# from https://code.luasoftware.com/tutorials/flask/execute-raw-sql-in-flask-sqlalchemy/
# in all calls the email address is checked for validity
def check_valid_domain(domain_text) -> bool:
    sql = "SELECT * FROM registration WHERE (:domain LIKE domain and allow = 1);"
    if not len(ub.session.query(ub.Registration).from_statement(text(sql)).params(domain=domain_text).all()):
        return False
    sql = "SELECT * FROM registration WHERE (:domain LIKE domain and allow = 0);"
    return not len(ub.session.query(ub.Registration).from_statement(text(sql)).params(domain=domain_text).all())


def get_download_link(book_id, book_format, client):
    book_format = book_format.split(".")[0]
    book = calibre_db.get_filtered_book(book_id, allow_show_archived=True)
    if book:
        data1 = calibre_db.get_book_format(book.id, book_format.upper())
        if data1:
            # collect downloaded books only for registered user and not for anonymous user
            if current_user.is_authenticated:
                ub.update_download(book_id, int(current_user.id))
            file_name = book.title
            if len(book.authors) > 0:
                file_name = file_name + " - " + book.authors[0].name
            file_name = get_valid_filename(file_name, replace_whitespace=False)
            headers = Headers()
            headers["Content-Type"] = mimetypes.types_map.get("." + book_format, "application/octet-stream")
            headers["Content-Disposition"] = "attachment; filename={}.{}; filename*=UTF-8''{}.{}".format(
                quote(file_name), book_format, quote(file_name), book_format
            )
            return do_download_file(book, book_format, client, data1, headers)
    else:
        log.error(f"Book id {book_id} not found for downloading")
    abort(404)
    return None


def get_calibre_binarypath(binary):
    binariesdir = CONFIG.config_binariesdir
    if binariesdir:
        try:
            return os.path.join(binariesdir, SUPPORTED_CALIBRE_BINARIES[binary])
        except KeyError:
            log.exception("Binary not supported by Calibre-Web: %s", SUPPORTED_CALIBRE_BINARIES[binary])
    return ""


def clear_cover_thumbnail_cache(book_id) -> None:
    if CONFIG.schedule_generate_book_covers:
        WorkerThread.add(None, TaskClearCoverThumbnailCache(book_id), hidden=True)


def replace_cover_thumbnail_cache(book_id) -> None:
    if CONFIG.schedule_generate_book_covers:
        WorkerThread.add(None, TaskClearCoverThumbnailCache(book_id), hidden=True)
        WorkerThread.add(None, TaskGenerateCoverThumbnails(book_id), hidden=True)


def delete_thumbnail_cache() -> None:
    WorkerThread.add(None, TaskClearCoverThumbnailCache(-1))


def add_book_to_thumbnail_cache(book_id) -> None:
    if CONFIG.schedule_generate_book_covers:
        WorkerThread.add(None, TaskGenerateCoverThumbnails(book_id), hidden=True)


def update_thumbnail_cache() -> None:
    if CONFIG.schedule_generate_book_covers:
        WorkerThread.add(None, TaskGenerateCoverThumbnails())


def set_all_metadata_dirty() -> None:
    WorkerThread.add(
        None,
        TaskBackupMetadata(
            export_language=get_locale(),
            translated_title=_("Cover"),
            set_dirty=True,
            task_message=N_("Queue all books for metadata backup"),
        ),
        hidden=False,
    )
