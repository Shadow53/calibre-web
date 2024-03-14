#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2019 OzzieIsaacs, cervinko, jkrehm, bodybybuddha, ok11,
#                            andy29485, idalin, Kyosfonica, wuqi, Kennyl, lemmsh,
#                            falgh1, grunjol, csitko, ytils, xybydy, trasba, vrabe,
#                            ruben-herold, marblepebble, JackED42, SiphonSquirrel,
#                            apetresc, nanu-c, mutschler
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

import sys
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.sql.expression import func, true

from . import db, logger, ub
from .db import calibre_db
from .config_sql import CONFIG
from .render_template import render_title_template
from .usermanagement import login_required_if_no_ano

log = logger.create()

shelf = Blueprint("shelf", __name__)


@shelf.route("/shelf/add/<int:shelf_id>/<int:book_id>", methods=["POST"])
@login_required
def add_to_shelf(shelf_id, book_id):
    xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if shelf is None:
        log.error("Invalid shelf specified: %s", shelf_id)
        if not xhr:
            flash(_("Invalid shelf specified"), category="error")
            return redirect(url_for("web.index"))
        return "Invalid shelf specified", 400

    if not check_shelf_edit_permissions(shelf):
        if not xhr:
            flash(_("Sorry you are not allowed to add a book to that shelf"), category="error")
            return redirect(url_for("web.index"))
        return "Sorry you are not allowed to add a book to the that shelf", 403

    book_in_shelf = (
        ub.session.query(ub.BookShelf).filter(ub.BookShelf.shelf == shelf_id, ub.BookShelf.book_id == book_id).first()
    )
    if book_in_shelf:
        log.error("Book %s is already part of %s", book_id, shelf)
        if not xhr:
            flash(_("Book is already part of the shelf: %(shelfname)s", shelfname=shelf.name), category="error")
            return redirect(url_for("web.index"))
        return "Book is already part of the shelf: %s" % shelf.name, 400

    maxOrder = ub.session.query(func.max(ub.BookShelf.order)).filter(ub.BookShelf.shelf == shelf_id).first()
    maxOrder = 0 if maxOrder[0] is None else maxOrder[0]

    if not calibre_db.session.query(db.Books).filter(db.Books.id == book_id).one_or_none():
        log.error("Invalid Book Id: %s. Could not be added to shelf %s", book_id, shelf.name)
        if not xhr:
            flash(_("%(book_id)s is a invalid Book Id. Could not be added to Shelf", book_id=book_id), category="error")
            return redirect(url_for("web.index"))
        return "%s is a invalid Book Id. Could not be added to Shelf" % book_id, 400

    shelf.books.append(ub.BookShelf(shelf=shelf.id, book_id=book_id, order=maxOrder + 1))
    shelf.last_modified = datetime.utcnow()
    try:
        ub.session.merge(shelf)
        ub.session.commit()
    except (OperationalError, InvalidRequestError) as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
        if "HTTP_REFERER" in request.environ:
            return redirect(request.environ["HTTP_REFERER"])
        else:
            return redirect(url_for("web.index"))
    if not xhr:
        log.debug(f"Book has been added to shelf: {shelf.name}")
        flash(_("Book has been added to shelf: %(sname)s", sname=shelf.name), category="success")
        if "HTTP_REFERER" in request.environ:
            return redirect(request.environ["HTTP_REFERER"])
        else:
            return redirect(url_for("web.index"))
    return "", 204


