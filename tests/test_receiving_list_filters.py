"""Список приемок (receiving.list_documents) — галочки "показывать только
незавершенные" и "приемка по накладным". Вторая опирается на supplier_id:
он проставляется только при загрузке накладной (см.
receiving._find_or_create_supplier), у ручных приемок его нет."""

from wms.extensions import db
from wms.models import ReceivingDocument, Warehouse


def _make_warehouse(code="WH-RL"):
    wh = Warehouse(code=code, name="Тест склад")
    db.session.add(wh)
    db.session.commit()
    return wh


def test_unfinished_filter_hides_completed_documents(db, client_logged_in):
    wh = _make_warehouse()
    draft = ReceivingDocument(number="RL-1", warehouse_id=wh.id, status="draft")
    completed = ReceivingDocument(number="RL-2", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()

    html = client_logged_in.get("/receiving/?unfinished=on").get_data(as_text=True)

    assert "RL-1" in html
    assert "RL-2" not in html


def test_without_filter_shows_all_documents(db, client_logged_in):
    wh = _make_warehouse()
    draft = ReceivingDocument(number="RL-3", warehouse_id=wh.id, status="draft")
    completed = ReceivingDocument(number="RL-4", warehouse_id=wh.id, status="completed")
    db.session.add_all([draft, completed])
    db.session.commit()

    html = client_logged_in.get("/receiving/").get_data(as_text=True)

    assert "RL-3" in html
    assert "RL-4" in html


def test_invoice_only_filter_keeps_documents_with_supplier_id(db, client_logged_in):
    from wms.models import Supplier

    wh = _make_warehouse()
    supplier = Supplier(name="ООО Тест")
    db.session.add(supplier)
    db.session.commit()

    manual = ReceivingDocument(number="RL-5", warehouse_id=wh.id, supplier="Просто текст")
    from_invoice = ReceivingDocument(number="RL-6", warehouse_id=wh.id, supplier_id=supplier.id)
    db.session.add_all([manual, from_invoice])
    db.session.commit()

    html = client_logged_in.get("/receiving/?invoice_only=on").get_data(as_text=True)

    assert "RL-5" not in html
    assert "RL-6" in html


def test_supplier_filter_matches_by_substring(db, client_logged_in):
    wh = _make_warehouse("WH-RL-SUP")
    matching = ReceivingDocument(number="RL-7", warehouse_id=wh.id, supplier="ИП Иванов Иван Иванович")
    other = ReceivingDocument(number="RL-8", warehouse_id=wh.id, supplier="ООО Ромашка")
    db.session.add_all([matching, other])
    db.session.commit()

    html = client_logged_in.get("/receiving/?supplier=Иванов").get_data(as_text=True)

    assert "RL-7" in html
    assert "RL-8" not in html


def test_warehouse_filter_keeps_only_matching_warehouse(db, client_logged_in):
    wh1 = _make_warehouse("WH-RL-A")
    wh2 = _make_warehouse("WH-RL-B")
    doc1 = ReceivingDocument(number="RL-9", warehouse_id=wh1.id)
    doc2 = ReceivingDocument(number="RL-10", warehouse_id=wh2.id)
    db.session.add_all([doc1, doc2])
    db.session.commit()

    html = client_logged_in.get(f"/receiving/?warehouse_id={wh1.id}").get_data(as_text=True)

    assert "RL-9" in html
    assert "RL-10" not in html
