"""Администратор может поправить количество в уже завершенной приемке
задним числом (см. receiving._apply_line_qty_change /
_adjust_completed_credited_qty) — через то же "Сохранить количества"
(update_lines_bulk), что и на черновике/пересчете, и через update_line.

Строка без короба уже зачислила годное количество в неразмещенный остаток
(UnplacedStock/UnplacedStockLot, см. _credit_receiving_line) — правка
количества проводит ту же дельту по остатку и партии. Строка, упакованная
в короб при приемке, просто синхронизирует BoxItem.qty, как и на
черновике. Не админ — доступа нет вообще."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    UnplacedStock,
    UnplacedStockLot,
    User,
    Warehouse,
)


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-ECQ{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-ECQ{suffix}", barcode=f"77720000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_completed_doc(client_logged_in, wh, number, item, qty, box_id=None):
    """Черновик без короба/с коробом -> сразу завершенная приемка (обычная
    приемка без этапов пересчета/разбраковки — см. receiving.complete)."""
    doc = ReceivingDocument(number=number, warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box_id)
    db.session.add(line)
    db.session.commit()
    client_logged_in.post(f"/receiving/{doc.id}/complete")
    db.session.refresh(doc)
    db.session.refresh(line)
    return doc, line


def test_admin_increases_qty_credits_more_to_unplaced_stock(db, client_logged_in):
    wh = _make_warehouse("1")
    item = _make_item("1")
    doc, line = _make_completed_doc(client_logged_in, wh, "ECQ-0001", item, 10)
    assert doc.status == "completed"
    assert UnplacedStock.available(wh.id, item.id) == 10

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "15"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert ReceivingLine.query.get(line.id).qty == 15
    assert UnplacedStock.available(wh.id, item.id) == 15
    lot = UnplacedStockLot.query.filter_by(receiving_line_id=line.id).first()
    assert lot.qty_received == 15
    assert lot.qty_remaining == 15


def test_admin_decreases_qty_debits_unplaced_stock(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc, line = _make_completed_doc(client_logged_in, wh, "ECQ-0002", item, 10)

    client_logged_in.post(f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "4"})

    assert ReceivingLine.query.get(line.id).qty == 4
    assert UnplacedStock.available(wh.id, item.id) == 4


def test_cannot_reduce_below_already_placed_amount(db, client_logged_in):
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc, line = _make_completed_doc(client_logged_in, wh, "ECQ-0003", item, 10)

    # Часть остатка уже размещена (как это делает "Размещение").
    UnplacedStock.consume(wh.id, item.id, 7)
    db.session.commit()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "5"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "уже размещено" in resp.get_data(as_text=True)
    assert ReceivingLine.query.get(line.id).qty == 10
    assert UnplacedStock.available(wh.id, item.id) == 3


def test_box_packed_line_syncs_box_item_qty(db, client_logged_in):
    wh = _make_warehouse("4")
    item = _make_item("4")
    box = Box(box_number="ECQ-BOX-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=6))
    db.session.commit()
    doc, line = _make_completed_doc(client_logged_in, wh, "ECQ-0004", item, 6, box_id=box.id)
    # Товар, упакованный в короб при приемке, в неразмещенный остаток не уходит.
    assert UnplacedStock.available(wh.id, item.id) == 0

    client_logged_in.post(f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "9"})

    assert ReceivingLine.query.get(line.id).qty == 9
    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first().qty == 9
    assert UnplacedStock.available(wh.id, item.id) == 0


def test_single_line_update_route_also_works_for_completed_admin(db, client_logged_in):
    wh = _make_warehouse("5")
    item = _make_item("5")
    doc, line = _make_completed_doc(client_logged_in, wh, "ECQ-0005", item, 10)

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/update", data={"qty": "20"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert ReceivingLine.query.get(line.id).qty == 20
    assert UnplacedStock.available(wh.id, item.id) == 20


def test_non_admin_cannot_edit_completed_receiving_qty(db, client):
    wh = _make_warehouse("6")
    item = _make_item("6")
    user = User(username="staffer-ecq", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()

    # created_by=user — иначе get_owned_or_404 (см. _restrict_document_access)
    # отдаст 404 раньше, чем дело дойдет до проверки статуса документа.
    doc = ReceivingDocument(number="ECQ-0006", warehouse_id=wh.id, status="completed", created_by_id=user.id)
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10, line_completed_at=doc.created_at)
    db.session.add(line)
    db.session.commit()
    UnplacedStock.add(wh.id, item.id, 10, receiving_document=doc, receiving_line=line)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    resp = client.post(
        f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "20"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "уже завершен" in resp.get_data(as_text=True)
    assert ReceivingLine.query.get(line.id).qty == 10
    assert UnplacedStock.available(wh.id, item.id) == 10


def test_never_credited_line_edits_qty_without_touching_stock(db, client_logged_in):
    """Строка, которая при завершении так и не была зачислена (например,
    неподтвержденная позиция накладной — см. receiving.complete) — правка
    количества меняет только саму строку, без эффекта на остаток, ровно
    как и было до правки."""
    wh = _make_warehouse("7")
    item = _make_item("7")
    doc = ReceivingDocument(number="ECQ-0007", warehouse_id=wh.id, status="completed")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/lines/update-bulk", data={f"qty_{line.id}": "999"})

    assert ReceivingLine.query.get(line.id).qty == 999
    assert UnplacedStock.available(wh.id, item.id) == 0
