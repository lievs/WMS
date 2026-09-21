"""Администратор может удалить приемку в любом статусе (см.
receiving.delete_document). Черновик/пересчет/разбраковка еще не повлияли
на остатки — удаляются как есть. Завершенная приемка уже зачислила
неразмещенный остаток — при удалении он списывается обратно, точно так
же, как при возврате на разбраковку (см. receiving.revert_to_sorting);
если часть остатка уже размещена в короба — откатить нельзя, документ не
удаляется."""

from wms.extensions import db
from wms.models import (
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    SupplierReturn,
    UnplacedStock,
    UnplacedStockLot,
    User,
    Warehouse,
)


def _make_warehouse(suffix):
    wh = Warehouse(code=f"WH-RDEL{suffix}", name="Склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-RDEL{suffix}", barcode=f"77706000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_doc(warehouse, number, from_invoice=True):
    doc = ReceivingDocument(
        number=number,
        warehouse_id=warehouse.id,
        supplier="ИП Тестов",
        invoice_file_name="накладная.xlsx" if from_invoice else None,
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def test_admin_can_delete_draft_document(db, client_logged_in):
    wh = _make_warehouse("1")
    item = _make_item("1")
    doc = _make_doc(wh, "RDEL-0001")
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=5))
    db.session.commit()

    resp = client_logged_in.post(f"/receiving/{doc.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert ReceivingDocument.query.get(doc.id) is None


def test_admin_can_delete_completed_document_and_stock_reverts(db, client_logged_in):
    wh = _make_warehouse("2")
    item = _make_item("2")
    doc = _make_doc(wh, "RDEL-0002")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10, expected_qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(
        f"/receiving/{doc.id}/lines/{line.id}/confirm",
        json={"qty": 10, "confirmed": True},
    )
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/complete")
    assert UnplacedStock.available(wh.id, item.id) == 10

    resp = client_logged_in.post(f"/receiving/{doc.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert ReceivingDocument.query.get(doc.id) is None
    assert UnplacedStock.available(wh.id, item.id) == 0
    assert UnplacedStockLot.query.filter_by(receiving_document_id=doc.id).count() == 0


def test_delete_completed_document_removes_unsynced_return_and_warns_about_synced(db, client_logged_in):
    wh = _make_warehouse("3")
    item = _make_item("3")
    doc = _make_doc(wh, "RDEL-0003")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/lines/{line.id}/update-defect", data={"defect_qty": "3"})
    client_logged_in.post(f"/receiving/{doc.id}/complete")
    ret = SupplierReturn.query.filter_by(receiving_document_id=doc.id).first()
    assert ret is not None
    ret.synced_to_1c_at = db.func.now()
    db.session.commit()

    resp = client_logged_in.post(f"/receiving/{doc.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert ReceivingDocument.query.get(doc.id) is None
    # Уже выгруженный в 1С возврат не трогаем — он остается как исторический факт.
    assert SupplierReturn.query.get(ret.id) is not None
    assert "не отменены" in resp.get_data(as_text=True)


def test_delete_completed_document_blocked_once_stock_already_placed(db, client_logged_in):
    wh = _make_warehouse("4")
    item = _make_item("4")
    doc = _make_doc(wh, "RDEL-0004")
    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=10)
    db.session.add(line)
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    client_logged_in.post(f"/receiving/{doc.id}/complete")

    # Часть уже разместили в короб (как это делает "Размещение").
    UnplacedStock.consume(wh.id, item.id, 3)
    db.session.commit()

    resp = client_logged_in.post(f"/receiving/{doc.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert ReceivingDocument.query.get(doc.id) is not None
    assert "уже частично размещен" in resp.get_data(as_text=True)
    assert UnplacedStock.available(wh.id, item.id) == 7


def test_delete_document_still_requires_admin(db, client):
    wh = _make_warehouse("5")
    doc = _make_doc(wh, "RDEL-0005")
    user = User(username="staffer-rdel", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    client.post(f"/receiving/{doc.id}/delete")

    assert ReceivingDocument.query.get(doc.id) is not None
