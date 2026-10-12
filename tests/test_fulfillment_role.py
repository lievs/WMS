"""Роль "фулфилмент" (см. чат): видит и ведет только приемки и
перемещения своего склада (warehouse_id) — ничего больше в WMS (другие
разделы, другие склады). Остатки в номенклатуре/приемке учитывают новый
физический склад "ЦЕХ Марат" как третий (см. models.PHYSICAL_STOCK_WAREHOUSE_NAMES)."""

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ReceivingDocument,
    User,
    Warehouse,
)


def _warehouse(code, name="Основной"):
    wh = Warehouse(code=code, name=name)
    db.session.add(wh)
    db.session.commit()
    return wh


def _fulfillment(username, warehouse, suffix=""):
    # nomenclature_edit_allowed=False — как и выставляет
    # auth.create_user/update_role для этой роли (см. FULFILLMENT_SECTIONS);
    # конструируем модель напрямую, а не через маршрут, поэтому значение
    # нужно проставить здесь тоже.
    user = User(
        username=username,
        is_admin=False,
        role="fulfillment",
        warehouse_id=warehouse.id,
        nomenclature_edit_allowed=False,
    )
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_is_fulfillment_only_true_only_for_non_admin():
    wh = Warehouse(id=1, code="X", name="X")
    assert User(role="fulfillment", is_admin=False).is_fulfillment_only() is True
    assert User(role="fulfillment", is_admin=True).is_fulfillment_only() is False
    assert User(role="warehouse", is_admin=False).is_fulfillment_only() is False


def test_create_user_fulfillment_requires_warehouse(client_logged_in, db):
    resp = client_logged_in.post("/users/create", data={"username": "no-wh-fulfillment", "role": "fulfillment"})
    assert resp.status_code == 302
    assert User.query.filter_by(username="no-wh-fulfillment").first() is None


def test_create_user_fulfillment_sets_allowed_sections(client_logged_in, db):
    wh = _warehouse("WH-FF-CREATE", "ЦЕХ Марат")
    resp = client_logged_in.post(
        "/users/create", data={"username": "new-fulfillment", "role": "fulfillment", "warehouse_id": wh.id}
    )
    assert resp.status_code == 302
    created = User.query.filter_by(username="new-fulfillment").first()
    assert created is not None
    assert created.role == "fulfillment"
    assert created.warehouse_id == wh.id
    assert created.allowed_sections == "receiving,movement,nomenclature"
    # Видит общий каталог, но не должен его редактировать (см. чат:
    # "видели номенклатуру общую" — не "правили").
    assert created.nomenclature_edit_allowed is False


def test_update_role_to_fulfillment_requires_warehouse(client_logged_in, db):
    target = User(username="switch-to-fulfillment", is_admin=False, role="warehouse")
    target.set_password("password123")
    db.session.add(target)
    db.session.commit()

    resp = client_logged_in.post(f"/users/{target.id}/role", data={"role": "fulfillment"})

    assert resp.status_code == 302
    assert User.query.get(target.id).role == "warehouse"  # не изменилось — нет склада

    wh = _warehouse("WH-FF-UPD", "ЦЕХ Марат 2")
    target.warehouse_id = wh.id
    db.session.commit()
    resp = client_logged_in.post(f"/users/{target.id}/role", data={"role": "fulfillment"})
    assert resp.status_code == 302
    updated = User.query.get(target.id)
    assert updated.role == "fulfillment"
    assert updated.allowed_sections == "receiving,movement,nomenclature"
    assert updated.nomenclature_edit_allowed is False


def test_fulfillment_sees_only_own_warehouse_sections_in_menu(db, client):
    wh = _warehouse("WH-FF-MENU", "ЦЕХ Марат")
    user = _fulfillment("ff-menu", wh)
    _login(client, user)

    html = client.get("/receiving/").get_data(as_text=True)
    nav = html.split("</nav>")[0]
    assert "Приемка" in nav
    assert "Перемещение" in nav
    # Номенклатура (общий каталог, только на чтение) и Короба (печать
    # своих этикеток) — см. чат: "видели номенклатуру общую", "печатать
    # стикеры для своих коробов".
    assert "Номенклатура" in nav
    assert "Короба" in nav
    assert "Размещение" not in nav
    assert "Инвентаризация" not in nav
    assert "Отчеты" not in nav


