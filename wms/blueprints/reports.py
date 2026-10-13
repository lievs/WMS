import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    OneCQuantityCheck,
    PlacementDocument,
    PlacementLine,
    ProductCategory,
    ReceivingDocument,
    ReceivingLine,
    User,
    MVB_ROLES,
    Warehouse,
    WorkEvent,
    WorkPlan,
)
from ..utils.excel_io import (
    export_movement_to_excel,
    export_placement_to_excel,
    export_receiving_status_report_to_excel,
    export_receiving_to_excel,
    export_shipped_report_to_excel,
    timestamp_for_filename,
)
from ..utils.http import content_disposition
from ..utils.work_log import work_day

bp = Blueprint("reports", __name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@bp.route("/")
def index():
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template("reports/index.html", warehouses=warehouses)


def _filtered_receiving():
    query = ReceivingDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if date_from:
        query = query.filter(ReceivingDocument.created_at >= date_from)
    if date_to:
        query = query.filter(ReceivingDocument.created_at < date_to)
    return query.order_by(ReceivingDocument.created_at.desc()).all()


def _filtered_placement():
    query = PlacementDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter_by(warehouse_id=warehouse_id)
    if date_from:
        query = query.filter(PlacementDocument.created_at >= date_from)
    if date_to:
        query = query.filter(PlacementDocument.created_at < date_to)
    return query.order_by(PlacementDocument.created_at.desc()).all()


def _filtered_movement():
    query = MovementDocument.query
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    if warehouse_id:
        query = query.filter(
            (MovementDocument.from_warehouse_id == warehouse_id)
            | (MovementDocument.to_warehouse_id == warehouse_id)
        )
    if date_from:
        query = query.filter(MovementDocument.created_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.created_at < date_to)
    return query.order_by(MovementDocument.created_at.desc()).all()


RECEIVING_STATUS_LABELS = {
    "draft": "Черновик",
    "recounting": "На пересчете",
    "sorting": "На разбраковке",
    "completed": "Завершена",
}


def _receiving_status_since(doc):
    """С какого момента документ находится в ТЕКУЩЕМ статусе — для
    draft/completed это просто created_at/completed_at, а для
    recounting/sorting берем отметку перехода (см. ReceivingDocument.
    recounting_started_at/sorting_started_at); нет отметки (приемка
    завершена до появления этих полей) — откатываемся на created_at,
    чтобы отчет не падал на старых документах."""
    if doc.status == "recounting":
        return doc.recounting_started_at or doc.created_at
    if doc.status == "sorting":
        return doc.sorting_started_at or doc.created_at
    if doc.status == "completed":
        return doc.completed_at or doc.created_at
    return doc.created_at


def _receiving_status_rows():
    """Для каждой приемки — статус, сколько дней она в нем висит, суммарное
    кол-во товара по строкам и вид(ы) товара (ProductCategory.name, см. ее
    докстринг — это и есть "вид товара" в терминах WMS). Помогает найти
    заявки, застрявшие на каком-то этапе дольше обычного."""
    unfinished_only = request.args.get("unfinished") == "on"
    documents = _filtered_receiving()
    if unfinished_only:
        documents = [d for d in documents if d.status != "completed"]

    doc_ids = [d.id for d in documents]
    qty_by_doc = defaultdict(float)
    category_ids_by_doc = defaultdict(set)
    if doc_ids:
        line_rows = (
            db.session.query(
                ReceivingLine.document_id,
                ReceivingLine.qty,
                Nomenclature.category_id,
            )
            .join(Nomenclature, ReceivingLine.nomenclature_id == Nomenclature.id)
            .filter(ReceivingLine.document_id.in_(doc_ids))
            .all()
        )
        category_ids = {r.category_id for r in line_rows if r.category_id is not None}
        category_names = (
            {c.id: c.name for c in ProductCategory.query.filter(ProductCategory.id.in_(category_ids)).all()}
            if category_ids
            else {}
        )
        for document_id, qty, category_id in line_rows:
            qty_by_doc[document_id] += qty
            if category_id is not None:
                category_ids_by_doc[document_id].add(category_names.get(category_id, ""))

    now = datetime.utcnow()
    rows = []
    for doc in documents:
        since = _receiving_status_since(doc)
        rows.append(
            {
                "document": doc,
                "status_label": RECEIVING_STATUS_LABELS.get(doc.status, doc.status),
                "days_in_status": (now - since).days if since else None,
                "qty": qty_by_doc.get(doc.id, 0),
                "categories": ", ".join(sorted(c for c in category_ids_by_doc.get(doc.id, set()) if c)),
            }
        )
    rows.sort(key=lambda r: r["days_in_status"] or 0, reverse=True)
    return rows


@bp.route("/receiving-status")
def receiving_status_report():
    rows = _receiving_status_rows()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/receiving_status.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=request.args.get("warehouse_id", type=int),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        unfinished_only=request.args.get("unfinished") == "on",
    )


