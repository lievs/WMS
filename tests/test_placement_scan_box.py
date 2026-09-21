"""placement.scan_box — быстрое размещение уже упакованного короба
(например, приехавшего перемещением) по принципу "куда везти короб" из
перемещений: сканируем короб, видим рекомендованную ячейку, подтверждаем
— без захода в какой-либо документ размещения (использует существующий
place_box_standalone)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Cell, Nomenclature, Warehouse


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-SCANBOX{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-SCANBOX{suffix}", barcode=f"77705000{suffix}", name="Товар", unit="шт")
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


def test_scan_unknown_box_shows_not_found(db, client_logged_in):
    html = client_logged_in.get("/placement/scan-box?box_number=NOPE").get_data(as_text=True)
    assert "не найден" in html


def test_scan_shows_suggested_cell_and_places_on_confirm(db, client_logged_in):
    warehouse = _make_warehouse("1")
    item = _make_item("1")
    cell = Cell(warehouse_id=warehouse.id, code="A-01")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(warehouse, item, "BOX-SCANBOX-1", qty=3)

    html = client_logged_in.get(f"/placement/scan-box?box_number={box.box_number}").get_data(as_text=True)
    assert box.box_number in html
    assert "A-01" in html
    assert 'value="A-01"' in html

    resp = client_logged_in.post(
        f"/placement/box/{box.id}/place",
        data={"cell_code": "A-01", "next": f"/placement/scan-box?box_number={box.box_number}"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Box.query.get(box.id).cell_id == cell.id
    assert "уже расставлен" in resp.get_data(as_text=True)


def test_scan_place_box_standalone_route_used_directly(db, client_logged_in):
    """Сама форма на странице шлет POST на place_box_standalone —
    проверяем эту связку end-to-end через реальный маршрут формы."""
    warehouse = _make_warehouse("2")
    item = _make_item("2")
    cell = Cell(warehouse_id=warehouse.id, code="A-02")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(warehouse, item, "BOX-SCANBOX-2", qty=1)

    resp = client_logged_in.post(
        f"/placement/box/{box.id}/place",
        data={"cell_code": "A-02", "next": f"/placement/scan-box?box_number={box.box_number}"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert Box.query.get(box.id).cell_id == cell.id


def test_scan_already_placed_box_shows_its_cell(db, client_logged_in):
    warehouse = _make_warehouse("3")
    item = _make_item("3")
    cell = Cell(warehouse_id=warehouse.id, code="A-03")
    db.session.add(cell)
    db.session.commit()
    box = _make_box(warehouse, item, "BOX-SCANBOX-3", qty=1)
    box.cell_id = cell.id
    db.session.commit()

    html = client_logged_in.get(f"/placement/scan-box?box_number={box.box_number}").get_data(as_text=True)

    assert "уже расставлен" in html
    assert "A-03" in html


def test_scan_empty_box_shows_nothing_to_place(db, client_logged_in):
    warehouse = _make_warehouse("4")
    box = _make_box(warehouse, None, "BOX-SCANBOX-4")

    html = client_logged_in.get(f"/placement/scan-box?box_number={box.box_number}").get_data(as_text=True)

    assert "пуст" in html
