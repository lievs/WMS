from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.excel_io import export_shipment_plan_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.shipment_plan_import import parse_plan_sheet

bp = Blueprint("shipment_plan", __name__)

MARKETPLACES = ("ozon", "wb")
MARKETPLACE_LABELS = {"ozon": "ОЗОН", "wb": "ВБ"}


def _get_or_create_city_warehouse(marketplace, city_name):
    wh = Warehouse.query.filter_by(marketplace=marketplace, marketplace_city=city_name).first()
    if wh:
        return wh
    wh = Warehouse(
        code=next_number("warehouse"),
        name=f"{MARKETPLACE_LABELS[marketplace]}: {city_name}",
        marketplace=marketplace,
        marketplace_city=city_name,
    )
    db.session.add(wh)
    db.session.flush()
    return wh


def _apply_plan(marketplace, parsed):
    """Полностью заменяет строки плана этого маркетплейса новыми из файла."""
    plan = ShipmentPlan.query.filter_by(marketplace=marketplace).first()
    if not plan:
        plan = ShipmentPlan(marketplace=marketplace)
        db.session.add(plan)

    plan.sheet_name = parsed.sheet_name
    plan.uploaded_by_id = current_user.id

    plan.lines.delete()

    city_warehouses = {
        city: _get_or_create_city_warehouse(marketplace, city) for city in parsed.cities
    }

    barcodes = {row["barcode"] for row in parsed.rows}
    nomenclature_by_barcode = {
        n.barcode: n
        for n in Nomenclature.query.filter(Nomenclature.barcode.in_(barcodes)).all()
    }

    # Один и тот же штрихкод изредка встречается в файле больше одного раза
    # для одного и того же города (дубль строки при ручном ведении таблицы) —
    # схлопываем такие дубли суммированием количества, а не падаем на
    # уникальном ограничении (plan, склад, штрихкод).
    merged = {}
    for row in parsed.rows:
        key = (row["city"], row["barcode"])
        if key in merged:
            merged[key]["qty"] += row["qty"]
            merged[key]["fact"] += row["fact"]
        else:
            merged[key] = dict(row)

    created = 0
    unmatched_barcodes = set()
    for row in merged.values():
        nomenclature = nomenclature_by_barcode.get(row["barcode"])
        if nomenclature is None:
            unmatched_barcodes.add(row["barcode"])
        db.session.add(
            ShipmentPlanLine(
                plan=plan,
                warehouse_id=city_warehouses[row["city"]].id,
                nomenclature_id=nomenclature.id if nomenclature else None,
                barcode=row["barcode"],
                article=row["article"],
                size=row["size"],
                planned_qty=row["qty"],
                # Факт "отгружено / в пути" из самого файла плана — уже
                # известное на момент выгрузки выполнение, а не только то,
                # что WMS увидит через будущие перемещения.
                fulfilled_qty=row.get("fact", 0.0),
            )
        )
        created += 1

    return created, len(unmatched_barcodes)


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if not current_user.is_admin:
        flash("Загружать план отгрузок может только администратор", "danger")
        return redirect(url_for("shipment_plan.dashboard"))

    if request.method == "GET":
        return render_template("shipment_plan/upload.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл xlsx", "danger")
        return redirect(url_for("shipment_plan.upload"))

    data = file.read()
    summary = []
    found_any = False
    for marketplace in MARKETPLACES:
        import io as _io

        parsed = parse_plan_sheet(_io.BytesIO(data), marketplace)
        if parsed is None:
            continue
        found_any = True
        created, unmatched = _apply_plan(marketplace, parsed)
        summary.append(
            f"{MARKETPLACE_LABELS[marketplace]} («{parsed.sheet_name}»): "
            f"{created} позиций, городов {len(parsed.cities)}, "
            f"неизвестных штрихкодов {unmatched}"
        )

    if not found_any:
        flash(
            "В файле не найден ни один лист «Распределение ОЗОН ФБС ...» "
            "или «Распределение ВБ ФБС ...»",
            "danger",
        )
        return redirect(url_for("shipment_plan.upload"))

    db.session.commit()
    flash("План отгрузок обновлен: " + "; ".join(summary), "success")
    return redirect(url_for("shipment_plan.dashboard"))