def test_fulfillment_blocked_from_other_sections_by_direct_url(db, client):
    wh = _warehouse("WH-FF-DIRECT", "ЦЕХ Марат")
    user = _fulfillment("ff-direct", wh)
    _login(client, user)

    for path in ("/placement/", "/inventory/", "/warehouses/", "/reports/"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")


def test_fulfillment_sees_shared_nomenclature_catalog(db, client):
    """Видит общий каталог номенклатуры (не только своего склада — товары
    сквозные для всех складов), но не может его редактировать."""
    wh = _warehouse("WH-FF-NOM", "ЦЕХ Марат")
    item = Nomenclature(sku="FF-NOM-1", barcode="7779990010", name="Товар каталога", unit="шт")
    db.session.add(item)
    db.session.commit()
    user = _fulfillment("ff-nom", wh)
    _login(client, user)

    resp = client.get("/nomenclature/")
    assert resp.status_code == 200
    assert "Товар каталога" in resp.get_data(as_text=True)

    # Серверная проверка — не только скрытая кнопка в шаблоне
    # (см. nomenclature._require_edit).
    resp = client.post(
        f"/nomenclature/{item.id}/category", data={"category_id": ""}, follow_redirects=True
    )
    assert "вам не разрешено" in resp.get_data(as_text=True)


def test_fulfillment_can_search_nomenclature_via_api(db, client):
    """Автокомплит добавления строки в приемку (см. app.js) ходит сюда —
    без доступа строка не нашла бы товар вообще (чат: "при приемке
    подтягивалась вся номенклатура")."""
    wh = _warehouse("WH-FF-API", "ЦЕХ Марат")
    item = Nomenclature(sku="FF-API-1", barcode="7779990011", name="Товар для поиска", unit="шт")
    db.session.add(item)
    db.session.commit()
    user = _fulfillment("ff-api", wh)
    _login(client, user)

    resp = client.get("/api/nomenclature/search?q=поиска")
    assert resp.status_code == 200
    assert any(row["id"] == item.id for row in resp.get_json())


def test_fulfillment_sees_receiving_of_own_warehouse_from_any_author(db, client):
    wh = _warehouse("WH-FF-RCV", "ЦЕХ Марат")
    other_wh = _warehouse("WH-FF-RCV-OTHER", "Другой склад")
    user = _fulfillment("ff-rcv", wh)
    colleague = User(username="ff-rcv-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    own = ReceivingDocument(number="RCV-FF-1", warehouse_id=wh.id, created_by_id=colleague.id)
    foreign = ReceivingDocument(number="RCV-FF-2", warehouse_id=other_wh.id, created_by_id=colleague.id)
    db.session.add_all([own, foreign])
    db.session.commit()

    _login(client, user)
    html = client.get("/receiving/").get_data(as_text=True)
    assert "RCV-FF-1" in html
    assert "RCV-FF-2" not in html

    assert client.get(f"/receiving/{own.id}").status_code == 200
    assert client.get(f"/receiving/{foreign.id}").status_code == 404


def test_fulfillment_can_only_create_receiving_for_own_warehouse(db, client):
    wh = _warehouse("WH-FF-NEW", "ЦЕХ Марат")
    _warehouse("WH-FF-NEW-OTHER", "основной")
    user = _fulfillment("ff-new", wh)
    _login(client, user)

    html = client.get("/receiving/new").get_data(as_text=True)
    assert "ЦЕХ Марат" in html
    assert "основной" not in html.lower().replace("основной склад", "")

    resp = client.post("/receiving/new", data={"warehouse_id": wh.id, "supplier": ""}, follow_redirects=True)
    assert resp.status_code == 200
    doc = ReceivingDocument.query.filter_by(number=ReceivingDocument.query.order_by(ReceivingDocument.id.desc()).first().number).first()
    assert doc.warehouse_id == wh.id


def test_fulfillment_sees_movements_from_own_warehouse_from_any_author(db, client):
    wh = _warehouse("WH-FF-MV", "ЦЕХ Марат")
    dest = _warehouse("WH-FF-MV-DEST", "Казань")
    other_wh = _warehouse("WH-FF-MV-OTHER", "Другой склад")
    user = _fulfillment("ff-mv", wh)
    colleague = User(username="ff-mv-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    own = MovementDocument(number="MV-FF-1", from_warehouse_id=wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="completed")
    incoming = MovementDocument(number="MV-FF-2", from_warehouse_id=other_wh.id, to_warehouse_id=wh.id, created_by_id=colleague.id, status="completed")
    foreign = MovementDocument(number="MV-FF-3", from_warehouse_id=other_wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="completed")
    db.session.add_all([own, incoming, foreign])
    db.session.commit()

    _login(client, user)
    html = client.get("/movement/").get_data(as_text=True)
    assert "MV-FF-1" in html
    assert "MV-FF-2" not in html  # входящие (to_warehouse) фулфилмент не видит
    assert "MV-FF-3" not in html

    assert client.get(f"/movement/{own.id}").status_code == 200
    assert client.get(f"/movement/{foreign.id}").status_code == 404


def test_fulfillment_can_complete_colleagues_movement_on_own_warehouse(db, client):
    wh = _warehouse("WH-FF-COMPLETE", "ЦЕХ Марат")
    dest = _warehouse("WH-FF-COMPLETE-DEST", "Казань")
    user = _fulfillment("ff-complete", wh)
    colleague = User(username="ff-complete-colleague", is_admin=False, role="fulfillment", warehouse_id=wh.id)
    colleague.set_password("password123")
    db.session.add(colleague)
    db.session.commit()

    item = Nomenclature(sku="FF-SKU-1", barcode="7779990001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    box = Box(box_number="BOX-FF-1", warehouse_id=wh.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=2))
    doc = MovementDocument(number="MV-FF-COMPLETE", from_warehouse_id=wh.id, to_warehouse_id=dest.id, created_by_id=colleague.id, status="draft")
    db.session.add(doc)
    db.session.commit()
    db.session.add(MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=wh.id))
    db.session.commit()

    _login(client, user)
    resp = client.post(f"/movement/{doc.id}/complete")
    assert resp.status_code == 302
    assert MovementDocument.query.get(doc.id).status == "completed"


def test_stock_report_shows_tsekh_marat_as_third_warehouse(db, client_logged_in):
    wh = _warehouse("WH-TSEKH-STOCK", "ЦЕХ Марат")
    item = Nomenclature(sku="FF-STOCK-1", barcode="7779990002", name="Товар на цехе", unit="шт")
    db.session.add(item)
    db.session.commit()
    from wms.models import UnplacedStock

    UnplacedStock.add(wh.id, item.id, 5)

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)
    assert "ЦЕХ Марат" in html


def test_fulfillment_can_print_own_box_label_but_not_others(db, client):
    """"Печатать стикеры для своих коробов" (см. чат) — доступ только к
    этикеткам коробов своего склада, чужой склад — 404."""
    wh = _warehouse("WH-FF-BOX", "ЦЕХ Марат")
    other_wh = _warehouse("WH-FF-BOX-OTHER", "Основной склад")
    own_box = Box(box_number="BOX-FF-OWN", warehouse_id=wh.id, status="open")
    foreign_box = Box(box_number="BOX-FF-FOREIGN", warehouse_id=other_wh.id, status="open")
    db.session.add_all([own_box, foreign_box])
    db.session.commit()
    user = _fulfillment("ff-box", wh)
    _login(client, user)

    assert client.get(f"/boxes/{own_box.id}").status_code == 200
    assert client.get(f"/labels/box/{own_box.id}").status_code == 200
    assert client.get(f"/labels/box/{own_box.id}.pdf").status_code == 200

    assert client.get(f"/boxes/{foreign_box.id}").status_code == 404
    assert client.get(f"/labels/box/{foreign_box.id}").status_code == 404
    assert client.get(f"/labels/box/{foreign_box.id}.pdf").status_code == 404
    assert client.get(f"/labels/boxes/batch.pdf?ids={foreign_box.id}").status_code == 404


def test_fulfillment_box_list_forced_to_own_warehouse(db, client):
    wh = _warehouse("WH-FF-LIST", "ЦЕХ Марат")
    other_wh = _warehouse("WH-FF-LIST-OTHER", "Основной склад")
    own_box = Box(box_number="BOX-FF-LIST-OWN", warehouse_id=wh.id, status="open")
    foreign_box = Box(box_number="BOX-FF-LIST-FOREIGN", warehouse_id=other_wh.id, status="open")
    db.session.add_all([own_box, foreign_box])
    db.session.commit()
    user = _fulfillment("ff-list", wh)
    _login(client, user)

    # Даже если подставить чужой склад в адрес вручную — список все равно
    # фильтруется своим складом (см. boxes.list_boxes).
    html = client.get(f"/boxes/?warehouse_id={other_wh.id}").get_data(as_text=True)
    assert "BOX-FF-LIST-OWN" in html
    assert "BOX-FF-LIST-FOREIGN" not in html


def test_fulfillment_cannot_bulk_create_or_export_boxes(db, client):
    wh = _warehouse("WH-FF-NOEXP", "ЦЕХ Марат")
    user = _fulfillment("ff-noexp", wh)
    _login(client, user)

    assert client.get("/boxes/export.xlsx").status_code == 302
    assert client.get("/boxes/bulk-create").status_code == 302
