"""«МВБ Логистика» — отдельный раздел на базе WMS со своим входом (/mvb/login).

Клиент оформляет заявку на передачу коробов (забор транспортной компанией
или самопривоз) для отправки на СЦ Wildberries / Ozon. При оформлении каждый
короб получает собственный штрихкод, клиент печатает этикетки 58×40 и
клеит их на короба. Дальше короба сканируются поштучно: водитель при
заборе, склад МВБ при приемке и при отгрузке на СЦ, — и клиент видит у себя
статус каждого короба.

Пользователи с ролями MVB_ROLES видят только этот раздел; пользователи WMS
(кроме администраторов) — наоборот, сюда не попадают.
"""

import secrets
from datetime import date, datetime

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, session,
    url_for,
)
from flask_login import current_user, login_user, logout_user

from ..extensions import db
from ..models import (
    MVB_BOX_STATUS_LABELS, MVB_BOX_STATUS_ORDER, MVB_BOX_STATUSES, MVB_DELIVERY_METHODS,
    MVB_MARKETPLACES, MVB_ROLES, MVB_TRIP_STATUSES, MvbBox, MvbBoxEvent, MvbClient, MvbOrder,
    MvbPallet, MvbTrip, MvbVehicle, User,
)
from ..utils.http import content_disposition
from ..utils.labels_pdf import build_labels_batch_pdf
from ..utils.numbering import next_number
from ..utils.timezone import MOSCOW_OFFSET

bp = Blueprint("mvb", __name__)

# Эндпоинты, доступные без входа (проверяется в require_login приложения).
MVB_PUBLIC_ENDPOINTS = {"mvb.login", "mvb.pass_page"}

MAX_BOXES_PER_ORDER = 500

# Режимы поштучного сканирования до склада: из каких статусов короб можно
# перевести в какой и каким ролям это разрешено. Администраторы могут всё.
# Дальше склада короба движутся в составе рейса (погрузка сканом, отправка
# и сдача на СЦ — см. раздел «Рейсы»).
SCAN_MODES = {
    "pickup": {
        "title": "Забор у клиента",
        "nav": "Скан забора",
        "to": "picked_up",
        "from": {"created"},
        "roles": {"mvb_driver", "mvb_staff", "mvb_admin"},
    },
    "receive": {
        "title": "Приемка на складе МВБ",
        "nav": "Приемка",
        "to": "received",
        "from": {"created", "picked_up"},
        "roles": {"mvb_staff", "mvb_admin"},
    },
}

BOX_TIMESTAMP_FIELDS = {
    "picked_up": "picked_up_at",
    "received": "received_at",
    "loaded": "loaded_at",
    "shipped": "shipped_at",
    "delivered": "delivered_at",
}


# ---------- доступ ----------


def _is_client():
    return current_user.role == "mvb_client" and not current_user.is_admin


def _is_staff():
    return current_user.is_admin or current_user.role in ("mvb_staff", "mvb_admin")


def _is_driver():
    return current_user.role == "mvb_driver" and not current_user.is_admin


def _can_scan(mode):
    return current_user.is_admin or current_user.role in SCAN_MODES[mode]["roles"]


def _visible_orders_query():
    query = MvbOrder.query
    if _is_client():
        query = query.filter(MvbOrder.client_id == current_user.mvb_client_id)
    return query


def _get_order_or_404(order_id):
    order = _visible_orders_query().filter(MvbOrder.id == order_id).first()
    if order is None:
        abort(404)
    return order


@bp.before_request
def _restrict_to_mvb_users():
    if request.endpoint in MVB_PUBLIC_ENDPOINTS or not current_user.is_authenticated:
        return None
    if not (current_user.is_admin or current_user.role in MVB_ROLES):
        flash("Раздел «МВБ Логистика» вам не доступен", "danger")
        return redirect(url_for("main.index"))
    if _is_client() and not current_user.mvb_client_id:
        logout_user()
        flash("Учетная запись не привязана к клиенту — обратитесь к администратору МВБ", "danger")
        return redirect(url_for("mvb.login"))
    return None


@bp.context_processor
def _inject():
    return {
        "MVB_BOX_STATUSES": MVB_BOX_STATUSES,
        "MVB_BOX_STATUS_LABELS": MVB_BOX_STATUS_LABELS,
        "MVB_MARKETPLACES": MVB_MARKETPLACES,
        "MVB_DELIVERY_METHODS": MVB_DELIVERY_METHODS,
        "MVB_ROLES": MVB_ROLES,
        "SCAN_MODES": SCAN_MODES,
        "mvb_is_client": current_user.is_authenticated and _is_client(),
        "mvb_can_scan": (lambda mode: current_user.is_authenticated and _can_scan(mode)),
        "mvb_is_staff": current_user.is_authenticated and _is_staff(),
        "MVB_TRIP_STATUSES": MVB_TRIP_STATUSES,
    }


