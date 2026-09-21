"""Объединение нескольких черновиков перемещения на один маршрут в один
документ (movement.merge_documents) — для админа: несколько сотрудников
могли собирать одно направление порознь и получить разные документы
вместо одного. Короба (MovementLine) переезжают в новый документ без
задвоения, исходные помечаются "merged" и остаются в истории."""

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, User, Warehouse


def _make_warehouses(suffix):
    sender = Warehouse(code=f"WH-MG{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-MG{suffix}B", name="ОЗОН: Город")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-MG{suffix}", barcode=f"77701000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_doc_with_box(sender, dest, item, number, box_number, qty=5):
    doc = MovementDocument(number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc)
    db.session.commit()
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=sender.id, from_cell_id=box.cell_id)
    )
    db.session.commit()
    return doc, box


def _make_staff_user():
    user = User(username="staffer-mg", full_name="Складской", role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _login_as(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_merge_moves_boxes_into_new_document_and_marks_source_merged(db, client_logged_in):
    sender, dest = _make_warehouses("1")
    item = _make_item("1")
    doc1, box1 = _make_doc_with_box(sender, dest, item, "PER-MG-0001", "BOX-MG101")
    doc2, box2 = _make_doc_with_box(sender, dest, item, "PER-MG-0002", "BOX-MG102")

    resp = client_logged_in.post(
        "/movement/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )

    assert resp.status_code == 200
    doc1 = MovementDocument.query.get(doc1.id)
    doc2 = MovementDocument.query.get(doc2.id)
    assert doc1.status == "merged"
    assert doc2.status == "merged"
    assert doc1.merged_into_id == doc2.merged_into_id
    assert doc1.lines.count() == 0
    assert doc2.lines.count() == 0

    merged = MovementDocument.query.get(doc1.merged_into_id)
    assert merged.status == "draft"
    assert merged.from_warehouse_id == sender.id
    assert merged.to_warehouse_id == dest.id
    box_ids = {line.box_id for line in merged.lines}
    assert box_ids == {box1.id, box2.id}


def test_merge_requires_admin(db, client):
    sender, dest = _make_warehouses("2")
    item = _make_item("2")
    doc1, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0003", "BOX-MG103")
    doc2, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0004", "BOX-MG104")
    user = _make_staff_user()
    _login_as(client, user)

    client.post("/movement/merge", data={"doc_ids": [doc1.id, doc2.id]})

    assert MovementDocument.query.get(doc1.id).status == "draft"
    assert MovementDocument.query.get(doc2.id).status == "draft"


def test_merge_rejects_different_routes(db, client_logged_in):
    sender, dest = _make_warehouses("3")
    other_dest = Warehouse(code="WH-MG3C", name="ВБ: Другой город")
    db.session.add(other_dest)
    db.session.commit()
    item = _make_item("3")
    doc1, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0005", "BOX-MG105")
    doc2 = MovementDocument(number="PER-MG-0006", from_warehouse_id=sender.id, to_warehouse_id=other_dest.id)
    db.session.add(doc2)
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )

    assert "разные маршруты" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc1.id).status == "draft"
    assert MovementDocument.query.get(doc2.id).status == "draft"


def test_merge_rejects_non_draft_documents(db, client_logged_in):
    sender, dest = _make_warehouses("4")
    item = _make_item("4")
    doc1, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0007", "BOX-MG107")
    doc2, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0008", "BOX-MG108")
    doc2.status = "completed"
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )

    assert "только черновики" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc1.id).status == "draft"


def test_merge_requires_at_least_two_documents(db, client_logged_in):
    sender, dest = _make_warehouses("5")
    item = _make_item("5")
    doc1, _ = _make_doc_with_box(sender, dest, item, "PER-MG-0009", "BOX-MG109")

    resp = client_logged_in.post("/movement/merge", data={"doc_ids": [doc1.id]}, follow_redirects=True)

    assert "минимум два" in resp.get_data(as_text=True)
    assert MovementDocument.query.get(doc1.id).status == "draft"


def test_merge_deduplicates_same_box_added_to_both_documents(db, client_logged_in):
    """Не должно происходить на практике (блокируется при сканировании), но
    объединение все равно не должно задваивать короб, если он как-то
    оказался в обоих документах."""
    sender, dest = _make_warehouses("6")
    item = _make_item("6")
    doc1, box1 = _make_doc_with_box(sender, dest, item, "PER-MG-0010", "BOX-MG110")
    doc2 = MovementDocument(number="PER-MG-0011", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add(doc2)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc2.id, box_id=box1.id, from_warehouse_id=sender.id, from_cell_id=box1.cell_id)
    )
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/merge", data={"doc_ids": [doc1.id, doc2.id]}, follow_redirects=True
    )

    doc1 = MovementDocument.query.get(doc1.id)
    merged = MovementDocument.query.get(doc1.merged_into_id)
    assert merged.lines.count() == 1
    assert "учтены один раз" in resp.get_data(as_text=True)
