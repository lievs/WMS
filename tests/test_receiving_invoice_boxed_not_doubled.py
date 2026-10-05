"""Приемка из накладной: если товар из строки накладной уже упаковали в
короб прямо в этой приемке, при завершении он не должен зачисляться еще и
в неразмещенный остаток — иначе остаток задваивается (в коробах 40 и
«неразмещенных» еще 40)."""

from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, UnplacedStock, Warehouse


def _setup(expected):
    wh = Warehouse(code="WH-DBL", name="Склад №2 (Шоссейная 167)")
    item = Nomenclature(sku="kard-sin", barcode="2056744446961", name="кардиган синий", unit="шт")
    db.session.add_all([wh, item])
    db.session.flush()
    box = Box(box_number="BOX-DBL-1", warehouse_id=wh.id)
    doc = ReceivingDocument(number="PR-DBL-1", warehouse_id=wh.id, invoice_file_name="накладная.xlsx", status="draft")
    db.session.add_all([box, doc])
    db.session.flush()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=expected, expected_qty=expected))
    db.session.commit()
    return wh, item, box, doc


def _finish(client, doc, item):
    client.post(f"/receiving/{doc.id}/send-to-recount")
    line = ReceivingLine.query.filter_by(document_id=doc.id, box_id=None, nomenclature_id=item.id).first()
    if line is not None:
        client.post(f"/receiving/{doc.id}/lines/{line.id}/confirm", json={"qty": line.qty, "confirmed": True})
    client.post(f"/receiving/{doc.id}/send-to-sorting")
    client.post(f"/receiving/{doc.id}/complete")


def test_boxed_part_of_invoice_line_not_credited_twice(db, client_logged_in):
    wh, item, box, doc = _setup(40)
    client_logged_in.post(f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": "40"})
    _finish(client_logged_in, doc, item)
    assert ReceivingDocument.query.get(doc.id).status == "completed"
    assert BoxItem.query.filter_by(box_id=box.id).one().qty == 40
    assert UnplacedStock.available(wh.id, item.id) == 0


def test_only_unboxed_rest_of_invoice_line_goes_to_unplaced(db, client_logged_in):
    wh, item, box, doc = _setup(50)
    client_logged_in.post(f"/receiving/{doc.id}/boxes/{box.id}/lines/add", data={"nomenclature_id": item.id, "qty": "40"})
    _finish(client_logged_in, doc, item)
    assert UnplacedStock.available(wh.id, item.id) == 10
