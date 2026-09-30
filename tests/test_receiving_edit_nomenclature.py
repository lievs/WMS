"""Исправление товара в строке приемки администратором (см. чат: ошибочно
отсканированный похожий штрихкод) — receiving.update_line_nomenclature.
В отличие от простого редактирования количества, переносит уже случившееся
влияние на остаток склада на новый товар: BoxItem короба, неразмещенный
остаток, возврат поставщику по браку."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    UnplacedStock,
    Warehouse,
)


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-RLN{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix, name="Товар"):
    item = Nomenclature(sku=f"SKU-RLN{suffix}", barcode=f"77720000{suffix}", name=name, unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_admin_can_rename_item_on_draft_line_without_stock_effect(db, client_logged_in):
    wh = _make_warehouse("1")
    old_item = _make_item("1a", "Ошибочно принятый товар")
    new_item = _make_item("1b", "Правильный товар")
    doc = ReceivingDocument(number="RLN-0001", warehouse_id=wh.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=old_item.id, qty=5)
    db.session.add(line)
    db.session.commit()

    resp = client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
        follow_redirects=True,
    )

    assert "изменен" in resp.get_data(as_text=True)
    assert ReceivingLine.query.get(line.id).nomenclature_id == new_item.id
    assert UnplacedStock.available(wh.id, old_item.id) == 0
    assert UnplacedStock.available(wh.id, new_item.id) == 0


def test_admin_edit_moves_unplaced_stock_from_completed_line(db, client_logged_in):
    wh = _make_warehouse("2")
    old_item = _make_item("2a")
    new_item = _make_item("2b")
    doc = ReceivingDocument(number="RLN-0002", warehouse_id=wh.id, status="completed")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(
        document_id=doc.id, nomenclature_id=old_item.id, qty=10, line_completed_at=doc.created_at,
    )
    db.session.add(line)
    db.session.commit()
    UnplacedStock.add(wh.id, old_item.id, 10, receiving_document=doc)

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
    )

    assert UnplacedStock.available(wh.id, old_item.id) == 0
    assert UnplacedStock.available(wh.id, new_item.id) == 10
    assert ReceivingLine.query.get(line.id).nomenclature_id == new_item.id


def test_admin_edit_moves_only_good_qty_when_line_has_defect(db, client_logged_in):
    wh = _make_warehouse("3")
    old_item = _make_item("3a")
    new_item = _make_item("3b")
    doc = ReceivingDocument(number="RLN-0003", warehouse_id=wh.id, status="completed")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(
        document_id=doc.id, nomenclature_id=old_item.id, qty=10, defect_qty=3,
        line_completed_at=doc.created_at,
    )
    db.session.add(line)
    db.session.add(
        SupplierReturn(
            warehouse_id=wh.id, nomenclature_id=old_item.id, qty=3,
            receiving_document_id=doc.id, comment="Брак",
        )
    )
    db.session.commit()
    UnplacedStock.add(wh.id, old_item.id, 7, receiving_document=doc)

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
    )

    assert UnplacedStock.available(wh.id, old_item.id) == 0
    assert UnplacedStock.available(wh.id, new_item.id) == 7
    supplier_return = SupplierReturn.query.filter_by(receiving_document_id=doc.id).one()
    assert supplier_return.nomenclature_id == new_item.id


def test_admin_edit_moves_boxitem_qty(db, client_logged_in):
    wh = _make_warehouse("4")
    old_item = _make_item("4a")
    new_item = _make_item("4b")
    box = Box(box_number="BOX-RLN-4", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    doc = ReceivingDocument(number="RLN-0004", warehouse_id=wh.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=old_item.id, qty=4, box_id=box.id)
    db.session.add(line)
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=old_item.id, qty=4))
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
    )

    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=old_item.id).first() is None
    new_box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=new_item.id).first()
    assert new_box_item is not None
    assert new_box_item.qty == 4
    assert ReceivingLine.query.get(line.id).nomenclature_id == new_item.id


def test_admin_edit_merges_into_existing_boxitem_of_new_item(db, client_logged_in):
    wh = _make_warehouse("5")
    old_item = _make_item("5a")
    new_item = _make_item("5b")
    box = Box(box_number="BOX-RLN-5", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    doc = ReceivingDocument(number="RLN-0005", warehouse_id=wh.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=old_item.id, qty=4, box_id=box.id)
    db.session.add(line)
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=old_item.id, qty=4))
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=new_item.id, qty=2))
    db.session.commit()

    client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
    )

    assert BoxItem.query.filter_by(box_id=box.id, nomenclature_id=old_item.id).first() is None
    new_box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=new_item.id).one()
    assert new_box_item.qty == 6


def test_edit_nomenclature_requires_admin(db, client):
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    wh = _make_warehouse("6")
    old_item = _make_item("6a")
    new_item = _make_item("6b")
    user = _make_staff_user(nomenclature_edit_allowed=True)
    # Владелец документа — иначе get_owned_or_404 отдаст 404 еще до проверки
    # прав на само действие (обычный пользователь видит только свои
    # документы приемки, см. utils.document_access).
    doc = ReceivingDocument(number="RLN-0006", warehouse_id=wh.id, status="draft", created_by_id=user.id)
    db.session.add(doc)
    db.session.commit()
    line = ReceivingLine(document_id=doc.id, nomenclature_id=old_item.id, qty=5)
    db.session.add(line)
    db.session.commit()

    _login_as(client, user)

    resp = client.post(
        f"/receiving/{doc.id}/lines/{line.id}/nomenclature",
        data={"nomenclature_id": new_item.id},
        follow_redirects=True,
    )

    assert "может только администратор" in resp.get_data(as_text=True)
    assert ReceivingLine.query.get(line.id).nomenclature_id == old_item.id