@shelf.route("/shelf/massadd/<int:shelf_id>", methods=["POST"])
@login_required
def search_to_shelf(shelf_id):
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if shelf is None:
        log.error(f"Invalid shelf specified: {shelf_id}")
        flash(_("Invalid shelf specified"), category="error")
        return redirect(url_for("web.index"))

    if not check_shelf_edit_permissions(shelf):
        log.warning("You are not allowed to add a book to the shelf".format())
        flash(_("You are not allowed to add a book to the shelf"), category="error")
        return redirect(url_for("web.index"))

    if ub.searched_ids.get(current_user.id):
        books_for_shelf = []
        books_in_shelf = ub.session.query(ub.BookShelf).filter(ub.BookShelf.shelf == shelf_id).all()
        if books_in_shelf:
            book_ids = []
            for book_id in books_in_shelf:
                book_ids.append(book_id.book_id)
            for searchid in ub.searched_ids[current_user.id]:
                if searchid not in book_ids:
                    books_for_shelf.append(searchid)
        else:
            books_for_shelf = ub.searched_ids[current_user.id]

        if not books_for_shelf:
            log.error(f"Books are already part of {shelf.name}")
            flash(_("Books are already part of the shelf: %(name)s", name=shelf.name), category="error")
            return redirect(url_for("web.index"))

        maxOrder = ub.session.query(func.max(ub.BookShelf.order)).filter(ub.BookShelf.shelf == shelf_id).first()[0] or 0

        for book in books_for_shelf:
            maxOrder += 1
            shelf.books.append(ub.BookShelf(shelf=shelf.id, book_id=book, order=maxOrder))
        shelf.last_modified = datetime.utcnow()
        try:
            ub.session.merge(shelf)
            ub.session.commit()
            flash(_("Books have been added to shelf: %(sname)s", sname=shelf.name), category="success")
        except (OperationalError, InvalidRequestError) as e:
            ub.session.rollback()
            log.error_or_exception(f"Settings Database error: {e}")
            flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
    else:
        log.error(f"Could not add books to shelf: {shelf.name}")
        flash(_("Could not add books to shelf: %(sname)s", sname=shelf.name), category="error")
    return redirect(url_for("web.index"))


@shelf.route("/shelf/remove/<int:shelf_id>/<int:book_id>", methods=["POST"])
@login_required
def remove_from_shelf(shelf_id, book_id):
    xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if shelf is None:
        log.error(f"Invalid shelf specified: {shelf_id}")
        if not xhr:
            return redirect(url_for("web.index"))
        return "Invalid shelf specified", 400

    # if shelf is public and use is allowed to edit shelfs, or if shelf is private and user is owner
    # allow editing shelfs
    # result   shelf public   user allowed    user owner
    #   false        1             0             x
    #   true         1             1             x
    #   true         0             x             1
    #   false        0             x             0

    if check_shelf_edit_permissions(shelf):
        book_shelf = (
            ub.session.query(ub.BookShelf)
            .filter(ub.BookShelf.shelf == shelf_id, ub.BookShelf.book_id == book_id)
            .first()
        )

        if book_shelf is None:
            log.error("Book %s already removed from %s", book_id, shelf)
            if not xhr:
                return redirect(url_for("web.index"))
            return "Book already removed from shelf", 410

        try:
            ub.session.delete(book_shelf)
            shelf.last_modified = datetime.utcnow()
            ub.session.commit()
        except (OperationalError, InvalidRequestError) as e:
            ub.session.rollback()
            log.error_or_exception(f"Settings Database error: {e}")
            flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
            if "HTTP_REFERER" in request.environ:
                return redirect(request.environ["HTTP_REFERER"])
            else:
                return redirect(url_for("web.index"))
        if not xhr:
            flash(_("Book has been removed from shelf: %(sname)s", sname=shelf.name), category="success")
            if "HTTP_REFERER" in request.environ:
                return redirect(request.environ["HTTP_REFERER"])
            else:
                return redirect(url_for("web.index"))
        return "", 204
    else:
        if not xhr:
            log.warning(f"You are not allowed to remove a book from shelf: {shelf.name}")
            flash(_("Sorry you are not allowed to remove a book from this shelf"), category="error")
            return redirect(url_for("web.index"))
        return "Sorry you are not allowed to remove a book from this shelf", 403


