"""Остаток по товару в номенклатуре — и в списке, и в выгрузке Excel —
считается только по двум физическим складам (Основной, Склад №2
(Шоссейная 167)); остальные склады в системе — города маркетплейсов
(Ozon/WB), это уже отгрузка, а не "сколько у нас есть на складе" (см.
nomenclature._stock_warehouses/_stock_by_item_and_warehouse). Выгрузка
Excel дополнительно поддерживает ?warehouse_id= — считать остаток только
по одному складу."""

import io

import openpyxl

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, UnplacedStock, Warehouse


def _make_main_warehouse():
    wh = Warehouse(code="WH-NOMEXP-MAIN", name="Основной")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_second_warehouse():
    wh = Warehouse(code="WH-NOMEXP-2", name="Склад №2 (Шоссейная 167)")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_marketplace_warehouse():
    wh = Warehouse(code="WH-NOMEXP-OZON", name="ОЗОН: Тверь", marketplace="ozon", marketplace_city="Тверь")
    db.session.add(wh)
    db.session.commit()
    return wh


def _read_rows(data):
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    header = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    return header, rows


def test_export_includes_stock_column_header(db, client_logged_in):
    item = Nomenclature(sku="SKU-NOMEXP-1", barcode="9992000001", name="Товар без остатка", unit="шт")
    db.session.add(item)
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/export.xlsx")

    assert resp.status_code == 200
    header, rows = _read_rows(resp.data)
    assert header[-1] == "Остаток"
    row = next(r for r in rows if r[0] == item.barcode)
    assert row[-1] == 0


def test_export_sums_boxed_and_unplaced_stock_on_physical_warehouses(db, client_logged_in):
    wh = _make_main_warehouse()
    item = Nomenclature(sku="SKU-NOMEXP-2", barcode="9992000002", name="Товар с остатком", unit="шт")
    db.session.add(item)
    db.session.commit()

    box = Box(box_number="BOX-NOMEXP-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=5))
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=7))
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/export.xlsx")

    _header, rows = _read_rows(resp.data)
    row = next(r for r in rows if r[0] == item.barcode)
    assert row[-1] == 12  # 5 в коробе + 7 неразмещенных


def test_export_ignores_zero_or_negative_unplaced_stock_rows(db, client_logged_in):
    wh = _make_main_warehouse()
    item = Nomenclature(sku="SKU-NOMEXP-3", barcode="9992000003", name="Товар с нулевым остатком", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(UnplacedStock(warehouse_id=wh.id, nomenclature_id=item.id, qty=0))
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/export.xlsx")

    _header, rows = _read_rows(resp.data)
    row = next(r for r in rows if r[0] == item.barcode)
    assert row[-1] == 0


def test_export_excludes_marketplace_warehouse_stock(db, client_logged_in):
    """Остаток на складе-городе маркетплейса — это уже отгруженное, а не
    "есть у нас на складе" — не должен попадать в остаток номенклатуры."""
    ozon = _make_marketplace_warehouse()
    item = Nomenclature(sku="SKU-NOMEXP-4", barcode="9992000004", name="Товар на маркетплейсе", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(UnplacedStock(warehouse_id=ozon.id, nomenclature_id=item.id, qty=50))
    db.session.commit()

    resp = client_logged_in.get("/nomenclature/export.xlsx")

    _header, rows = _read_rows(resp.data)
    row = next(r for r in rows if r[0] == item.barcode)
    assert row[-1] == 0


def test_export_warehouse_filter_narrows_to_one_warehouse(db, client_logged_in):
    main = _make_main_warehouse()
    second = _make_second_warehouse()
    item = Nomenclature(sku="SKU-NOMEXP-5", barcode="9992000005", name="Товар на двух складах", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(UnplacedStock(warehouse_id=main.id, nomenclature_id=item.id, qty=3))
    db.session.add(UnplacedStock(warehouse_id=second.id, nomenclature_id=item.id, qty=9))
    db.session.commit()

    resp_all = client_logged_in.get("/nomenclature/export.xlsx")
    _header, rows_all = _read_rows(resp_all.data)
    assert next(r for r in rows_all if r[0] == item.barcode)[-1] == 12

    resp_main = client_logged_in.get(f"/nomenclature/export.xlsx?warehouse_id={main.id}")
    _header, rows_main = _read_rows(resp_main.data)
    assert next(r for r in rows_main if r[0] == item.barcode)[-1] == 3

    resp_second = client_logged_in.get(f"/nomenclature/export.xlsx?warehouse_id={second.id}")
    _header, rows_second = _read_rows(resp_second.data)
    assert next(r for r in rows_second if r[0] == item.barcode)[-1] == 9


def test_list_shows_separate_column_per_physical_warehouse(db, client_logged_in):
    main = _make_main_warehouse()
    second = _make_second_warehouse()
    item = Nomenclature(sku="SKU-NOMEXP-6", barcode="9992000006", name="Товар в списке", unit="шт")
    db.session.add(item)
    db.session.commit()
    db.session.add(UnplacedStock(warehouse_id=main.id, nomenclature_id=item.id, qty=4))
    db.session.add(UnplacedStock(warehouse_id=second.id, nomenclature_id=item.id, qty=6))
    db.session.commit()

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)

    assert "Основной" in html
    assert "Склад №2 (Шоссейная 167)" in html
