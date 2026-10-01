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

from datetime import date, datetime

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, session,
    url_for,
)
from flask_login import current_user, login_user, logout_user

from ..extensions import db
from ..models import (
    MVB_BOX_STATUS_LABELS, MVB_BOX_STATUS_ORDER, MVB_BOX_STATUSES, MVB_DELIVERY_METHODS,
    MVB_MARKETPLACES, MVB_ROLES, MvbBox, MvbBoxEvent, MvbClient, MvbOrder, User,
)
from ..utils.http import content_disposition
from ..utils.labels_pdf import build_labels_batch_pdf
from ..utils.numbering import next_number

bp = Blueprint("mvb", __name__)

# Эндпоинты, доступные без входа (проверяется в require_login приложения).
MVB_PUBLIC_ENDPOINTS = {"mvb.login"}

MAX_BOXES_PER_ORDER = 500

# Режимы сканирования: из каких статусов короб можно перевести в какой и
# каким ролям это разрешено. Администраторы могут всё.
SCAN_MODES = {
    "pickup": {
        "title": "Забор у клиента",
        "to": "picked_up",
        "from": {"created"},
        "roles": {"mvb_driver", "mvb_staff", "mvb_admin"},
    },
    "receive": {
        "title": "Приемка на складе МВБ",
        "to": "received",
        "from": {"created", "picked_up"},
        "roles": {"mvb_staff", "mvb_admin"},
    },
    "ship": {
        "title": "Отгрузка на СЦ",
        "to": "shipped",
        "from": {"received"},
        "roles": {"mvb_staff", "mvb_admin"},
    },
    "deliver": {
        "title": "Сдано на СЦ",
        "to": "delivered",
        "from": {"shipped"},
        "roles": {"mvb_driver", "mvb_staff", "mvb_admin"},
    },
}

BOX_TIMESTAMP_FIELDS = {
    "picked_up": "picked_up_at",
    "received": "received_at",
    "shipped": "shipped_at",
    "delivered": "delivered_at",
}


# ---------- доступ ----------


def _is_client():
    return current_user.role == "mvb_client" and not current_user.is_admin


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
    return render_template("mvb/order_detail.html", order=order, counts=order.status_counts())


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
    """Экран водителя: заявки на забор, в которых еще есть незабранные короба."""
    candidates = (
        MvbOrder.query.filter(MvbOrder.status == "confirmed", MvbOrder.delivery_method == "pickup")
        .order_by(MvbOrder.planned_date.is_(None), MvbOrder.planned_date, MvbOrder.time_from)
        .all()
    )
    waiting = [o for o in candidates if any(b.status == "created" for b in o.boxes)]
    return render_template("mvb/driver.html", orders=waiting)


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