def _sender_warehouse_ids():
    """Склады-отправители — все обычные (не городские склады маркетплейсов)."""
    return [
        wh.id
        for wh in Warehouse.query.filter_by(marketplace=None, is_active=True).all()
    ]


def _stock_by_nomenclature(warehouse_ids):
    """{nomenclature_id: суммарный остаток} по заданным складам — неразмещенный
    остаток плюс товар, упакованный в короба на этих складах (независимо от
    того, размещен ли короб в ячейке)."""
    if not warehouse_ids:
        return {}

    stock = {}
    for nomenclature_id, qty in (
        db.session.query(UnplacedStock.nomenclature_id, func.sum(UnplacedStock.qty))
        .filter(UnplacedStock.warehouse_id.in_(warehouse_ids))
        .group_by(UnplacedStock.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    for nomenclature_id, qty in (
        db.session.query(BoxItem.nomenclature_id, func.sum(BoxItem.qty))
        .join(Box, BoxItem.box_id == Box.id)
        .filter(Box.warehouse_id.in_(warehouse_ids))
        .group_by(BoxItem.nomenclature_id)
        .all()
    ):
        stock[nomenclature_id] = stock.get(nomenclature_id, 0) + (qty or 0)

    return stock


@bp.route("/")
def dashboard():
    plans = {p.marketplace: p for p in ShipmentPlan.query.all()}
    sender_ids = _sender_warehouse_ids()
    stock = _stock_by_nomenclature(sender_ids)

    marketplaces_data = []
    for marketplace in MARKETPLACES:
        plan = plans.get(marketplace)
        if not plan:
            marketplaces_data.append(
                {"marketplace": marketplace, "label": MARKETPLACE_LABELS[marketplace], "plan": None}
            )
            continue

        lines = plan.lines.all()

        by_warehouse = {}
        for line in lines:
            row = by_warehouse.setdefault(
                line.warehouse_id,
                {"warehouse": line.warehouse, "planned": 0, "fulfilled": 0},
            )
            row["planned"] += line.planned_qty
            row["fulfilled"] += line.fulfilled_qty
        cities = sorted(by_warehouse.values(), key=lambda r: r["warehouse"].marketplace_city)
        city_names = [row["warehouse"].marketplace_city for row in cities]

        # Свод по товару сразу по всем городам — так, как сборщик привык видеть
        # план (одна строка на артикул/размер, город — колонкой), а не по одной
        # строке на каждую пару товар-город. Показываем только то, что еще не
        # довезено хотя бы в один город — выполненные позиции сборщику видеть
        # незачем, только загромождают список.
        products = {}
        for line in lines:
            key = line.barcode
            product = products.setdefault(
                key,
                {
                    "barcode": line.barcode,
                    "article": line.article,
                    "size": line.size,
                    "nomenclature": line.nomenclature,
                    "no_stock": line.nomenclature_id is None
                    or stock.get(line.nomenclature_id, 0) <= 0,
                    "per_city": {},
                    "max_remaining": 0,
                },
            )
            product["per_city"][line.warehouse.marketplace_city] = line
            product["max_remaining"] = max(product["max_remaining"], line.remaining_qty())

        picking_list = sorted(
            (p for p in products.values() if p["max_remaining"] > 0),
            key=lambda p: (p["article"] or "", p["size"] or ""),
        )
        problems_count = sum(1 for p in picking_list if p["no_stock"])

        total_planned = sum(line.planned_qty for line in lines)
        total_fulfilled = sum(line.fulfilled_qty for line in lines)

        marketplaces_data.append(
            {
                "marketplace": marketplace,
                "label": MARKETPLACE_LABELS[marketplace],
                "plan": plan,
                "cities": cities,
                "city_names": city_names,
                "picking_list": picking_list,
                "problems_count": problems_count,
                "total_planned": total_planned,
                "total_fulfilled": total_fulfilled,
            }
        )

    return render_template("shipment_plan/dashboard.html", marketplaces=marketplaces_data)


@bp.route("/export.xlsx")
def export_all():
    lines = ShipmentPlanLine.query.join(ShipmentPlan).all()
    data = export_shipment_plan_to_excel(lines)
    fname = f"shipment_plan_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
