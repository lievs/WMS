"""Пока перемещение в статусе "черновик" — его собирают сообща (см.
route_box_add: черновик на маршрут ищется без учета автора, чтобы разные
сотрудники, сканирующие короба на одно направление, попадали в один
документ). Видимость документа должна поспевать за этим: иначе для
сотрудника, который добавил короб не первым, документ выглядит так, будто
короб "потерялся" — не находится через "Найти короб", не виден в его
собственном списке "Перемещение", хотя на самом деле он там и корректно
учтен в остатке потребности (что и выглядит как два несвязанных
симптома одного бага). После завершения документ снова приватен."""

from flask import g

from wms.extensions import db
from wms.models import Box, BoxItem, MovementDocument, MovementLine, Nomenclature, User, Warehouse


def _login_as(client, user):
    g.pop("_login_user", None)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        # В реальности у каждого пользователя своя сессия/cookie — здесь же
        # один и тот же test client переиспользуется под разными
        # пользователями, поэтому явно чистим _flashes: иначе несведенное
        # flash-сообщение предыдущего пользователя (например, из
        # route_box_add) утекало бы в ответ следующему и ломало проверки
        # текста страницы, не будучи настоящим багом видимости.
        sess.pop("_flashes", None)


def _make_staff_user(username):
    user = User(username=username, full_name=username, role="warehouse")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def _make_warehouses(suffix):
    sender = Warehouse(code=f"WH-SDV{suffix}A", name="Склад-отправитель")
    dest = Warehouse(code=f"WH-SDV{suffix}B", name="ОЗОН: Город")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _make_box(warehouse, box_number):
    item = Nomenclature(sku=f"SKU-{box_number}", barcode=f"777050{box_number[-5:]}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number=box_number, warehouse_id=warehouse.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=1))
    db.session.commit()
    return box


def test_find_box_locates_box_added_by_another_user_to_shared_draft(db, client):
    sender, dest = _make_warehouses("1")
    alice = _make_staff_user("alice-sdv1")
    bob = _make_staff_user("bob-sdv1")
    box = _make_box(sender, "BOX-SDV101")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": dest.id})
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()
    assert doc.created_by_id == alice.id

    _login_as(client, bob)
    resp = client.get(f"/movement/find-box?box_number={box.box_number}")

    html = resp.get_data(as_text=True)
    assert doc.number in html


def test_movement_list_shows_shared_draft_to_non_creator(db, client):
    sender, dest = _make_warehouses("2")
    alice = _make_staff_user("alice-sdv2")
    bob = _make_staff_user("bob-sdv2")
    box = _make_box(sender, "BOX-SDV201")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": dest.id})
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()

    _login_as(client, bob)
    resp = client.get("/movement/")

    assert doc.number in resp.get_data(as_text=True)


def test_movement_detail_opens_for_non_creator_while_draft(db, client):
    sender, dest = _make_warehouses("3")
    alice = _make_staff_user("alice-sdv3")
    bob = _make_staff_user("bob-sdv3")
    box = _make_box(sender, "BOX-SDV301")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": dest.id})
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()

    _login_as(client, bob)
    resp = client.get(f"/movement/{doc.id}")

    assert resp.status_code == 200
    assert box.box_number in resp.get_data(as_text=True)


def test_completed_document_stays_private_to_non_creator(db, client):
    """Регрессия: видимость расширяется только для черновиков — уже
    законченную сборку по-прежнему видит только автор/админ (или тот, у
    кого есть отдельное право просмотра всех перемещений)."""
    sender, dest = _make_warehouses("4")
    alice = _make_staff_user("alice-sdv4")
    bob = _make_staff_user("bob-sdv4")
    box = _make_box(sender, "BOX-SDV401")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": dest.id})
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()
    client.post(f"/movement/{doc.id}/complete")
    doc = MovementDocument.query.get(doc.id)
    assert doc.status == "completed"

    _login_as(client, bob)
    resp_detail = client.get(f"/movement/{doc.id}")
    resp_list = client.get("/movement/")
    resp_find = client.get(f"/movement/find-box?box_number={box.box_number}")

    assert resp_detail.status_code == 404
    assert doc.number not in resp_list.get_data(as_text=True)
    assert doc.number not in resp_find.get_data(as_text=True)


def test_completed_document_visible_with_movement_view_permission(db, client):
    sender, dest = _make_warehouses("5")
    alice = _make_staff_user("alice-sdv5")
    bob = _make_staff_user("bob-sdv5")
    bob.movement_view_allowed = True
    db.session.commit()
    box = _make_box(sender, "BOX-SDV501")

    _login_as(client, alice)
    client.post("/movement/route-box/add", data={"box_id": box.id, "to_warehouse_id": dest.id})
    doc = MovementDocument.query.filter_by(from_warehouse_id=sender.id, to_warehouse_id=dest.id).first()
    client.post(f"/movement/{doc.id}/complete")

    _login_as(client, bob)
    resp = client.get(f"/movement/{doc.id}")

    assert resp.status_code == 200