@shelf.route("/shelf/create", methods=["GET", "POST"])
@login_required
def create_shelf():
    shelf = ub.Shelf()
    return create_edit_shelf(shelf, page_title=_("Create a Shelf"), page="shelfcreate")


@shelf.route("/shelf/edit/<int:shelf_id>", methods=["GET", "POST"])
@login_required
def edit_shelf(shelf_id):
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if not check_shelf_edit_permissions(shelf):
        flash(_("Sorry you are not allowed to edit this shelf"), category="error")
        return redirect(url_for("web.index"))
    return create_edit_shelf(shelf, page_title=_("Edit a shelf"), page="shelfedit", shelf_id=shelf_id)


@shelf.route("/shelf/delete/<int:shelf_id>", methods=["POST"])
@login_required
def delete_shelf(shelf_id):
    cur_shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    try:
        if not delete_shelf_helper(cur_shelf):
            flash(_("Error deleting Shelf"), category="error")
        else:
            flash(_("Shelf successfully deleted"), category="success")
    except InvalidRequestError as e:
        ub.session.rollback()
        log.error_or_exception(f"Settings Database error: {e}")
        flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")
    return redirect(url_for("web.index"))


@shelf.route("/simpleshelf/<int:shelf_id>")
@login_required_if_no_ano
def show_simpleshelf(shelf_id):
    return render_show_shelf(2, shelf_id, 1, None)


@shelf.route("/shelf/<int:shelf_id>", defaults={"sort_param": "order", "page": 1})
@shelf.route("/shelf/<int:shelf_id>/<sort_param>", defaults={"page": 1})
@shelf.route("/shelf/<int:shelf_id>/<sort_param>/<int:page>")
@login_required_if_no_ano
def show_shelf(shelf_id, sort_param, page):
    return render_show_shelf(1, shelf_id, page, sort_param)


@shelf.route("/shelf/order/<int:shelf_id>", methods=["GET", "POST"])
@login_required
def order_shelf(shelf_id):
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if shelf and check_shelf_view_permissions(shelf):
        if request.method == "POST":
            to_save = request.form.to_dict()
            books_in_shelf = (
                ub.session.query(ub.BookShelf)
                .filter(ub.BookShelf.shelf == shelf_id)
                .order_by(ub.BookShelf.order.asc())
                .all()
            )
            counter = 0
            for book in books_in_shelf:
                book.order = to_save[str(book.book_id)]
                counter += 1
                # if order different from before -> shelf.last_modified = datetime.utcnow()
            try:
                ub.session.commit()
            except (OperationalError, InvalidRequestError) as e:
                ub.session.rollback()
                log.error_or_exception(f"Settings Database error: {e}")
                flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")

        result = []
        if shelf:
            result = (
                calibre_db.session.query(db.Books)
                .join(ub.BookShelf, ub.BookShelf.book_id == db.Books.id, isouter=True)
                .add_columns(calibre_db.common_filters().label("visible"))
                .filter(ub.BookShelf.shelf == shelf_id)
                .order_by(ub.BookShelf.order.asc())
                .all()
            )
        return render_title_template(
            "shelf_order.html",
            entries=result,
            title=_("Change order of Shelf: '%(name)s'", name=shelf.name),
            shelf=shelf,
            page="shelforder",
        )
    else:
        abort(404)
        return None


def check_shelf_edit_permissions(cur_shelf) -> bool:
    if not cur_shelf.is_public and cur_shelf.user_id != int(current_user.id):
        log.error(f"User {current_user.id} not allowed to edit shelf: {cur_shelf.name}")
        return False
    if cur_shelf.is_public and not current_user.role_edit_shelfs():
        log.info(f"User {current_user.id} not allowed to edit public shelves")
        return False
    return True


def check_shelf_view_permissions(cur_shelf) -> bool:
    try:
        if cur_shelf.is_public:
            return True
        if current_user.is_anonymous or cur_shelf.user_id != current_user.id:
            log.error(f"User is unauthorized to view non-public shelf: {cur_shelf.name}")
            return False
    except Exception as e:
        log.exception(e)
    return True


