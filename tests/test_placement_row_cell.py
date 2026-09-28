"""«Размещение в ряды» (см. чат) — вкладка placement.list_documents,
placement.scan_row: для ряда без ячеек заводится ОДНА специальная ячейка,
названная тем же кодом, что и сам ряд (Cell.unlimited=True), без
ограничения по вместимости (CELL_CAPACITY на нее не действует). Дальше
размещение в нее работает через обычный механизм ячеек (scan_cell,
place_box_standalone) — отдельного пути через Box.zone_id для этой
вкладки не используется."""

from wms.extensions import db
from wms.models import CELL_CAPACITY, Box, BoxItem, Cell, Nomenclature, Warehouse, Zone


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-ROWCELL{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_zone(warehouse, code, suffix):
    zone = Zone(warehouse_id=warehouse.id, code=code)
    db.session.add(zone)
    db.session.commit()
    return zone


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-ROWCELL{suffix}", barcode=f"77707000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_box(warehouse, item, box_number, qty=1):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, status="open")
    db.session.add(box)
    db.session.commit()
    if item is not None:
        db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
        db.session.commit()
    return box


def test_scan_row_creates_unlimited_cell_named_after_zone_and_redirects(db, client_logged_in):
    warehouse = _make_warehouse("1")
    zone = _make_zone(warehouse, "А", "1")

    resp = client_logged_in.get(f"/placement/rows/{zone.id}/scan", follow_redirects=False)
    assert resp.status_code == 302

    followed = client_logged_in.get(f"/placement/rows/{zone.id}/scan", follow_redirects=True)
    html = followed.get_data(as_text=True)
    assert "Ряд А" in html
    assert "без ограничения по вместимости" in html

    cell = Cell.query.filter_by(warehouse_id=warehouse.id, code="А").first()
    assert cell is not None
    assert cell.unlimited is True
    assert cell.zone_id == zone.id


def test_scan_row_reuses_existing_row_cell(db, client_logged_in):
    """Второй заход в тот же ряд не создает вторую ячейку — код ряда
    уникален в пределах склада (uq_cell_warehouse_code)."""
    warehouse = _make_warehouse("2")
    zone = _make_zone(warehouse, "Б", "2")

    client_logged_in.get(f"/placement/rows/{zone.id}/scan")
    client_logged_in.get(f"/placement/rows/{zone.id}/scan")

    assert Cell.query.filter_by(warehouse_id=warehouse.id, code="Б").count() == 1


def test_row_cell_placement_ignores_capacity_limit(db, client_logged_in):
    """Безлимитная ячейка ряда принимает больше коробов, чем обычная
    вместимость CELL_CAPACITY — в отличие от обычной ячейки."""
    warehouse = _make_warehouse("3")
    zone = _make_zone(warehouse, "В", "3")
    item = _make_item("3")

    client_logged_in.get(f"/placement/rows/{zone.id}/scan")
    cell = Cell.query.filter_by(warehouse_id=warehouse.id, code="В").first()

    for i in range(CELL_CAPACITY + 3):
        box = _make_box(warehouse, item, f"BOX-ROWCELL-3-{i}", qty=1)
        resp = client_logged_in.post(
            "/placement/scan-cell/add-box",
            data={"warehouse_id": warehouse.id, "cell_code": "В", "box_number": box.box_number},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert Box.query.get(box.id).cell_id == cell.id

    assert cell.boxes.count() == CELL_CAPACITY + 3


def test_regular_cell_still_enforces_capacity_limit(db, client_logged_in):
    """Контроль: обычная (не безлимитная) ячейка по-прежнему ограничена
    CELL_CAPACITY — изменение капасити-проверки не затронуло обычный путь."""
    warehouse = _make_warehouse("4")
    item = _make_item("4")
    cell = Cell(warehouse_id=warehouse.id, code="C-01")
    db.session.add(cell)
    db.session.commit()

    for i in range(CELL_CAPACITY):
        box = _make_box(warehouse, item, f"BOX-ROWCELL-4-{i}", qty=1)
        box.cell_id = cell.id
        box.status = "stored"
    db.session.commit()

    overflow_box = _make_box(warehouse, item, "BOX-ROWCELL-4-OVERFLOW", qty=1)
    resp = client_logged_in.post(
        "/placement/scan-cell/add-box",
        data={"warehouse_id": warehouse.id, "cell_code": "C-01", "box_number": overflow_box.box_number},
        follow_redirects=True,
    )

    assert Box.query.get(overflow_box.id).cell_id is None
    assert "заполнена" in resp.get_data(as_text=True)


def test_scan_cell_page_shows_row_wording_for_unlimited_cell(db, client_logged_in):
    warehouse = _make_warehouse("5")
    zone = _make_zone(warehouse, "Д", "5")
    client_logged_in.get(f"/placement/rows/{zone.id}/scan")

    html = client_logged_in.get(
        f"/placement/scan-cell?warehouse_id={warehouse.id}&cell_code=Д"
    ).get_data(as_text=True)

    assert "Ряд Д" in html
    assert "без ограничения по вместимости" in html
    assert "следующий ряд" in html


def test_placement_list_shows_rows_tab_with_box_count(db, client_logged_in):
    warehouse = _make_warehouse("6")
    zone = _make_zone(warehouse, "Е", "6")
    item = _make_item("6")
    client_logged_in.get(f"/placement/rows/{zone.id}/scan")
    cell = Cell.query.filter_by(warehouse_id=warehouse.id, code="Е").first()
    box = _make_box(warehouse, item, "BOX-ROWCELL-6", qty=1)
    box.cell_id = cell.id
    box.status = "stored"
    db.session.commit()

    html = client_logged_in.get("/placement/").get_data(as_text=True)

    assert "Размещение в ряды" in html
    assert "Е" in html
    idx = html.find(">Е<")
    assert idx != -1
    snippet = html[idx:idx + 400]
    assert ">1<" in snippet


def test_unlimited_row_cell_excluded_from_automatic_cell_suggestion(db, client_logged_in):
    """Безлимитная ячейка ряда — осознанный ручной выбор (см. чат), поэтому
    автоподбор ячейки (scan_box) не должен предлагать ее наравне с
    обычными ячейками."""
    warehouse = _make_warehouse("7")
    zone = _make_zone(warehouse, "Ж", "7")
    client_logged_in.get(f"/placement/rows/{zone.id}/scan")

    item = _make_item("7")
    box = _make_box(warehouse, item, "BOX-ROWCELL-7", qty=1)

    html = client_logged_in.get(f"/placement/scan-box?box_number={box.box_number}").get_data(as_text=True)

    assert ">Ж<" not in html
