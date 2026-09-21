"""В списке номенклатуры колонка "Норма, мин" убрана, вместо нее —
остаток отдельной колонкой на каждый физический склад (Основной, Склад
№2 (Шоссейная 167)) — короба + неразмещенный остаток по товару на этом
складе, см. nomenclature.list_nomenclature / nomenclature._stock_warehouses
/ wms/templates/nomenclature/list.html. Остальные склады (города
маркетплейсов) в этот остаток не входят — это уже отгрузка."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, UnplacedStock, Warehouse


def _make_main_warehouse():
    wh = Warehouse(code="WH-STK-MAIN", name="Основной")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix, name="Товар"):
    item = Nomenclature(sku=f"SKU-STK{suffix}", barcode=f"77704000{suffix}", name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_norm_column_removed_and_warehouse_columns_shown(db, client_logged_in):
    _make_main_warehouse()
    _make_item("1")

    resp = client_logged_in.get("/nomenclature/")

    html = resp.get_data(as_text=True)
    assert "Норма, мин" not in html
    assert "Основной" in html


def test_stock_column_sums_boxes_and_unplaced_on_matching_warehouse(db, client_logged_in):
    wh = _make_main_warehouse()
    item = _make_item("2", name="Уникальный товар для остатка")

    box = Box(box_number="BOX-STK201", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=12))
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/")
    html = resp.get_data(as_text=True)
    idx = html.find("Уникальный товар для остатка")

    assert idx != -1
    assert "17 шт" in html[idx : idx + 3000]


def test_stock_shows_zero_for_item_with_no_stock(db, client_logged_in):
    _make_main_warehouse()
    _make_item("3", name="Товар без остатка")

    resp = client_logged_in.get("/nomenclature/")
    html = resp.get_data(as_text=True)
    idx = html.find("Товар без остатка")

    assert idx != -1
    assert "0 шт" in html[idx : idx + 3000]


def test_stock_on_non_physical_warehouse_is_not_shown(db, client_logged_in):
    """Склад-город маркетплейса не входит в _stock_warehouses() — остаток
    там не должен попадать ни в одну из колонок остатка."""
    ozon = Warehouse(code="WH-STK-OZON", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add(ozon)
    db.session.commit()
    item = _make_item("4", name="Товар на маркетплейсе")
    db.session.add(UnplacedStock(warehouse_id=ozon.id, nomenclature_id=item.id, qty=99))
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/")
    html = resp.get_data(as_text=True)
    idx = html.find("Товар на маркетплейсе")

    assert idx != -1
    assert "99 шт" not in html[idx : idx + 3000]
