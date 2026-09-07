from datetime import datetime

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Cell,
    MovementLine,
    Nomenclature,
    PlacementDocument,
    PlacementLine,
    UnplacedStock,
    Warehouse,
)
from ..utils.excel_io import export_placement_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number

bp = Blueprint("placement", __name__)


@bp.route("/")
def list_documents():
    documents = PlacementDocument.query.order_by(PlacementDocument.created_at.desc()).all()

    stock_rows = (
        db.session.query(UnplacedStock)
        .filter(UnplacedStock.qty > 0)
        .join(Warehouse)
        .order_by(Warehouse.code, UnplacedStock.nomenclature_id)
        .all()
    )
    open_boxes = (
        Box.query.filter_by(cell_id=None)
        .join(Warehouse, Box.warehouse_id == Warehouse.id)
        .order_by(Warehouse.code, Box.box_number)
        .all()
    )
    return render_template(
        "placement/list.html", documents=documents, stock_rows=stock_rows, open_boxes=open_boxes
    )


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("placement/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        flash("Выберите склад размещения", "danger")
        return redirect(url_for("placement.new_document"))

    doc = PlacementDocument(
        number=next_number("placement"),
        warehouse_id=warehouse_id,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Документ размещения {doc.number} создан", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    unpacked_lines = doc.lines.filter_by(box_id=None).all()
    boxes = doc.boxes.order_by(Box.created_at.asc()).all()
    open_boxes = Box.query.filter_by(warehouse_id=doc.warehouse_id, cell_id=None).all()
    available_stock = (
        UnplacedStock.query.filter_by(warehouse_id=doc.warehouse_id)
        .filter(UnplacedStock.qty > 0)
        .all()
    )

    # "Активный" короб — выбирается сканированием/созданием и сохраняется в
    # адресе страницы (?box=ID), чтобы дальше сканировать в него товар
    # штрихкод за штрихкодом, без выбора короба из выпадающего списка на
    # каждую позицию (короб — контекст сессии сборки, а не поле формы).
    active_box = None
    active_box_id = request.args.get("box", type=int)
    if active_box_id:
        active_box = Box.query.filter_by(id=active_box_id, warehouse_id=doc.warehouse_id).first()

    return render_template(
        "placement/detail.html",
        doc=doc,
        unpacked_lines=unpacked_lines,
        boxes=boxes,
        open_boxes=open_boxes,
        available_stock=available_stock,
        active_box=active_box,
    )


@bp.route("/<int:doc_id>/boxes/select", methods=["POST"])
def select_box(doc_id):
    """Сканирование/ввод номера короба — первый шаг размещения. Годится
    любой открытый короб этого склада (в т.ч. заготовленный заранее массовым
    созданием), не только принадлежащий этому документу."""
    doc = PlacementDocument.query.get_or_404(doc_id)
    box_number = request.form.get("box_number", "").strip()
    box = Box.query.filter_by(box_number=box_number, warehouse_id=doc.warehouse_id).first()
    if not box:
        flash(f"Короб '{box_number}' не найден на складе «{doc.warehouse.name}»", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if box.placement_document_id is None:
        box.placement_document_id = doc.id
        db.session.commit()

    return redirect(url_for("placement.detail", doc_id=doc.id, box=box.id))


def _scan_item_into_box(doc, box, item, qty):
    available = UnplacedStock.available(doc.warehouse_id, item.id)
    if qty <= 0 or qty > available:
        return None, (
            f"Недостаточно неразмещенного остатка «{item.name}»: "
            f"доступно {available} {item.unit}"
        )

    row = UnplacedStock.query.filter_by(
        warehouse_id=doc.warehouse_id, nomenclature_id=item.id
    ).first()
    row.qty -= qty

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)

    # Строка размещения сразу с проставленным коробом — для отчета/экспорта
    # и истории, отдельный шаг "упаковать в короб" в этом потоке не нужен.
    line = PlacementLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box.id)
    db.session.add(line)
    db.session.commit()
    return line, None


@bp.route("/<int:doc_id>/boxes/<int:box_id>/items/add-by-barcode", methods=["POST"])
def add_item_to_box_by_barcode(doc_id, box_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        return jsonify({"ok": False, "error": "Короб не найден"}), 404

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, error = _scan_item_into_box(doc, box, item, qty)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify(
        {"ok": True, "line": {"id": line.id, "name": item.name, "qty": line.qty}}
    )


@bp.route("/<int:doc_id>/boxes/<int:box_id>/items/add", methods=["POST"])
def add_item_to_box(doc_id, box_id):
    """Ручное добавление товара в активный короб (поиск "содержит") — то же
    самое действие, что и сканирование штрихкода, только выбор товара через
    автоподбор по названию/артикулу."""
    doc = PlacementDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if doc.status != "draft" or not box:
        flash("Документ уже завершен или короб не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 0
    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))

    _, error = _scan_item_into_box(doc, box, item, qty)
    if error:
        flash(error, "danger")
    else:
        flash(f"В короб {box.box_number} добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id, box=box_id))


def _add_line(doc, nomenclature, qty):
    available = UnplacedStock.available(doc.warehouse_id, nomenclature.id)
    if qty <= 0 or qty > available:
        return None, (
            f"Недостаточно неразмещенного остатка «{nomenclature.name}»: "
            f"доступно {available} {nomenclature.unit}"
        )

    row = UnplacedStock.query.filter_by(
        warehouse_id=doc.warehouse_id, nomenclature_id=nomenclature.id
    ).first()
    row.qty -= qty

    line = PlacementLine(document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty)
    db.session.add(line)
    db.session.commit()
    return line, None


@bp.route("/<int:doc_id>/lines/add-by-barcode", methods=["POST"])
def add_line_by_barcode(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, error = _add_line(doc, item, qty)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify(
        {"ok": True, "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty}}
    )


@bp.route("/<int:doc_id>/lines/add", methods=["POST"])
def add_line(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 0

    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    line, error = _add_line(doc, item, qty)
    if error:
        flash(error, "danger")
    else:
        flash(f"Добавлено под упаковку: {item.name} ({qty} {item.unit})", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    line = PlacementLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id is not None:
        flash("Нельзя удалить строку, уже упакованную в короб", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("placement.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/boxes/create", methods=["POST"])
def create_box(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    box = Box(
        box_number=next_number("box"),
        warehouse_id=doc.warehouse_id,
        placement_document_id=doc.id,
        status="open",
    )
    db.session.add(box)
    db.session.commit()
    flash(f"Короб {box.box_number} создан", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/pack", methods=["POST"])
def pack_line(doc_id, line_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    line = PlacementLine.query.filter_by(id=line_id, document_id=doc.id).first_or_404()
    box_id = request.form.get("box_id", type=int)
    qty = request.form.get("qty", type=float)

    # Короб может быть создан прямо в этом документе или заготовлен заранее
    # (массовое создание в «Склады → Массовое создание коробов») — годится
    # любой короб этого склада, в том числе уже размещенный в ячейке (можно
    # доукомплектовать короб товаром и после того, как его расставили).
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        flash("Короб не найден", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if line.box_id is not None:
        flash("Строка уже упакована", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if qty is None or qty <= 0 or qty > line.qty:
        qty = line.qty

    if box.placement_document_id is None:
        box.placement_document_id = doc.id

    box_item = BoxItem(box_id=box.id, nomenclature_id=line.nomenclature_id, qty=qty)
    db.session.add(box_item)

    if qty >= line.qty:
        line.box_id = box.id
    else:
        line.qty -= qty
        packed_line = PlacementLine(
            document_id=doc.id,
            nomenclature_id=line.nomenclature_id,
            qty=qty,
            box_id=box.id,
        )
        db.session.add(packed_line)

    db.session.commit()
    flash(f"Товар упакован в короб {box.box_number}", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


def _place_box(box, cell_code, expected_warehouse_id):
    if not cell_code:
        return "Укажите или отсканируйте код ячейки"

    cell = Cell.query.filter_by(warehouse_id=expected_warehouse_id, code=cell_code).first()
    if not cell:
        return f"Ячейка '{cell_code}' не найдена на этом складе"

    box.cell_id = cell.id
    box.status = "stored"
    db.session.commit()
    return None


@bp.route("/<int:doc_id>/boxes/<int:box_id>/place", methods=["POST"])
def place_box(doc_id, box_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    # Разместить можно любой открытый короб этого склада — не только упакованный
    # в рамках именно этого документа (например, короб мог приехать перемещением
    # без ячейки и теперь ждет размещения).
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first_or_404()

    error = _place_box(box, request.form.get("cell_code", "").strip(), doc.warehouse_id)
    if error:
        flash(error, "danger")
    else:
        flash(f"Короб {box.box_number} размещен в ячейке {box.cell.code}", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/box/<int:box_id>/place", methods=["POST"])
def place_box_standalone(box_id):
    """Быстрое размещение уже упакованного короба без ячейки (например,
    приехавшего перемещением) — без создания отдельного документа."""
    box = Box.query.get_or_404(box_id)
    next_url = request.form.get("next") or url_for("placement.list_documents")

    error = _place_box(box, request.form.get("cell_code", "").strip(), box.warehouse_id)
    if error:
        flash(error, "danger")
    else:
        flash(f"Короб {box.box_number} размещен в ячейке {box.cell.code}", "success")
    return redirect(next_url)


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    """Удаляет черновик размещения целиком: неупакованный остаток и остаток,
    упакованный в короба этого документа, возвращается обратно в
    неразмещенный остаток склада, сами короба (и их содержимое) удаляются.
    Если какой-то короб уже успел уехать перемещением — удалить нельзя,
    чтобы не потерять историю и не рассинхронизировать расположение."""
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик — завершенный документ уже разместил товар в ячейках", "danger")
        return redirect(url_for("placement.detail", doc_id=doc_id))

    boxes = doc.boxes.all()
    for box in boxes:
        if MovementLine.query.filter_by(box_id=box.id).first():
            flash(
                f"Нельзя удалить: короб {box.box_number} уже участвует в перемещении",
                "danger",
            )
            return redirect(url_for("placement.detail", doc_id=doc_id))

    for line in doc.lines.filter_by(box_id=None).all():
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)

    for box in boxes:
        for item in box.items:
            UnplacedStock.add(doc.warehouse_id, item.nomenclature_id, item.qty)
        db.session.delete(box)

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Документ размещения {number} удален, остаток возвращен в неразмещенный", "success")
    return redirect(url_for("placement.list_documents"))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if doc.lines.filter_by(box_id=None).count() > 0:
        flash("Не все позиции упакованы в короба", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    if doc.boxes.count() == 0:
        flash("Нет коробов для завершения размещения", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    unplaced = [b for b in doc.boxes if b.cell_id is None]
    if unplaced:
        names = ", ".join(b.box_number for b in unplaced)
        flash(f"Не все короба размещены в ячейках: {names}", "danger")
        return redirect(url_for("placement.detail", doc_id=doc.id))

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Размещение {doc.number} завершено", "success")
    return redirect(url_for("placement.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = PlacementDocument.query.get_or_404(doc_id)
    data = export_placement_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