# if shelf ID is set, we are editing a shelf
def create_edit_shelf(shelf, page_title, page, shelf_id=False):
    sync_only_selected_shelves = current_user.kobo_only_shelves_sync
    # calibre_db.session.query(ub.Shelf).filter(ub.Shelf.user_id == current_user.id).filter(ub.Shelf.kobo_sync).count()
    if request.method == "POST":
        to_save = request.form.to_dict()
        if not current_user.role_edit_shelfs() and to_save.get("is_public") == "on":
            flash(_("Sorry you are not allowed to create a public shelf"), category="error")
            return redirect(url_for("web.index"))
        is_public = 1 if to_save.get("is_public") == "on" else 0
        if CONFIG.config_kobo_sync:
            shelf.kobo_sync = bool(to_save.get("kobo_sync"))
            if shelf.kobo_sync:
                ub.session.query(ub.ShelfArchive).filter(ub.ShelfArchive.user_id == current_user.id).filter(
                    ub.ShelfArchive.uuid == shelf.uuid
                ).delete()
                ub.session_commit()
        shelf_title = to_save.get("title", "")
        if check_shelf_is_unique(shelf_title, is_public, shelf_id):
            shelf.name = shelf_title
            shelf.is_public = is_public
            if not shelf_id:
                shelf.user_id = int(current_user.id)
                ub.session.add(shelf)
                shelf_action = "created"
                flash_text = _("Shelf %(title)s created", title=shelf_title)
            else:
                shelf_action = "changed"
                flash_text = _("Shelf %(title)s changed", title=shelf_title)
            try:
                ub.session.commit()
                log.info(f"Shelf {shelf_title} {shelf_action}")
                flash(flash_text, category="success")
                return redirect(url_for("shelf.show_shelf", shelf_id=shelf.id))
            except (OperationalError, InvalidRequestError) as ex:
                ub.session.rollback()
                log.error_or_exception(ex)
                log.error_or_exception(f"Settings Database error: {ex}")
                flash(_("Oops! Database Error: %(error)s.", error=ex.orig), category="error")
            except Exception as ex:
                ub.session.rollback()
                log.error_or_exception(ex)
                flash(_("There was an error"), category="error")
    return render_title_template(
        "shelf_edit.html",
        shelf=shelf,
        title=page_title,
        page=page,
        kobo_sync_enabled=CONFIG.config_kobo_sync,
        sync_only_selected_shelves=sync_only_selected_shelves,
    )


def check_shelf_is_unique(title, is_public, shelf_id=False):
    ident = ub.Shelf.id != shelf_id if shelf_id else true()
    if is_public == 1:
        is_shelf_name_unique = (
            ub.session.query(ub.Shelf)
            .filter((ub.Shelf.name == title) & (ub.Shelf.is_public == 1))
            .filter(ident)
            .first()
            is None
        )

        if not is_shelf_name_unique:
            log.error(f"A public shelf with the name '{title}' already exists.")
            flash(_("A public shelf with the name '%(title)s' already exists.", title=title), category="error")
    else:
        is_shelf_name_unique = (
            ub.session.query(ub.Shelf)
            .filter((ub.Shelf.name == title) & (ub.Shelf.is_public == 0) & (ub.Shelf.user_id == int(current_user.id)))
            .filter(ident)
            .first()
            is None
        )

        if not is_shelf_name_unique:
            log.error(f"A private shelf with the name '{title}' already exists.")
            flash(_("A private shelf with the name '%(title)s' already exists.", title=title), category="error")
    return is_shelf_name_unique


