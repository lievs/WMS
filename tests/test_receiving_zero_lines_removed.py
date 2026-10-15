"""Приемка по накладной: товар, которого совсем не привезли (на пересчете
выставлен ноль), удаляется из документа при переходе на разбраковку —
чтобы при сверке расхождений не было строк-нулей. Запись об удаленном
остается (removed_zero_lines): видна на странице и уходит в 1С нулем."""

from wms.extensions import db
from wms.models import Nomenclature, ReceivingDocument, ReceivingLine, Warehouse
from wms.blueprints.integration_1c import _receiving_adjustments_candidates, _receiving_adjustments_export


def _doc():
    wh = Warehouse(code="WH-ZL", name="Склад ZL")
    db.session.add(wh)
    db.session.commit()
    items = []
    for i in range(3):
        item = Nomenclature(sku=f"ZL-{i}", barcode=f"ZL000{i}", name=f"Позиция {i}", unit="шт")
        db.session.add(item)
        items.append(item)
    db.session.commit()
    doc = ReceivingDocument(number="ZL-0001", warehouse_id=wh.id, supplier="ИП", invoice_file_name="inv.xlsx",
                            status="recounting")
    db.session.add(doc)
    db.session.commit()
    lines = [
        ReceivingLine(document_id=doc.id, nomenclature_id=items[0].id, qty=10, expected_qty=10, confirmed=True),
        ReceivingLine(document_id=doc.id, nomenclature_id=items[1].id, qty=0, expected_qty=40, confirmed=True),
        ReceivingLine(document_id=doc.id, nomenclature_id=items[2].id, qty=7, expected_qty=9, confirmed=True),
    ]
    db.session.add_all(lines)
    db.session.commit()
    return doc, items


def test_zero_lines_removed_on_send_to_sorting_but_recorded(db, client_logged_in):
    doc, items = _doc()
    resp = client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting", follow_redirects=True)
    html = resp.get_data(as_text=True)

    doc = ReceivingDocument.query.get(doc.id)
    assert doc.status == "sorting"
    assert sorted(l.qty for l in doc.lines) == [7, 10]            # нулевая строка удалена
    assert doc.removed_zero_items() == [{"barcode": "ZL0001", "name": "Позиция 1", "expected_qty": 40}]
    assert "Удалены строки с нулевым количеством" in html
    assert "Не привезли" in html and "Позиция 1" in html


def test_nothing_removed_when_no_zero_lines(db, client_logged_in):
    doc, items = _doc()
    ReceivingLine.query.filter_by(document_id=doc.id, qty=0).first().qty = 3
    db.session.commit()
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    doc = ReceivingDocument.query.get(doc.id)
    assert doc.lines.count() == 3 and doc.removed_zero_lines is None


def test_1c_adjustment_still_zeroes_removed_item(db, client_logged_in):
    doc, items = _doc()
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    assert [d.id for d, _ in _receiving_adjustments_candidates()] == [doc.id]
    payload = _receiving_adjustments_export()[0]
    by_barcode = {l["barcode"]: l["qty"] for l in payload["lines"]}
    assert by_barcode == {"ZL0000": 10, "ZL0002": 7, "ZL0001": 0}


def test_doc_where_only_zero_line_was_removed_still_goes_to_1c_and_is_highlighted(db, client_logged_in):
    doc, items = _doc()
    ReceivingLine.query.filter_by(document_id=doc.id, nomenclature_id=items[2].id).first().qty = 9
    db.session.commit()
    client_logged_in.post(f"/receiving/{doc.id}/send-to-sorting")
    assert [d.id for d, _ in _receiving_adjustments_candidates()] == [doc.id]
    html = client_logged_in.get("/receiving/").get_data(as_text=True)
    assert "table-warning" in html and "ZL-0001" in html
