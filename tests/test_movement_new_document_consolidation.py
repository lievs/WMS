"""Создание нового перемещения через форму "Новое перемещение" (не через
"Куда везти короб") теперь присоединяется к уже существующему черновику на
тот же маршрут (тот же склад-отправитель и склад назначения), если такой
есть — вместо создания дубля. Несколько сотрудников, собирающих одно
направление независимо друг от друга, должны попасть в один документ."""

from wms.extensions import db
from wms.models import MovementDocument, Warehouse


def _make_warehouses(suffix):
    sender = Warehouse(code=f"WH-ND{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-ND{suffix}B", name="ОЗОН: Город")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def test_new_document_joins_existing_draft_on_same_route(db, client_logged_in):
    sender, dest = _make_warehouses("1")
    existing = MovementDocument(
        number="PER-ND-0001", from_warehouse_id=sender.id, to_warehouse_id=dest.id
    )
    db.session.add(existing)
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/new",
        data={"from_warehouse_id": sender.id, "to_warehouse_id": dest.id},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).count() == 1
    assert resp.request.path == f"/movement/{existing.id}"
    assert "уже есть черновик" in resp.get_data(as_text=True)


def test_new_document_creates_new_when_no_draft_on_route(db, client_logged_in):
    sender, dest = _make_warehouses("2")

    resp = client_logged_in.post(
        "/movement/new",
        data={"from_warehouse_id": sender.id, "to_warehouse_id": dest.id},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).count() == 1


def test_new_document_does_not_join_completed_document_on_same_route(db, client_logged_in):
    sender, dest = _make_warehouses("3")
    completed = MovementDocument(
        number="PER-ND-0002", from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed"
    )
    db.session.add(completed)
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/new",
        data={"from_warehouse_id": sender.id, "to_warehouse_id": dest.id},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).count() == 2


def test_new_document_does_not_join_draft_with_different_sender(db, client_logged_in):
    sender, dest = _make_warehouses("4")
    other_sender = Warehouse(code="WH-ND4C", name="Другой склад-отправитель")
    db.session.add(other_sender)
    db.session.commit()
    existing = MovementDocument(
        number="PER-ND-0003", from_warehouse_id=other_sender.id, to_warehouse_id=dest.id
    )
    db.session.add(existing)
    db.session.commit()

    resp = client_logged_in.post(
        "/movement/new",
        data={"from_warehouse_id": sender.id, "to_warehouse_id": dest.id},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    new_doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()
    assert new_doc is not None
    assert new_doc.id != existing.id