@bp.route("/receiving-status.xlsx")
def receiving_status_report_export():
    rows = _receiving_status_rows()
    data = export_receiving_status_report_to_excel(rows)
    fname = f"receiving_status_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/receiving.xlsx")
def receiving_report():
    documents = _filtered_receiving()
    data = export_receiving_to_excel(documents)
    fname = f"receiving_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/placement.xlsx")
def placement_report():
    documents = _filtered_placement()
    data = export_placement_to_excel(documents)
    fname = f"placement_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/movement.xlsx")
def movement_report():
    documents = _filtered_movement()
    data = export_movement_to_excel(documents)
    fname = f"movement_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/movement-shortages")
def movement_shortages_report():
    """Документы перемещения, по которым маркетплейс принял меньше товара.

    Детализация уже хранится в MovementReceiptDiscrepancy после действия
    «Принято на складе», поэтому отчет не копирует данные в отдельный реестр.
    """
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    query = (
        MovementDocument.query
        .join(
            MovementReceiptDiscrepancy,
            MovementReceiptDiscrepancy.document_id == MovementDocument.id,
        )
        .filter(
            MovementReceiptDiscrepancy.received_qty
            < MovementReceiptDiscrepancy.expected_qty
        )
    )
    if warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == warehouse_id)
    if date_from:
        query = query.filter(MovementDocument.received_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.received_at < date_to)

    documents = query.order_by(MovementDocument.received_at.desc()).all()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/movement_shortages.html",
        documents=documents,
        warehouses=warehouses,
        selected_warehouse_id=warehouse_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@bp.route("/one-c-quantity-mismatches")
def one_c_quantity_mismatches():
    """По умолчанию показывает только неразобранные расхождения — то, что
    перенесено "в архив" (см. dismiss_one_c_quantity_check), скрыто, чтобы
    тестовые/неисправимые документы не висели в списке вечно. ?archived=1
    показывает архив отдельно, для проверки/возврата."""
    show_archived = request.args.get("archived") == "1"
    query = OneCQuantityCheck.query
    query = query.filter(
        OneCQuantityCheck.dismissed_at.isnot(None)
        if show_archived
        else OneCQuantityCheck.dismissed_at.is_(None)
    )
    rows = query.order_by(
        OneCQuantityCheck.checked_at.desc(), OneCQuantityCheck.document_number
    ).all()
    return render_template(
        "reports/one_c_quantity_mismatches.html", rows=rows, show_archived=show_archived
    )


@bp.route("/one-c-quantity-mismatches/<int:row_id>/dismiss", methods=["POST"])
def dismiss_one_c_quantity_check(row_id):
    """Переносит расхождение "в архив" — для документов, которые уже
    нельзя исправить (например, тестовые документы, реально никогда не
    будут пересверены с 1С), чтобы они не засоряли основной список."""
    if not current_user.is_admin:
        flash("Переносить расхождения в архив может только администратор", "danger")
        return redirect(url_for("reports.one_c_quantity_mismatches"))

    row = OneCQuantityCheck.query.get_or_404(row_id)
    row.dismissed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Расхождение по документу {row.document_number} перенесено в архив", "success")
    return redirect(url_for("reports.one_c_quantity_mismatches"))