# ---------- вход ----------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and (current_user.is_admin or current_user.role in MVB_ROLES):
        return redirect(url_for("mvb.index"))

    if request.method == "GET":
        return render_template("mvb/login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = User.query.filter_by(username=username).first()
    if (
        not user
        or not user.is_active_user
        or not user.check_password(password)
        or not (user.is_admin or user.role in MVB_ROLES)
    ):
        flash("Неверный логин или пароль", "danger")
        return render_template("mvb/login.html", username=username)

    login_user(user, remember=True)
    session["session_version"] = user.session_version or 0
    return redirect(url_for("mvb.index"))


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    flash("Вы вышли из системы", "success")
    return redirect(url_for("mvb.login"))


@bp.route("/")
def index():
    if current_user.role == "mvb_driver" and not current_user.is_admin:
        return redirect(url_for("mvb.driver"))
    return redirect(url_for("mvb.orders"))


# ---------- заявки ----------


@bp.route("/orders")
def orders():
    query = _visible_orders_query()
    status = request.args.get("status", "")
    if status in ("draft", "confirmed", "cancelled"):
        query = query.filter(MvbOrder.status == status)
    client_id = request.args.get("client_id", type=int)
    if client_id and not _is_client():
        query = query.filter(MvbOrder.client_id == client_id)
    items = query.order_by(MvbOrder.created_at.desc()).limit(300).all()
    clients = [] if _is_client() else MvbClient.query.order_by(MvbClient.name).all()
    return render_template(
        "mvb/orders.html", orders=items, clients=clients, status=status, client_id=client_id
    )


def _parse_time(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return None
    return value


def _fill_order_from_form(order):
    """Заполняет заявку из формы; возвращает текст ошибки или None."""
    marketplace = request.form.get("marketplace", "wb")
    delivery_method = request.form.get("delivery_method", "pickup")
    if marketplace not in MVB_MARKETPLACES:
        return "Выберите маркетплейс"
    if delivery_method not in MVB_DELIVERY_METHODS:
        return "Выберите способ передачи коробов"
    try:
        box_count = int(request.form.get("box_count", ""))
    except ValueError:
        return "Укажите количество коробов"
    if box_count < 1 or box_count > MAX_BOXES_PER_ORDER:
        return f"Количество коробов — от 1 до {MAX_BOXES_PER_ORDER}"
    planned_date = None
    raw_date = request.form.get("planned_date", "").strip()
    if raw_date:
        try:
            planned_date = date.fromisoformat(raw_date)
        except ValueError:
            return "Некорректная дата"
    pickup_address = request.form.get("pickup_address", "").strip()
    if delivery_method == "pickup" and not pickup_address:
        return "Для забора укажите адрес"

    order.marketplace = marketplace
    order.delivery_method = delivery_method
    order.box_count = box_count
    order.destination = request.form.get("destination", "").strip() or None
    order.pickup_address = pickup_address or None
    order.planned_date = planned_date
    order.time_from = _parse_time(request.form.get("time_from"))
    order.time_to = _parse_time(request.form.get("time_to"))
    order.comment = request.form.get("comment", "").strip() or None
    return None


@bp.route("/orders/new", methods=["GET", "POST"])
def order_new():
    clients = [] if _is_client() else MvbClient.query.filter_by(is_active=True).order_by(MvbClient.name).all()
    if request.method == "GET":
        client = current_user.mvb_client if _is_client() else None
        return render_template(
            "mvb/order_form.html", order=None, clients=clients,
            default_address=(client.address if client else ""),
        )

    if _is_client():
        client_id = current_user.mvb_client_id
    else:
        client_id = request.form.get("client_id", type=int)
        if not client_id or not db.session.get(MvbClient, client_id):
            flash("Выберите клиента", "danger")
            return render_template("mvb/order_form.html", order=None, clients=clients, form=request.form)

    order = MvbOrder(client_id=client_id, created_by_id=current_user.id, status="draft")
    error = _fill_order_from_form(order)
    if error:
        flash(error, "danger")
        return render_template("mvb/order_form.html", order=None, clients=clients, form=request.form)
    order.number = next_number("mvb_order", "MVB-", 6)
    db.session.add(order)
    db.session.commit()
    flash(f"Заявка {order.number} создана. Проверьте и нажмите «Оформить» — коробам будут присвоены штрихкоды.", "success")
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/orders/<int:order_id>")
def order_detail(order_id):
    order = _get_order_or_404(order_id)
    drivers = _active_drivers() if _is_staff() else []
    return render_template(
        "mvb/order_detail.html", order=order, counts=order.status_counts(), drivers=drivers,
    )


@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
def order_edit(order_id):
    order = _get_order_or_404(order_id)
    if order.status != "draft":
        flash("Изменить можно только черновик", "warning")
        return redirect(url_for("mvb.order_detail", order_id=order.id))
    if request.method == "GET":
        return render_template("mvb/order_form.html", order=order, clients=[])
    error = _fill_order_from_form(order)
    if error:
        db.session.rollback()
        flash(error, "danger")
        return render_template("mvb/order_form.html", order=order, clients=[], form=request.form)
    db.session.commit()
    flash("Заявка сохранена", "success")
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/orders/<int:order_id>/confirm", methods=["POST"])
def order_confirm(order_id):
    """Оформление заявки: способ передачи фиксируется, каждому коробу
    присваивается собственный штрихкод «номер заявки-порядковый номер»."""
    order = _get_order_or_404(order_id)
    if order.status != "draft":
        flash("Заявка уже оформлена", "warning")
        return redirect(url_for("mvb.order_detail", order_id=order.id))
    for seq in range(1, order.box_count + 1):
        order.boxes.append(MvbBox(seq=seq, barcode=f"{order.number}-{seq:03d}", status="created"))
    order.status = "confirmed"
    order.confirmed_at = datetime.utcnow()
    if order.delivery_method == "self" and not order.pass_token:
        order.pass_token = secrets.token_urlsafe(16)
    db.session.commit()
    flash(f"Заявка оформлена: присвоено штрихкодов — {order.box_count}. Распечатайте этикетки и наклейте на каждый короб.", "success")
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
def order_cancel(order_id):
    order = _get_order_or_404(order_id)
    if order.status == "cancelled":
        return redirect(url_for("mvb.order_detail", order_id=order.id))
    if any(box.status != "created" for box in order.boxes):
        flash("Нельзя отменить: часть коробов уже отсканирована", "danger")
        return redirect(url_for("mvb.order_detail", order_id=order.id))
    order.status = "cancelled"
    db.session.commit()
    flash("Заявка отменена", "success")
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/orders/<int:order_id>/labels.pdf")
def order_labels_pdf(order_id):
    """Этикетки 58×40 на все короба заявки (или на выбранные ?seq=1,2)."""
    order = _get_order_or_404(order_id)
    if order.status != "confirmed" or not order.boxes:
        abort(404)
    boxes = order.boxes
    seq_param = request.args.get("seq", "")
    if seq_param:
        try:
            wanted = {int(v) for v in seq_param.split(",") if v.strip()}
        except ValueError:
            abort(400)
        boxes = [b for b in boxes if b.seq in wanted]
    total = len(order.boxes)
    entries = [
        (
            box.barcode,
            f"{box.barcode}  ({box.seq}/{total})",
            f"{order.client.name[:28]} → {order.marketplace_label}",
        )
        for box in boxes
    ]
    pdf = build_labels_batch_pdf(entries, title_font_size=9, max_img_h_ratio=0.6)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{order.number}.pdf", "inline")},
    )


# ---------- сканирование ----------


@bp.route("/driver")
def driver():
    """Экран водителя: заявки на забор с незабранными коробами (назначенные
    на него и еще никому не назначенные) и его рейсы на СЦ."""
    candidates = (
        MvbOrder.query.filter(MvbOrder.status == "confirmed", MvbOrder.delivery_method == "pickup")
        .order_by(MvbOrder.planned_date.is_(None), MvbOrder.planned_date, MvbOrder.time_from)
        .all()
    )
    waiting = [o for o in candidates if any(b.status == "created" for b in o.boxes)]
    trips = []
    if _is_driver():
        waiting = [o for o in waiting if o.driver_id in (None, current_user.id)]
        trips = (
            MvbTrip.query.filter(
                MvbTrip.driver_id == current_user.id,
                MvbTrip.status.in_(["assigned", "arrived", "loading", "departed"]),
            )
            .order_by(MvbTrip.planned_arrival_at)
            .all()
        )
    return render_template("mvb/driver.html", orders=waiting, trips=trips)


@bp.route("/scan/<mode>")
def scan(mode):
    if mode not in SCAN_MODES:
        abort(404)
    if not _can_scan(mode):
        flash("Этот режим сканирования вам не доступен", "danger")
        return redirect(url_for("mvb.index"))
    return render_template("mvb/scan.html", mode=mode, mode_info=SCAN_MODES[mode])


@bp.route("/scan/<mode>", methods=["POST"])
def scan_box(mode):
    """Скан одного короба: JSON {ok, message, box...}. Повторный скан уже
    переведенного короба не ошибка — просто сообщаем, что он уже учтен."""
    if mode not in SCAN_MODES:
        abort(404)
    if not _can_scan(mode):
        return jsonify(ok=False, message="Нет доступа к этому режиму"), 403
    info = SCAN_MODES[mode]
    code = (request.form.get("barcode") or (request.get_json(silent=True) or {}).get("barcode") or "").strip()
    if not code:
        return jsonify(ok=False, message="Пустой штрихкод"), 400

    box = MvbBox.query.filter(db.func.upper(MvbBox.barcode) == code.upper()).first()
    if box is None:
        return jsonify(ok=False, message=f"Короб {code} не найден"), 404
    order = box.order
    payload = {
        "barcode": box.barcode,
        "order": order.number,
        "order_url": url_for("mvb.order_detail", order_id=order.id),
        "client": order.client.name,
        "seq": box.seq,
        "total": len(order.boxes),
    }
    if order.status != "confirmed":
        return jsonify(ok=False, message=f"Заявка {order.number} не оформлена или отменена", **payload), 409
    if mode == "pickup" and order.delivery_method != "pickup":
        return jsonify(ok=False, message="Это самопривоз — короб принимается на складе", **payload), 409

    if box.status == info["to"]:
        return jsonify(ok=True, already=True, message=f"Уже отмечен: {box.status_label}",
                       status=box.status_label, **payload)
    if box.status not in info["from"]:
        return jsonify(ok=False, message=f"Сейчас короб в статусе «{box.status_label}»", **payload), 409

    now = datetime.utcnow()
    box.status = info["to"]
    setattr(box, BOX_TIMESTAMP_FIELDS[info["to"]], now)
    db.session.add(MvbBoxEvent(box=box, status=info["to"], user_id=current_user.id, created_at=now))
    db.session.commit()
    done = sum(
        1 for b in order.boxes
        if MVB_BOX_STATUS_ORDER.get(b.status, 0) >= MVB_BOX_STATUS_ORDER[info["to"]]
    )
    return jsonify(
        ok=True, already=False, message=f"{box.barcode}: {box.status_label}",
        status=box.status_label, done=done, **payload,
    )


# ---------- администрирование ----------


def _require_manage():
    if not current_user.can_manage_mvb():
        flash("Доступно только администратору МВБ", "danger")
        return False
    return True


@bp.route("/admin/clients", methods=["GET", "POST"])
def admin_clients():
    if not _require_manage():
        return redirect(url_for("mvb.index"))
    if request.method == "POST":
        client_id = request.form.get("client_id", type=int)
        client = db.session.get(MvbClient, client_id) if client_id else MvbClient()
        if client is None:
            abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Укажите название клиента", "danger")
            return redirect(url_for("mvb.admin_clients"))
        client.name = name
        client.inn = request.form.get("inn", "").strip() or None
        client.contact_name = request.form.get("contact_name", "").strip() or None
        client.phone = request.form.get("phone", "").strip() or None
        client.address = request.form.get("address", "").strip() or None
        if client_id:
            client.is_active = request.form.get("is_active") == "1"
        db.session.add(client)
        db.session.commit()
        flash(f"Клиент «{client.name}» сохранен", "success")
        return redirect(url_for("mvb.admin_clients"))
    clients = MvbClient.query.order_by(MvbClient.name).all()
    return render_template("mvb/admin_clients.html", clients=clients)


@bp.route("/admin/users", methods=["GET", "POST"])
def admin_users():
    if not _require_manage():
        return redirect(url_for("mvb.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        client_id = request.form.get("client_id", type=int)
        if not username or len(password) < 6:
            flash("Укажите логин и пароль не короче 6 символов", "danger")
        elif role not in MVB_ROLES:
            flash("Выберите роль", "danger")
        elif role == "mvb_client" and not (client_id and db.session.get(MvbClient, client_id)):
            flash("Для роли «Клиент» выберите клиента", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Такой логин уже занят", "danger")
        else:
            user = User(
                username=username,
                full_name=request.form.get("full_name", "").strip() or None,
                role=role,
                is_admin=False,
                mvb_client_id=client_id if role == "mvb_client" else None,
                allowed_sections="none",
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Пользователь «{username}» создан", "success")
        return redirect(url_for("mvb.admin_users"))
    users = User.query.filter(User.role.in_(list(MVB_ROLES))).order_by(User.username).all()
    clients = MvbClient.query.filter_by(is_active=True).order_by(MvbClient.name).all()
    return render_template("mvb/admin_users.html", users=users, clients=clients)


def _get_mvb_user_or_404(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.role not in MVB_ROLES:
        abort(404)
    return user


@bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
def admin_user_toggle(user_id):
    if not _require_manage():
        return redirect(url_for("mvb.index"))
    user = _get_mvb_user_or_404(user_id)
    if user.id == current_user.id:
        flash("Нельзя отключить самого себя", "danger")
        return redirect(url_for("mvb.admin_users"))
    user.is_active_user = not user.is_active_user
    if not user.is_active_user:
        user.session_version = (user.session_version or 0) + 1
    db.session.commit()
    flash(f"Пользователь «{user.username}» {'включен' if user.is_active_user else 'отключен'}", "success")
    return redirect(url_for("mvb.admin_users"))


@bp.route("/admin/users/<int:user_id>/password", methods=["POST"])
def admin_user_password(user_id):
    if not _require_manage():
        return redirect(url_for("mvb.index"))
    user = _get_mvb_user_or_404(user_id)
    password = request.form.get("password", "")
    if len(password) < 6:
        flash("Пароль не короче 6 символов", "danger")
        return redirect(url_for("mvb.admin_users"))
    user.set_password(password)
    user.session_version = (user.session_version or 0) + 1
    db.session.commit()
    flash(f"Пароль для «{user.username}» изменен", "success")
    return redirect(url_for("mvb.admin_users"))


# ---------- общие помощники этапа 2 ----------


def _require_staff():
    if not _is_staff():
        flash("Доступно только складу МВБ", "danger")
        return False
    return True


def _active_drivers():
    return (
        User.query.filter(User.role == "mvb_driver", User.is_active_user.is_(True))
        .order_by(User.full_name, User.username)
        .all()
    )


def _move_box(box, status, now):
    box.status = status
    setattr(box, BOX_TIMESTAMP_FIELDS[status], now)
    db.session.add(MvbBoxEvent(box=box, status=status, user_id=current_user.id, created_at=now))


def _parse_dt(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _local_to_utc(value):
    """Время из формы вводится по Москве (как отображается везде в WMS), а
    хранится в UTC — как и остальные отметки времени."""
    dt = _parse_dt(value)
    return dt - MOSCOW_OFFSET if dt else None


def _same_direction(box_order, marketplace, destination):
    return box_order.marketplace == marketplace and (
        (box_order.destination or "").strip().lower() == (destination or "").strip().lower()
    )


# ---------- назначение водителя и пропуск ----------


@bp.route("/orders/<int:order_id>/driver", methods=["POST"])
def order_assign_driver(order_id):
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    order = _get_order_or_404(order_id)
    driver_id = request.form.get("driver_id", type=int)
    if driver_id:
        driver = db.session.get(User, driver_id)
        if driver is None or driver.role != "mvb_driver":
            abort(400)
        order.driver_id = driver.id
        flash(f"На забор назначен водитель {driver.display_name()}", "success")
    else:
        order.driver_id = None
        flash("Водитель снят с заявки", "success")
    db.session.commit()
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/orders/<int:order_id>/pass", methods=["POST"])
def order_pass(order_id):
    """Данные водителя для пропуска на склад (самопривоз)."""
    order = _get_order_or_404(order_id)
    if order.delivery_method != "self" or order.status != "confirmed":
        abort(400)
    order.pass_driver_name = request.form.get("pass_driver_name", "").strip() or None
    order.pass_car_plate = request.form.get("pass_car_plate", "").strip().upper() or None
    order.pass_phone = request.form.get("pass_phone", "").strip() or None
    if not order.pass_token:
        order.pass_token = secrets.token_urlsafe(16)
    db.session.commit()
    flash("Пропуск сохранен — отправьте ссылку водителю", "success")
    return redirect(url_for("mvb.order_detail", order_id=order.id))


@bp.route("/pass/<token>")
def pass_page(token):
    """Электронный пропуск: открывается по ссылке без входа (водителю,
    охране, приемщику)."""
    order = MvbOrder.query.filter_by(pass_token=token).first()
    if order is None or order.status != "confirmed":
        abort(404)
    from ..utils.barcodes import generate_barcode_data_uri

    return render_template(
        "mvb/pass.html", order=order, barcode_img=generate_barcode_data_uri(order.number),
    )


# ---------- транспорт ----------


@bp.route("/vehicles", methods=["GET", "POST"])
def vehicles():
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    if request.method == "POST":
        vehicle_id = request.form.get("vehicle_id", type=int)
        vehicle = db.session.get(MvbVehicle, vehicle_id) if vehicle_id else MvbVehicle()
        if vehicle is None:
            abort(404)
        plate = request.form.get("plate", "").strip().upper()
        try:
            capacity = int(request.form.get("capacity_boxes") or 0)
        except ValueError:
            capacity = -1
        if not plate or capacity < 0:
            flash("Укажите госномер и вместимость (число коробов)", "danger")
            return redirect(url_for("mvb.vehicles"))
        driver_id = request.form.get("driver_id", type=int)
        vehicle.plate = plate
        vehicle.model = request.form.get("model", "").strip() or None
        vehicle.carrier = request.form.get("carrier", "").strip() or None
        vehicle.capacity_boxes = capacity
        vehicle.driver_id = driver_id or None
        if vehicle_id:
            vehicle.is_active = request.form.get("is_active") == "1"
        db.session.add(vehicle)
        db.session.commit()
        flash(f"Транспорт {vehicle.plate} сохранен", "success")
        return redirect(url_for("mvb.vehicles"))
    return render_template(
        "mvb/vehicles.html",
        vehicles=MvbVehicle.query.order_by(MvbVehicle.is_active.desc(), MvbVehicle.plate).all(),
        drivers=_active_drivers(),
    )


# ---------- паллеты ----------


@bp.route("/pallets", methods=["GET", "POST"])
def pallets():
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    if request.method == "POST":
        marketplace = request.form.get("marketplace", "")
        if marketplace not in MVB_MARKETPLACES:
            flash("Выберите маркетплейс", "danger")
            return redirect(url_for("mvb.pallets"))
        pallet = MvbPallet(
            number=next_number("mvb_pallet", "PLT-", 6),
            marketplace=marketplace,
            destination=request.form.get("destination", "").strip() or None,
            created_by_id=current_user.id,
        )
        db.session.add(pallet)
        db.session.commit()
        return redirect(url_for("mvb.pallet_detail", pallet_id=pallet.id))
    items = MvbPallet.query.order_by(MvbPallet.created_at.desc()).limit(200).all()
    return render_template("mvb/pallets.html", pallets=items)


@bp.route("/pallets/<int:pallet_id>")
def pallet_detail(pallet_id):
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    pallet = db.session.get(MvbPallet, pallet_id) or abort(404)
    return render_template("mvb/pallet_detail.html", pallet=pallet)


@bp.route("/pallets/<int:pallet_id>/scan", methods=["POST"])
def pallet_scan(pallet_id):
    """Скан короба на паллету: только принятые на складе короба того же
    направления, еще не погруженные; с другой паллеты короб переносится."""
    if not _is_staff():
        return jsonify(ok=False, message="Нет доступа"), 403
    pallet = db.session.get(MvbPallet, pallet_id) or abort(404)
    code = (request.form.get("barcode") or "").strip()
    box = MvbBox.query.filter(db.func.upper(MvbBox.barcode) == code.upper()).first()
    if box is None:
        return jsonify(ok=False, message=f"Короб {code} не найден"), 404
    if box.status != "received":
        return jsonify(ok=False, message=f"Короб в статусе «{box.status_label}» — на паллету только принятые на складе"), 409
    if not _same_direction(box.order, pallet.marketplace, pallet.destination):
        return jsonify(ok=False, message=f"Другое направление: {box.order.marketplace_label} · {box.order.destination or '—'}"), 409
    if box.pallet_id == pallet.id:
        return jsonify(ok=True, already=True, message=f"{box.barcode} уже на этой паллете", count=len(pallet.boxes))
    moved_from = box.pallet.number if box.pallet else None
    box.pallet = pallet
    db.session.commit()
    message = f"{box.barcode} → {pallet.number}" + (f" (снят с {moved_from})" if moved_from else "")
    return jsonify(ok=True, already=False, message=message, count=len(pallet.boxes))


@bp.route("/pallets/<int:pallet_id>/remove/<int:box_id>", methods=["POST"])
def pallet_remove_box(pallet_id, box_id):
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    box = db.session.get(MvbBox, box_id)
    if box is None or box.pallet_id != pallet_id or box.status != "received":
        abort(400)
    box.pallet_id = None
    db.session.commit()
    return redirect(url_for("mvb.pallet_detail", pallet_id=pallet_id))


@bp.route("/pallets/<int:pallet_id>/label.pdf")
def pallet_label_pdf(pallet_id):
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    pallet = db.session.get(MvbPallet, pallet_id) or abort(404)
    subtitle = f"{pallet.marketplace_label} · {pallet.destination or ''} · {len(pallet.boxes)} кор."
    pdf = build_labels_batch_pdf([(pallet.number, pallet.number, subtitle)], title_font_size=12, max_img_h_ratio=0.6)
    return Response(
        pdf, mimetype="application/pdf",
        headers={"Content-Disposition": content_disposition(f"{pallet.number}.pdf", "inline")},
    )


# ---------- отправка на СЦ: готово к отправке и рейсы ----------


def _ready_boxes_query():
    return (
        MvbBox.query.join(MvbOrder)
        .filter(MvbBox.status == "received", MvbBox.trip_id.is_(None))
        .order_by(MvbBox.received_at)
    )


@bp.route("/dispatch")
def dispatch():
    """Готово к отправке: принятые и еще не погруженные короба по
    направлениям (маркетплейс + СЦ), самые давние сверху (FIFO)."""
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    groups = {}
    for box in _ready_boxes_query().all():
        key = (box.order.marketplace, (box.order.destination or "").strip())
        group = groups.setdefault(key, {
            "marketplace": key[0], "destination": key[1], "boxes": 0,
            "oldest": box.received_at, "pallets": set(), "clients": set(),
        })
        group["boxes"] += 1
        group["clients"].add(box.order.client.name)
        if box.pallet_id:
            group["pallets"].add(box.pallet.number)
    open_trips = {}
    for trip in MvbTrip.query.filter(MvbTrip.status.in_(["searching", "assigned", "arrived", "loading"])).all():
        open_trips.setdefault((trip.marketplace, (trip.destination or "").strip()), []).append(trip)
    rows = sorted(groups.values(), key=lambda g: g["oldest"])
    for row in rows:
        row["trips"] = open_trips.get((row["marketplace"], row["destination"]), [])
    return render_template("mvb/dispatch.html", rows=rows)


@bp.route("/trips")
def trips():
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    status = request.args.get("status", "active")
    query = MvbTrip.query
    if status == "active":
        query = query.filter(MvbTrip.status.notin_(["delivered", "cancelled"]))
    elif status in MVB_TRIP_STATUSES:
        query = query.filter(MvbTrip.status == status)
    items = query.order_by(MvbTrip.created_at.desc()).limit(300).all()
    return render_template("mvb/trips.html", trips=items, status=status)


@bp.route("/trips/new", methods=["POST"])
def trip_new():
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    marketplace = request.form.get("marketplace", "")
    if marketplace not in MVB_MARKETPLACES:
        abort(400)
    planned = request.form.get("planned_boxes", type=int) or 0
    trip = MvbTrip(
        number=next_number("mvb_trip", "RS-", 6),
        marketplace=marketplace,
        destination=request.form.get("destination", "").strip() or None,
        planned_boxes=max(planned, 0),
        status="searching",
        created_by_id=current_user.id,
    )
    db.session.add(trip)
    db.session.commit()
    flash(f"Рейс {trip.number} создан — статус «Поиск авто»", "success")
    return redirect(url_for("mvb.trip_detail", trip_id=trip.id))


def _get_trip_or_404(trip_id):
    trip = db.session.get(MvbTrip, trip_id)
    if trip is None:
        abort(404)
    if not _is_staff() and not (_is_driver() and trip.driver_id == current_user.id):
        abort(404)
    return trip


@bp.route("/trips/<int:trip_id>")
def trip_detail(trip_id):
    trip = _get_trip_or_404(trip_id)
    ready = [
        b for b in _ready_boxes_query().all()
        if _same_direction(b.order, trip.marketplace, trip.destination)
    ] if _is_staff() else []
    return render_template(
        "mvb/trip_detail.html", trip=trip, ready_count=len(ready),
        vehicles=MvbVehicle.query.filter_by(is_active=True).order_by(MvbVehicle.plate).all() if _is_staff() else [],
        drivers=_active_drivers() if _is_staff() else [],
    )


@bp.route("/trips/<int:trip_id>/plan", methods=["POST"])
def trip_plan(trip_id):
    """Авто найдено: транспорт, водитель и плановое время подачи/погрузки."""
    if not _require_staff():
        return redirect(url_for("mvb.index"))
    trip = _get_trip_or_404(trip_id)
    if trip.status in ("departed", "delivered", "cancelled"):
        abort(400)
    vehicle_id = request.form.get("vehicle_id", type=int)
    vehicle = db.session.get(MvbVehicle, vehicle_id) if vehicle_id else None
    driver_id = request.form.get("driver_id", type=int)
    trip.vehicle = vehicle
    trip.driver_id = driver_id or (vehicle.driver_id if vehicle else None)
    trip.planned_arrival_at = _local_to_utc(request.form.get("planned_arrival_at"))
    trip.planned_load_start_at = _local_to_utc(request.form.get("planned_load_start_at"))
    trip.planned_load_end_at = _local_to_utc(request.form.get("planned_load_end_at"))
    trip.comment = request.form.get("comment", "").strip() or None
    if vehicle and trip.status == "searching":
        trip.status = "assigned"
    if not vehicle and trip.status == "assigned":
        trip.status = "searching"
    db.session.commit()
    flash("План рейса сохранен", "success")
    return redirect(url_for("mvb.trip_detail", trip_id=trip.id))


@bp.route("/trips/<int:trip_id>/<action>", methods=["POST"])
def trip_action(trip_id, action):
    """Фактические отметки рейса. Отправка переводит погруженные короба в
    «В пути на СЦ», сдача — в «Сдан на СЦ»; водитель рейса может отметить
    подачу авто и сдачу на СЦ."""
    trip = _get_trip_or_404(trip_id)
    now = datetime.utcnow()
    driver_actions = {"arrive", "deliver"}
    if not _is_staff() and action not in driver_actions:
        abort(403)

    if action == "arrive" and trip.status in ("assigned",):
        trip.status = "arrived"
        trip.arrived_at = now
    elif action == "start_loading" and trip.status in ("assigned", "arrived"):
        trip.status = "loading"
        trip.arrived_at = trip.arrived_at or now
        trip.load_started_at = now
    elif action == "depart" and trip.status in ("assigned", "arrived", "loading"):
        if not trip.boxes:
            flash("В рейсе нет погруженных коробов", "danger")
            return redirect(url_for("mvb.trip_detail", trip_id=trip.id))
        trip.status = "departed"
        trip.load_finished_at = now
        trip.departed_at = now
        for box in trip.boxes:
            if box.status == "loaded":
                _move_box(box, "shipped", now)
    elif action == "deliver" and trip.status == "departed":
        trip.status = "delivered"
        trip.delivered_at = now
        for box in trip.boxes:
            if box.status == "shipped":
                _move_box(box, "delivered", now)
    elif action == "cancel" and trip.status in ("searching", "assigned", "arrived", "loading"):
        trip.status = "cancelled"
        for box in list(trip.boxes):
            # погруженные короба возвращаются на склад
            box.status = "received"
            box.loaded_at = None
            box.trip_id = None
    else:
        flash("Это действие сейчас недоступно", "warning")
        return redirect(url_for("mvb.trip_detail", trip_id=trip.id))
    db.session.commit()
    flash(f"Рейс {trip.number}: {trip.status_label}", "success")
    if _is_driver():
        return redirect(url_for("mvb.driver"))
    return redirect(url_for("mvb.trip_detail", trip_id=trip.id))


@bp.route("/trips/<int:trip_id>/scan", methods=["POST"])
def trip_scan(trip_id):
    """Погрузка сканом: короб или паллета целиком (все ее короба). Первый
    скан сам отмечает начало погрузки. Сверх вместимости авто — предупреждение."""
    if not _is_staff():
        return jsonify(ok=False, message="Нет доступа"), 403
    trip = _get_trip_or_404(trip_id)
    if trip.status not in ("assigned", "arrived", "loading"):
        return jsonify(ok=False, message=f"Рейс в статусе «{trip.status_label}» — погрузка закрыта"), 409
    code = (request.form.get("barcode") or "").strip()
    now = datetime.utcnow()

    pallet = MvbPallet.query.filter(db.func.upper(MvbPallet.number) == code.upper()).first()
    if pallet is not None:
        candidates = [b for b in pallet.boxes if b.status == "received" and b.trip_id is None]
        if not candidates:
            return jsonify(ok=False, message=f"На паллете {pallet.number} нет коробов к погрузке"), 409
        if not _same_direction(candidates[0].order, trip.marketplace, trip.destination):
            return jsonify(ok=False, message=f"Паллета другого направления: {pallet.marketplace_label} · {pallet.destination or '—'}"), 409
        boxes = candidates
    else:
        box = MvbBox.query.filter(db.func.upper(MvbBox.barcode) == code.upper()).first()
        if box is None:
            return jsonify(ok=False, message=f"Короб или паллета {code} не найдены"), 404
        if box.trip_id == trip.id:
            return jsonify(ok=True, already=True, message=f"{box.barcode} уже в этом рейсе", count=len(trip.boxes))
        if box.status != "received" or box.trip_id is not None:
            return jsonify(ok=False, message=f"Короб в статусе «{box.status_label}»"), 409
        if not _same_direction(box.order, trip.marketplace, trip.destination):
            return jsonify(ok=False, message=f"Другое направление: {box.order.marketplace_label} · {box.order.destination or '—'}"), 409
        boxes = [box]

    if trip.status != "loading":
        trip.status = "loading"
        trip.arrived_at = trip.arrived_at or now
        trip.load_started_at = now
    for box in boxes:
        box.trip = trip
        _move_box(box, "loaded", now)
    db.session.commit()
    count = len(trip.boxes)
    message = (
        f"Паллета {pallet.number}: погружено {len(boxes)} кор." if pallet is not None
        else f"{boxes[0].barcode} погружен"
    )
    warning = None
    capacity = trip.vehicle.capacity_boxes if trip.vehicle else 0
    if capacity and count > capacity:
        warning = f"Внимание: {count} кор. больше вместимости авто ({capacity})"
    return jsonify(ok=True, already=False, message=message, count=count, warning=warning)