def delete_shelf_helper(cur_shelf) -> bool:
    if not cur_shelf or not check_shelf_edit_permissions(cur_shelf):
        return False
    shelf_id = cur_shelf.id
    ub.session.delete(cur_shelf)
    ub.session.query(ub.BookShelf).filter(ub.BookShelf.shelf == shelf_id).delete()
    ub.session.add(ub.ShelfArchive(uuid=cur_shelf.uuid, user_id=cur_shelf.user_id))
    ub.session_commit(f"successfully deleted Shelf {cur_shelf.name}")
    return True


def change_shelf_order(shelf_id, order) -> None:
    result = (
        calibre_db.session.query(db.Books)
        .outerjoin(db.books_series_link, db.Books.id == db.books_series_link.c.book)
        .outerjoin(db.Series)
        .join(ub.BookShelf, ub.BookShelf.book_id == db.Books.id)
        .filter(ub.BookShelf.shelf == shelf_id)
        .order_by(*order)
        .all()
    )
    for index, entry in enumerate(result):
        book = (
            ub.session.query(ub.BookShelf)
            .filter(ub.BookShelf.shelf == shelf_id)
            .filter(ub.BookShelf.book_id == entry.id)
            .first()
        )
        book.order = index
    ub.session_commit(f"Shelf-id:{shelf_id} - Order changed")


def render_show_shelf(shelf_type, shelf_id, page_no, sort_param):
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()

    # check user is allowed to access shelf
    if shelf and check_shelf_view_permissions(shelf):
        if shelf_type == 1:
            # order = [ub.BookShelf.order.asc()]
            if sort_param == "pubnew":
                change_shelf_order(shelf_id, [db.Books.pubdate.desc()])
            if sort_param == "pubold":
                change_shelf_order(shelf_id, [db.Books.pubdate])
            if sort_param == "abc":
                change_shelf_order(shelf_id, [db.Books.sort])
            if sort_param == "zyx":
                change_shelf_order(shelf_id, [db.Books.sort.desc()])
            if sort_param == "new":
                change_shelf_order(shelf_id, [db.Books.timestamp.desc()])
            if sort_param == "old":
                change_shelf_order(shelf_id, [db.Books.timestamp])
            if sort_param == "authaz":
                change_shelf_order(shelf_id, [db.Books.author_sort.asc(), db.Series.name, db.Books.series_index])
            if sort_param == "authza":
                change_shelf_order(
                    shelf_id, [db.Books.author_sort.desc(), db.Series.name.desc(), db.Books.series_index.desc()]
                )
            page = "shelf.html"
            pagesize = 0
        else:
            pagesize = sys.maxsize
            page = "shelfdown.html"

        result, __, pagination = calibre_db.fill_indexpage(
            page_no,
            pagesize,
            db.Books,
            ub.BookShelf.shelf == shelf_id,
            [ub.BookShelf.order.asc()],
            True,
            CONFIG.config_read_column,
            ub.BookShelf,
            ub.BookShelf.book_id == db.Books.id,
        )
        # delete chelf entries where book is not existent anymore, can happen if book is deleted outside calibre-web
        wrong_entries = (
            calibre_db.session.query(ub.BookShelf)
            .join(db.Books, ub.BookShelf.book_id == db.Books.id, isouter=True)
            .filter(db.Books.id is None)
            .all()
        )
        for entry in wrong_entries:
            log.info(f"Not existing book {entry.book_id} in {shelf} deleted")
            try:
                ub.session.query(ub.BookShelf).filter(ub.BookShelf.book_id == entry.book_id).delete()
                ub.session.commit()
            except (OperationalError, InvalidRequestError) as e:
                ub.session.rollback()
                log.error_or_exception(f"Settings Database error: {e}")
                flash(_("Oops! Database Error: %(error)s.", error=e.orig), category="error")

        return render_title_template(
            page,
            entries=result,
            pagination=pagination,
            title=_("Shelf: '%(name)s'", name=shelf.name),
            shelf=shelf,
            page="shelf",
        )
    else:
        flash(_("Error opening shelf. Shelf does not exist or is not accessible"), category="error")
        return redirect(url_for("web.index"))
