"""placement.suggest_cells_for_boxes — версия placement.suggest_cell для
подсказки ячеек сразу по многим коробам одного склада за один проход:
общий контекст (список ячеек, занятость, остатки по товару) считается
ОДИН РАЗ, а не заново на каждый короб. Раньше страницы /placement/ и
/placement/<id> вызывали suggest_cell в цикле по каждому открытому коробу
на складе — с учетом массового создания коробов (короба заготавливают
впрок) это давало примерно 5 SQL-запросов НА КАЖДЫЙ короб и было основной
причиной, почему страница размещения долго грузилась."""

from sqlalchemy import event

from wms.blueprints.placement import suggest_cell, suggest_cells_for_boxes
from wms.extensions import db
from wms.models import Box, BoxItem, Cell, Nomenclature, PlacementDocument, Warehouse, Zone


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-PERF{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-PERF{suffix}", barcode=f"77704000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_cell(warehouse, code, zone=None):
    cell = Cell(warehouse_id=warehouse.id, code=code, zone_id=zone.id if zone else None)
    db.session.add(cell)
    db.session.commit()
    return cell


def _make_box(warehouse, item, box_number, cell=None, qty=1):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, cell_id=cell.id if cell else None, status="open")
    db.session.add(box)
    db.session.commit()
    if item is not None:
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()
    return box


def _count_queries(fn):
    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _capture)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)
    return result, len(statements)


def test_suggest_cells_for_boxes_matches_individual_suggest_cell(db):
    warehouse = _make_warehouse("1")
    item_a = _make_item("1a")
    item_b = _make_item("1b")
    zone = Zone(warehouse_id=warehouse.id, code="A")
    db.session.add(zone)
    db.session.commit()

    cell1 = _make_cell(warehouse, "A-01", zone=zone)
    _make_cell(warehouse, "A-02", zone=zone)
    _make_box(warehouse, item_a, "BOX-PERF-PLACED1", cell=cell1)

    target1 = _make_box(warehouse, item_a, "BOX-PERF-T1")
    target2 = _make_box(warehouse, item_b, "BOX-PERF-T2")
    boxes = [target1, target2]

    batched = suggest_cells_for_boxes(warehouse.id, boxes)
    individual = {box.id: suggest_cell(warehouse.id, box) for box in boxes}

    for box in boxes:
        assert batched[box.id] == individual[box.id]


def test_placement_detail_query_count_does_not_scale_with_open_box_count(db, client_logged_in):
    warehouse = _make_warehouse("2")
    item = _make_item("2")
    _make_cell(warehouse, "A-01")
    _make_cell(warehouse, "A-02")

    doc = PlacementDocument(number="PLACE-PERF-1", warehouse_id=warehouse.id)
    db.session.add(doc)
    db.session.commit()

    for i in range(3):
        _make_box(warehouse, item, f"BOX-PERF-SMALL-{i}")
    _, queries_with_few_boxes = _count_queries(
        lambda: client_logged_in.get(f"/placement/{doc.id}")
    )

    for i in range(40):
        _make_box(warehouse, item, f"BOX-PERF-MANY-{i}")
    _, queries_with_many_boxes = _count_queries(
        lambda: client_logged_in.get(f"/placement/{doc.id}")
    )

    # С фиксированным контекстом на склад рост числа запросов от 3 до 43
    # открытых коробов должен быть далек от линейного (было бы +~200
    # запросов при старой реализации — по 5 на каждый новый короб).
    assert queries_with_many_boxes - queries_with_few_boxes < 20