@bp.route("/one-c-quantity-mismatches/<int:row_id>/restore", methods=["POST"])
def restore_one_c_quantity_check(row_id):
    if not current_user.is_admin:
        flash("Возвращать расхождения из архива может только администратор", "danger")
        return redirect(url_for("reports.one_c_quantity_mismatches", archived=1))

    row = OneCQuantityCheck.query.get_or_404(row_id)
    row.dismissed_at = None
    db.session.commit()
    flash(f"Расхождение по документу {row.document_number} возвращено из архива", "success")
    return redirect(url_for("reports.one_c_quantity_mismatches", archived=1))


def _shipped_rows():
    """Сколько и какого товара реально отгружено (перемещение со склада
    отправки завершено — товар физически уехал) по складам назначения, за
    период. Не путать с «Планом отгрузок» (это план/факт по маркетплейсам)
    — здесь просто фактически отгруженные количества, по любым складам."""
    warehouse_id = request.args.get("warehouse_id", type=int)
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))

    query = (
        db.session.query(
            MovementDocument.to_warehouse_id,
            BoxItem.nomenclature_id,
            db.func.sum(BoxItem.qty).label("qty"),
        )
        .join(MovementLine, MovementLine.document_id == MovementDocument.id)
        .join(BoxItem, BoxItem.box_id == MovementLine.box_id)
        .filter(MovementDocument.shipped_at.isnot(None))
    )
    if warehouse_id:
        query = query.filter(MovementDocument.to_warehouse_id == warehouse_id)
    if date_from:
        query = query.filter(MovementDocument.shipped_at >= date_from)
    if date_to:
        query = query.filter(MovementDocument.shipped_at < date_to)

    grouped = query.group_by(MovementDocument.to_warehouse_id, BoxItem.nomenclature_id).all()

    warehouses_by_id = {w.id: w for w in Warehouse.query.all()}
    nomenclature_ids = [r.nomenclature_id for r in grouped]
    items_by_id = (
        {n.id: n for n in Nomenclature.query.filter(Nomenclature.id.in_(nomenclature_ids)).all()}
        if nomenclature_ids
        else {}
    )

    rows = [
        {
            "warehouse": warehouses_by_id.get(r.to_warehouse_id),
            "nomenclature": items_by_id.get(r.nomenclature_id),
            "qty": r.qty,
        }
        for r in grouped
    ]
    rows.sort(
        key=lambda row: (
            row["warehouse"].name if row["warehouse"] else "",
            row["nomenclature"].name if row["nomenclature"] else "",
        )
    )
    return rows


