"""Отчет «Статусы приемки» (reports.receiving_status_report) — по каждой
приемке: статус, сколько дней она в нем висит (см. reports.
_receiving_status_since), суммарное кол-во товара по строкам и вид(ы)
товара (ProductCategory.name)."""

from datetime import datetime, timedelta

from wms.extensions import db
from wms.models import Nomenclature, ProductCategory, ReceivingDocument, ReceivingLine, Warehouse


def _make_warehouse(code):
    wh = Warehouse(code=code, name=f"Склад {code}")
    db.session.add(wh)
    db.session.commit()
    return wh


def _make_item(barcode, category=None):
    item = Nomenclature(sku=barcode, barcode=barcode, name="Товар отчета", unit="шт", category_id=category.id if category else None)
    db.session.add(item)
    db.session.commit()
    return item


def _make_category(name):
    cat = ProductCategory(name=name)
    db.session.add(cat)
    db.session.commit()
    return cat


def test_report_shows_status_qty_and_category(db, client_logged_in):
    wh = _make_warehouse("WH-RSR-1")
    cat = _make_category("Вид-отчета-RSR")
    item = _make_item("9991000001", category=cat)
    doc = ReceivingDocument(number="RSR-0001", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=6))
    db.session.commit()

    html = client_logged_in.get("/reports/receiving-status").get_data(as_text=True)

    assert "RSR-0001" in html
    assert "Черновик" in html
    assert "Вид-отчета-RSR" in html
    assert ">6<" in html


def test_report_computes_days_in_status_from_recounting_started_at(db, client_logged_in):
    wh = _make_warehouse("WH-RSR-2")
    item = _make_item("9991000002")
    doc = ReceivingDocument(number="RSR-0002", warehouse_id=wh.id, invoice_file_name="накладная.xlsx")
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=1, expected_qty=1))
    db.session.commit()

    client_logged_in.post(f"/receiving/{doc.id}/send-to-recount")
    doc = ReceivingDocument.query.get(doc.id)
    doc.recounting_started_at = datetime.utcnow() - timedelta(days=4)
    db.session.commit()

    html = client_logged_in.get("/reports/receiving-status").get_data(as_text=True)

    assert "На пересчете" in html
    assert ">4<" in html


def test_unfinished_only_filter_hides_completed(db, client_logged_in):
    wh = _make_warehouse("WH-RSR-3")
    item = _make_item("9991000003")
    draft = ReceivingDocument(number="RSR-0003", warehouse_id=wh.id)
    completed = ReceivingDocument(number="RSR-0004", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()
    db.session.add_all(
        [
            ReceivingLine(document_id=draft.id, nomenclature_id=item.id, qty=1),
            ReceivingLine(document_id=completed.id, nomenclature_id=item.id, qty=1),
        ]
    )
    db.session.commit()

    html = client_logged_in.get("/reports/receiving-status?unfinished=on").get_data(as_text=True)

    assert "RSR-0003" in html
    assert "RSR-0004" not in html


def test_report_filters_by_warehouse(db, client_logged_in):
    wh1 = _make_warehouse("WH-RSR-5")
    wh2 = _make_warehouse("WH-RSR-6")
    item = _make_item("9991000005")
    doc1 = ReceivingDocument(number="RSR-0005", warehouse_id=wh1.id)
    doc2 = ReceivingDocument(number="RSR-0006", warehouse_id=wh2.id)
    db.session.add_all([doc1, doc2])
    db.session.commit()
    db.session.add_all(
        [
            ReceivingLine(document_id=doc1.id, nomenclature_id=item.id, qty=1),
            ReceivingLine(document_id=doc2.id, nomenclature_id=item.id, qty=1),
        ]
    )
    db.session.commit()

    html = client_logged_in.get(f"/reports/receiving-status?warehouse_id={wh1.id}").get_data(as_text=True)

    assert "RSR-0005" in html
    assert "RSR-0006" not in html


def test_report_excel_export_returns_xlsx(db, client_logged_in):
    wh = _make_warehouse("WH-RSR-7")
    item = _make_item("9991000007")
    doc = ReceivingDocument(number="RSR-0007", warehouse_id=wh.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=2))
    db.session.commit()

    resp = client_logged_in.get("/reports/receiving-status.xlsx")

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
