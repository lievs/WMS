"""Дедупликация номенклатуры по совпадающему наименованию (см. чат):
страница /nomenclature/duplicates группирует товары с одинаковым
наименованием (без учета регистра/пробелов), администратор выбирает,
какой оставить, и объединяет в него остальные — остатки и строки
документов переносятся на оставленный товар, дубли удаляются."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    InventoryDocument,
    InventoryLine,
    Nomenclature,
    UnplacedStock,
    Warehouse,
)


def _make_item(barcode, name, barcode2=None, sku=None):
    item = Nomenclature(
        sku=sku or barcode, barcode=barcode, barcode2=barcode2, name=name, unit="шт",
    )
    db.session.add(item)
    db.session.commit()
    return item


def _make_warehouse(code):
    wh = Warehouse(code=code, name=f"Склад {code}")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_duplicates_page_groups_items_with_same_name(db, client_logged_in):
    _make_item("1110000000101", "Одинаковое имя")
    _make_item("1110000000102", "одинаковое имя  ")  # регистр/пробелы не важны
    _make_item("1110000000103", "Совсем другое имя")

    html = client_logged_in.get("/nomenclature/duplicates").get_data(as_text=True)

    assert "1110000000101" in html
    assert "1110000000102" in html
    assert "1110000000103" not in html


def test_duplicates_page_hides_names_without_duplicates(db, client_logged_in):
    _make_item("1110000000110", "Уникальное имя")

    html = client_logged_in.get("/nomenclature/duplicates").get_data(as_text=True)

    assert "Совпадающих наименований не найдено" in html


def test_merge_moves_boxitem_and_deletes_duplicate(db, client_logged_in):
    keep = _make_item("2220000000201", "Дубль товара")
    drop = _make_item("2220000000202", "Дубль товара")
    wh = _make_warehouse("WH-DUP-1")
    box = Box(box_number="BOX-DUP-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=drop.id, qty=3))
    db.session.commit()

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [keep.id, drop.id]},
    )

    assert Nomenclature.query.get(keep.id) is not None
    assert Nomenclature.query.get(drop.id) is None
    box_item = BoxItem.query.filter_by(box_id=box.id).first()
    assert box_item.nomenclature_id == keep.id
    assert box_item.qty == 3


def test_merge_sums_unplaced_stock_on_same_warehouse(db, client_logged_in):
    keep = _make_item("2220000000301", "Дубль с остатком")
    drop = _make_item("2220000000302", "Дубль с остатком")
    wh = _make_warehouse("WH-DUP-2")
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=keep.id, qty=5))
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=drop.id, qty=7))
    db.session.commit()

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    rows = UnplacedStock.query.filter_by(nomenclature_id=keep.id).all()
    assert len(rows) == 1
    assert rows[0].qty == 12
    assert UnplacedStock.query.filter_by(nomenclature_id=drop.id).count() == 0


def test_merge_reassigns_unplaced_stock_when_no_collision(db, client_logged_in):
    keep = _make_item("2220000000401", "Дубль без пересечения склада")
    drop = _make_item("2220000000402", "Дубль без пересечения склада")
    wh = _make_warehouse("WH-DUP-3")
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=drop.id, qty=4))
    db.session.commit()

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    row = UnplacedStock.query.filter_by(warehouse_id=wh.id).first()
    assert row.nomenclature_id == keep.id
    assert row.qty == 4


def test_merge_sums_inventory_line_on_same_document(db, client_logged_in):
    keep = _make_item("2220000000501", "Дубль в инвентаризации")
    drop = _make_item("2220000000502", "Дубль в инвентаризации")
    wh = _make_warehouse("WH-DUP-4")
    doc = InventoryDocument(number="INV-DUP-1", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(InventoryLine(document_id=doc.id, nomenclature_id=keep.id, qty=2))
    db.session.add(InventoryLine(document_id=doc.id, nomenclature_id=drop.id, qty=9))
    db.session.commit()

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    lines = InventoryLine.query.filter_by(document_id=doc.id).all()
    assert len(lines) == 1
    assert lines[0].nomenclature_id == keep.id
    assert lines[0].qty == 11


def test_merge_adopts_duplicate_barcode_as_barcode2_when_free(db, client_logged_in):
    keep = _make_item("2220000000601", "Дубль без доп. штрихкода")
    drop = _make_item("2220000000602", "Дубль без доп. штрихкода")

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    assert Nomenclature.query.get(keep.id).barcode2 == "2220000000602"


def test_merge_keeps_existing_barcode2_if_already_set(db, client_logged_in):
    keep = _make_item("2220000000701", "Дубль с занятым доп. штрихкодом", barcode2="9990000000001")
    drop = _make_item("2220000000702", "Дубль с занятым доп. штрихкодом")

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    assert Nomenclature.query.get(keep.id).barcode2 == "9990000000001"


def test_merged_duplicate_barcode_still_finds_the_kept_item(db, client_logged_in):
    keep = _make_item("2220000000801", "Дубль для поиска")
    drop = _make_item("2220000000802", "Дубль для поиска")

    client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
    )

    found = Nomenclature.find_by_barcode("2220000000802")
    assert found is not None
    assert found.id == keep.id


def test_merge_requires_admin(db, client):
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    keep = _make_item("2220000000901", "Дубль без прав")
    drop = _make_item("2220000000902", "Дубль без прав")
    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    resp = client.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [drop.id]},
        follow_redirects=True,
    )

    assert "может только администратор" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(drop.id) is not None


def test_merge_requires_keep_and_at_least_one_duplicate(db, client_logged_in):
    keep = _make_item("2220000001001", "Одинокий товар без выбора дублей")

    resp = client_logged_in.post(
        "/nomenclature/duplicates/merge",
        data={"keep_id": keep.id, "merge_ids": [keep.id]},
        follow_redirects=True,
    )

    assert "Выберите товар" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(keep.id) is not None