@bp.route("/shipped")
def shipped_report():
    rows = _shipped_rows()
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    return render_template(
        "reports/shipped.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=request.args.get("warehouse_id", type=int),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )


@bp.route("/shipped.xlsx")
def shipped_report_export():
    rows = _shipped_rows()
    data = export_shipped_report_to_excel(rows)
    fname = f"shipped_report_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


BOX_ANOMALY_RATIO_DEFAULT = 3.0
# Группе нужно минимум 2 короба (сам + хотя бы один для сравнения), иначе
# медиана — это просто сам товар и сравнивать не с чем.
BOX_ANOMALY_MIN_GROUP_SIZE = 2


def _box_anomaly_group_key(nomenclature):
    """Группа для сравнения "типичного" количества в коробе. Лучший
    случай — категория (вид товара) + размер сразу по всем цветам/SKU
    этой связки (одна и та же модель разного цвета того же размера
    обычно упаковывается одинаково). Если у товара не заполнена
    категория или размер — сравнивать по этому признаку не с чем, но это
    не повод пропускать проверку вообще: откатываемся на сравнение
    короба с другими коробами ТОГО ЖЕ SKU (без кросс-цветового
    сопоставления, но хоть какая-то проверка лучше отсутствия проверки)."""
    if nomenclature.category_id is not None and nomenclature.size is not None:
        return ("cat_size", nomenclature.category_id, nomenclature.size)
    return ("sku", nomenclature.id)


def _box_anomaly_rows(warehouse_id=None, ratio_threshold=BOX_ANOMALY_RATIO_DEFAULT):
    """Ищет короба с подозрительным количеством товара — сравнивает qty
    каждой строки короба с медианой ОСТАЛЬНЫХ записей той же группы (см.
    _box_anomaly_group_key).

    Медиана считается БЕЗ самой сравниваемой записи (leave-one-out), а не
    по группе целиком — иначе при маленькой группе (например, всего 2
    короба — по одному на каждый цвет, самый частый случай) сам выброс
    сдвигает медиану к себе и перестает быть заметен: короб с 10-кратным
    перебором и обычный короб дали бы почти одинаковое отношение к общей
    медиане, и аномалия осталась бы незамеченной.

    Медиана считается по ВСЕМ коробам во всех складах (чем больше
    выборка, тем надежнее "типичное" значение), а склад из фильтра
    сужает только то, что показываем."""
    all_items = (
        db.session.query(BoxItem, Box, Nomenclature)
        .join(Box, BoxItem.box_id == Box.id)
        .join(Nomenclature, BoxItem.nomenclature_id == Nomenclature.id)
        .all()
    )

    groups = defaultdict(list)
    for box_item, _box, nomenclature in all_items:
        groups[_box_anomaly_group_key(nomenclature)].append(box_item.qty)

    rows = []
    for box_item, box, nomenclature in all_items:
        if warehouse_id and box.warehouse_id != warehouse_id:
            continue
        group = groups[_box_anomaly_group_key(nomenclature)]
        if len(group) < BOX_ANOMALY_MIN_GROUP_SIZE:
            continue
        others = list(group)
        others.remove(box_item.qty)
        median = statistics.median(others)
        if not median:
            continue
        ratio = box_item.qty / median
        if ratio >= ratio_threshold or ratio <= 1 / ratio_threshold:
            rows.append(
                {
                    "box": box,
                    "nomenclature": nomenclature,
                    "qty": box_item.qty,
                    "median": median,
                    "ratio": ratio,
                    "group_size": len(group),
                }
            )

    rows.sort(key=lambda r: max(r["ratio"], 1 / r["ratio"]), reverse=True)
    return rows


def _boxes_by_nomenclature(nomenclature_ids):
    """{nomenclature_id: [{"box_number", "warehouse", "qty"}, ...]} — ВСЕ
    короба (по всем складам, без учета фильтра отчета — как и медиана в
    _box_anomaly_rows) с этим товаром, отсортированные по количеству. Дает
    возможность сверить конкретную аномальную строку с остальными
    коробами того же товара и понять, ошибка это приемки или нормальная
    вариация (см. reports/box_anomalies.html)."""
    if not nomenclature_ids:
        return {}
    result = defaultdict(list)
    query = (
        db.session.query(BoxItem.nomenclature_id, Box.box_number, Warehouse.name, BoxItem.qty)
        .join(Box, BoxItem.box_id == Box.id)
        .outerjoin(Warehouse, Box.warehouse_id == Warehouse.id)
        .filter(BoxItem.nomenclature_id.in_(nomenclature_ids))
    )
    for nomenclature_id, box_number, warehouse_name, qty in query.all():
        result[nomenclature_id].append(
            {"box_number": box_number, "warehouse": warehouse_name or "—", "qty": qty}
        )
    for boxes in result.values():
        boxes.sort(key=lambda b: b["qty"], reverse=True)
    return result


@bp.route("/box-anomalies")
def box_anomalies_report():
    warehouse_id = request.args.get("warehouse_id", type=int)
    threshold = request.args.get("threshold", type=float) or BOX_ANOMALY_RATIO_DEFAULT
    if threshold <= 1:
        threshold = BOX_ANOMALY_RATIO_DEFAULT
    rows = _box_anomaly_rows(warehouse_id=warehouse_id, ratio_threshold=threshold)
    warehouses = Warehouse.query.order_by(Warehouse.code).all()
    boxes_by_nomenclature = _boxes_by_nomenclature({row["nomenclature"].id for row in rows})
    return render_template(
        "reports/box_anomalies.html",
        rows=rows,
        warehouses=warehouses,
        selected_warehouse_id=warehouse_id,
        threshold=threshold,
        boxes_by_nomenclature=boxes_by_nomenclature,
    )


# --- Выработка сотрудников -------------------------------------------------

WORK_KINDS = (
    ("pack", "Упаковка", "Сколько товара положил в короба и в скольких коробах"),
    ("pick", "Сборка на отгрузку", "Сколько коробов собрал в перемещения"),
)


def _parse_day(value):
    parsed = _parse_date(value)
    return parsed.date() if parsed else None


def _percent(fact, plan):
    if not plan:
        return None
    return round(fact / plan * 100)


def _dashboard_payload(date_from, date_to, facts, users, defaults, overrides):
    """Данные для графиков страницы (рисуются в браузере, переключение
    «упаковка / сборка» без перезагрузки): даты периода, факт по
    сотрудникам и дням [шт, коробов], планы [шт, коробов]."""
    days = []
    day = date_from
    while day <= date_to:
        days.append(day)
        day += timedelta(days=1)
    payload = {
        "dates": [d.isoformat() for d in days],
        "users": {str(uid): u.display_name() for uid, u in users.items()},
        "data": {"pack": {}, "pick": {}},
        "plans": {"pack": {}, "pick": {}},
    }
    for (kind, uid, day), (qty, boxes) in facts.items():
        if uid in users:
            payload["data"][kind].setdefault(str(uid), {})[day.isoformat()] = [round(qty, 3), boxes]
    for (kind, uid), plan in defaults.items():
        if uid in users:
            payload["plans"][kind].setdefault(str(uid), {})["def"] = [plan.qty_plan, plan.boxes_plan]
    for (kind, uid, day), plan in overrides.items():
        if uid in users:
            payload["plans"][kind].setdefault(str(uid), {}).setdefault("days", {})[day.isoformat()] = [
                plan.qty_plan, plan.boxes_plan,
            ]
    return payload


def _history_events_in_range(date_from, date_to):
    return WorkEvent.query.filter(
        WorkEvent.ref_kind == "history",
        WorkEvent.work_date >= date_from,
        WorkEvent.work_date <= date_to,
    ).count()


def _employee_output_context(date_from, date_to, user_id):
    events = (
        db.session.query(
            WorkEvent.kind,
            WorkEvent.user_id,
            WorkEvent.work_date,
            db.func.sum(WorkEvent.qty),
            db.func.count(db.func.distinct(WorkEvent.box_id)),
        )
        .filter(WorkEvent.work_date >= date_from, WorkEvent.work_date <= date_to)
        .group_by(WorkEvent.kind, WorkEvent.user_id, WorkEvent.work_date)
    )
    if user_id:
        events = events.filter(WorkEvent.user_id == user_id)
    facts = {}
    for kind, uid, day, qty, boxes in events.all():
        facts[(kind, uid, day)] = (qty or 0, boxes or 0)

    defaults, overrides = {}, {}
    for plan in WorkPlan.query.all():
        if plan.plan_date is None:
            defaults[(plan.kind, plan.user_id)] = plan
        elif date_from <= plan.plan_date <= date_to:
            overrides[(plan.kind, plan.user_id, plan.plan_date)] = plan

    # Только сотрудники склада: клиенты, водители и операторы МВБ Логистики —
    # другой контур (отдельный вход), в этот отчет они не попадают.
    users = {u.id: u for u in User.query.all() if (u.role or "") not in MVB_ROLES or u.is_admin}
    sections = []
    for kind, title, hint in WORK_KINDS:
        rows = []
        totals = {}
        for (k, uid, day), (qty, boxes) in sorted(
            ((key, val) for key, val in facts.items() if key[1] in users), key=lambda item: (item[0][2], users[item[0][1]].display_name() if item[0][1] in users else ""),
            reverse=True,
        ):
            if k != kind:
                continue
            default = defaults.get((kind, uid))
            override = overrides.get((kind, uid, day))
            plan = override or default
            plan_qty = plan.qty_plan if plan else None
            plan_boxes = plan.boxes_plan if plan else None
            rows.append({
                "user": users.get(uid), "user_id": uid, "day": day, "qty": qty, "boxes": boxes,
                "plan_qty": plan_qty, "plan_boxes": plan_boxes,
                "override_qty": override.qty_plan if override else None,
                "override_boxes": override.boxes_plan if override else None,
                "pct_qty": _percent(qty, plan_qty), "pct_boxes": _percent(boxes, plan_boxes),
            })
            total = totals.setdefault(uid, {"user": users.get(uid), "qty": 0, "boxes": 0, "days": 0})
            total["qty"] += qty
            total["boxes"] += boxes
            total["days"] += 1
        sections.append({
            "kind": kind, "title": title, "hint": hint, "rows": rows,
            "totals": sorted(totals.values(), key=lambda t: -t["boxes"]),
        })
    return {
        "payload": _dashboard_payload(date_from, date_to, facts, users, defaults, overrides),
        "history_events": _history_events_in_range(date_from, date_to),
        "sections": sections,
        "defaults": defaults,
        "users": sorted((u for u in users.values() if u.is_active_user), key=lambda u: u.display_name().lower()),
    }


@bp.route("/employee-output")
def employee_output():
    today = work_day()
    date_from = _parse_day(request.args.get("date_from")) or (today - timedelta(days=13))
    date_to = _parse_day(request.args.get("date_to")) or today
    user_id = request.args.get("user_id", type=int)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days > 120:
        date_from = date_to - timedelta(days=120)
        flash("Период ограничен 120 днями", "warning")
    context = _employee_output_context(date_from, date_to, user_id)
    return render_template(
        "reports/employee_output.html",
        date_from=date_from, date_to=date_to, user_id=user_id, kinds=WORK_KINDS, timedelta=timedelta, **context,
    )


def _number_or_none(raw, as_int=False):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(round(value)) if as_int else value


def _save_plan(user_id, kind, plan_date, qty_plan, boxes_plan):
    plan = WorkPlan.query.filter_by(user_id=user_id, kind=kind, plan_date=plan_date).first()
    if qty_plan is None and boxes_plan is None:
        if plan:
            db.session.delete(plan)
        return
    if plan is None:
        plan = WorkPlan(user_id=user_id, kind=kind, plan_date=plan_date)
        db.session.add(plan)
    plan.qty_plan = qty_plan
    plan.boxes_plan = boxes_plan


@bp.route("/employee-output/plans", methods=["POST"])
def employee_output_save_plans():
    """Сохранение планов с формы дашборда. Имена полей:
    def__<kind>__<user_id>__qty|boxes — план по умолчанию (на каждый день),
    day__<kind>__<user_id>__<YYYY-MM-DD>__qty|boxes — на конкретную дату.
    Пустое значение убирает план."""
    back = url_for(
        "reports.employee_output",
        date_from=request.form.get("date_from") or None,
        date_to=request.form.get("date_to") or None,
        user_id=request.form.get("user_id") or None,
    )
    if not current_user.is_admin:
        flash("План выработки может менять только администратор", "danger")
        return redirect(back)

    pending = {}
    one_user = request.form.get("one_user", "")
    if one_user.isdigit():
        # форма «План на конкретный день»: сотрудник + вид + дата + значения
        one_date = _parse_day(request.form.get("one_date"))
        one_kind = request.form.get("one_kind")
        if one_date is not None and one_kind in ("pack", "pick"):
            pending[(int(one_user), one_kind, one_date)] = {
                "qty": _number_or_none(request.form.get("one_qty")),
                "boxes": _number_or_none(request.form.get("one_boxes"), as_int=True),
            }
    for name, raw in request.form.items():
        parts = name.split("__")
        if parts[0] == "def" and len(parts) == 4:
            _, kind, uid, field = parts
            plan_date = None
        elif parts[0] == "day" and len(parts) == 5:
            _, kind, uid, day, field = parts
            plan_date = _parse_day(day)
            if plan_date is None:
                continue
        else:
            continue
        if kind not in ("pack", "pick") or field not in ("qty", "boxes") or not uid.isdigit():
            continue
        entry = pending.setdefault((int(uid), kind, plan_date), {})
        entry[field] = _number_or_none(raw, as_int=field == "boxes")

    valid_users = {u.id for u in User.query.filter(User.id.in_({key[0] for key in pending})).all()} if pending else set()
    for (uid, kind, plan_date), fields in pending.items():
        if uid in valid_users:
            _save_plan(uid, kind, plan_date, fields.get("qty"), fields.get("boxes"))
    db.session.commit()
    flash("План выработки сохранен", "success")
    return redirect(back)


@bp.route("/employee-output/backfill", methods=["POST"])
def employee_output_backfill():
    """Приблизительное восстановление истории выработки (до появления
    журнала WorkEvent кто именно упаковывал/собирал нигде не хранилось):
    автор документа приёмки/размещения/перемещения считается тем, кто
    сделал все его строки, дата — дата документа (у строк перемещения —
    время скана). Заполняется только период ДО первой настоящей записи
    журнала; повторный запуск заменяет прежнее восстановление."""
    if not current_user.is_admin:
        flash("Восстанавливать историю может только администратор", "danger")
        return redirect(url_for("reports.employee_output"))

    real = db.session.query(db.func.min(WorkEvent.created_at)).filter(
        db.or_(WorkEvent.ref_kind.is_(None), WorkEvent.ref_kind != "history")
    ).scalar()
    cutoff = real or datetime.utcnow()
    WorkEvent.query.filter_by(ref_kind="history").delete(synchronize_session=False)

    rows = []

    def add(kind, user_id, moment, box_id, qty, nomenclature_id=None):
        rows.append({
            "user_id": user_id, "work_date": work_day(moment), "kind": kind, "box_id": box_id,
            "nomenclature_id": nomenclature_id, "qty": qty, "ref_kind": "history", "created_at": moment,
        })

    packed = 0
    for line, doc in (
        db.session.query(ReceivingLine, ReceivingDocument)
        .join(ReceivingDocument, ReceivingLine.document_id == ReceivingDocument.id)
        .filter(ReceivingLine.box_id.isnot(None), ReceivingDocument.created_by_id.isnot(None),
                ReceivingDocument.created_at < cutoff, ReceivingLine.qty > 0)
    ):
        add("pack", doc.created_by_id, doc.created_at, line.box_id, line.qty, line.nomenclature_id)
        packed += 1
    for line, doc in (
        db.session.query(PlacementLine, PlacementDocument)
        .join(PlacementDocument, PlacementLine.document_id == PlacementDocument.id)
        .filter(PlacementLine.box_id.isnot(None), PlacementDocument.created_by_id.isnot(None),
                PlacementDocument.created_at < cutoff, PlacementLine.qty > 0)
    ):
        add("pack", doc.created_by_id, doc.created_at, line.box_id, line.qty, line.nomenclature_id)
        packed += 1

    picked = 0
    movement_lines = (
        db.session.query(MovementLine, MovementDocument)
        .join(MovementDocument, MovementLine.document_id == MovementDocument.id)
        .filter(MovementDocument.created_by_id.isnot(None), MovementLine.scanned_at < cutoff)
        .all()
    )
    box_ids = {line.box_id for line, _ in movement_lines}
    totals = {}
    if box_ids:
        for box_id, qty in (
            db.session.query(BoxItem.box_id, db.func.sum(BoxItem.qty))
            .filter(BoxItem.box_id.in_(box_ids)).group_by(BoxItem.box_id)
        ):
            totals[box_id] = qty or 0
    for line, doc in movement_lines:
        add("pick", doc.created_by_id, line.scanned_at, line.box_id, totals.get(line.box_id, 0))
        rows[-1]["ref_id"] = doc.id
        picked += 1
    # ref_kind="history" у сборки тоже — отличать восстановленное от настоящего
    if rows:
        for row in rows:
            row.setdefault("ref_id", None)
        db.session.bulk_insert_mappings(WorkEvent, rows)
    db.session.commit()
    flash(
        f"История восстановлена приблизительно, по авторам документов: упаковка — {packed} записей, "
        f"сборка — {picked} записей. Настоящий учёт идёт с {work_day(cutoff).strftime('%d.%m.%Y')}.",
        "success",
    )
    return redirect(url_for("reports.employee_output"))
