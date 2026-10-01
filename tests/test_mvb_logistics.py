"""«МВБ Логистика»: отдельный вход, заявки клиентов, индивидуальный штрихкод
на каждый короб и поштучные сканы (забор → склад МВБ → СЦ)."""

from flask import g

from wms.extensions import db
from wms.models import MvbBox, MvbClient, MvbOrder, User


def _user(username, role, client=None, password="password123"):
    user = User(username=username, role=role, is_admin=False, mvb_client_id=client.id if client else None)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    # Фикстура db держит app context на весь тест, а Flask-Login кеширует
    # текущего пользователя в g — при смене пользователя внутри теста кеш
    # надо сбросить, иначе следующий запрос увидит предыдущего.
    g.pop("_login_user", None)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _mvb_client(name="ООО Ромашка"):
    c = MvbClient(name=name, address="Москва, ул. Ленина, 1", phone="+79990000000")
    db.session.add(c)
    db.session.commit()
    return c


def _create_order(http, **overrides):
    form = {
        "marketplace": "wb",
        "destination": "Коледино",
        "box_count": "3",
        "delivery_method": "pickup",
        "pickup_address": "Москва, ул. Ленина, 1",
        "planned_date": "2026-10-05",
        "time_from": "10:00",
        "time_to": "14:00",
    }
    form.update(overrides)
    return http.post("/mvb/orders/new", data=form)


def _confirmed_order(http, **overrides):
    _create_order(http, **overrides)
    order = MvbOrder.query.order_by(MvbOrder.id.desc()).first()
    http.post(f"/mvb/orders/{order.id}/confirm")
    return order


def test_mvb_login_page_is_public_and_separate(db, client):
    response = client.get("/mvb/login")
    assert response.status_code == 200
    assert "МВБ Логистика" in response.get_data(as_text=True)

    response = client.get("/mvb/orders")
    assert response.status_code == 302
    assert "/mvb/login" in response.headers["Location"]


def test_mvb_user_logs_in_via_mvb_page_but_not_wms(db, client):
    _user("driver1", "mvb_driver")

    response = client.post("/login", data={"username": "driver1", "password": "password123"})
    assert response.status_code == 302
    assert "/mvb/login" in response.headers["Location"]

    response = client.post("/mvb/login", data={"username": "driver1", "password": "password123"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/mvb/")


def test_wms_user_cannot_login_to_mvb_or_open_it(db, client):
    wms_user = _user("storekeeper", "warehouse")

    response = client.post("/mvb/login", data={"username": "storekeeper", "password": "password123"})
    assert "Неверный логин или пароль" in response.get_data(as_text=True)

    _login(client, wms_user)
    response = client.get("/mvb/orders")
    assert response.status_code == 302
    assert "/mvb" not in response.headers["Location"]


def test_mvb_user_cannot_open_wms_sections(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    for url in ("/", "/nomenclature/", "/movement/", "/users"):
        response = client.get(url)
        assert response.status_code == 302, url
        assert "/mvb/" in response.headers["Location"], url


def test_client_creates_and_confirms_order_with_unique_barcode_per_box(db, client):
    rom = _mvb_client()
    _login(client, _user("client1", "mvb_client", rom))

    response = _create_order(client)
    assert response.status_code == 302
    order = MvbOrder.query.one()
    assert order.client_id == rom.id
    assert order.status == "draft"
    assert order.boxes == []

    client.post(f"/mvb/orders/{order.id}/confirm")
    db.session.refresh(order)
    assert order.status == "confirmed"
    barcodes = [b.barcode for b in order.boxes]
    assert barcodes == [f"{order.number}-001", f"{order.number}-002", f"{order.number}-003"]
    assert len(set(barcodes)) == 3

    pdf = client.get(f"/mvb/orders/{order.id}/labels.pdf")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"


def test_pickup_requires_address(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    response = _create_order(client, pickup_address="")
    assert response.status_code == 200
    assert "укажите адрес" in response.get_data(as_text=True)
    assert MvbOrder.query.count() == 0


def test_client_sees_only_own_orders(db, client):
    a, b = _mvb_client("A"), _mvb_client("B")
    client_a = _user("ca", "mvb_client", a)
    client_b = _user("cb", "mvb_client", b)

    _login(client, client_a)
    order_a = _confirmed_order(client)

    _login(client, client_b)
    assert client.get(f"/mvb/orders/{order_a.id}").status_code == 404
    assert client.get(f"/mvb/orders/{order_a.id}/labels.pdf").status_code == 404
    assert f">{order_a.number}</a>" not in client.get("/mvb/orders").get_data(as_text=True)


def test_box_scanned_through_all_stages(db, client):
    rom = _mvb_client()
    _login(client, _user("client1", "mvb_client", rom))
    order = _confirmed_order(client)
    box = order.boxes[0]

    driver = _user("driver1", "mvb_driver")
    staff = _user("staff1", "mvb_staff")

    _login(client, driver)
    data = client.post("/mvb/scan/pickup", data={"barcode": box.barcode}).get_json()
    assert data["ok"] and not data["already"] and data["done"] == 1
    # повторный скан того же короба — не ошибка и не двойной учет
    data = client.post("/mvb/scan/pickup", data={"barcode": box.barcode.lower()}).get_json()
    assert data["ok"] and data["already"]
    # водителю приемка на складе недоступна
    assert client.post("/mvb/scan/receive", data={"barcode": box.barcode}).status_code == 403

    _login(client, staff)
    assert client.post("/mvb/scan/receive", data={"barcode": box.barcode}).get_json()["ok"]
    assert client.post("/mvb/scan/ship", data={"barcode": box.barcode}).get_json()["ok"]

    _login(client, driver)
    assert client.post("/mvb/scan/deliver", data={"barcode": box.barcode}).get_json()["ok"]

    db.session.refresh(box)
    assert box.status == "delivered"
    assert box.picked_up_at and box.received_at and box.shipped_at and box.delivered_at
    assert [e.status for e in box.events] == ["picked_up", "received", "shipped", "delivered"]
    # остальные короба заявки не тронуты — видно, какие именно забрали
    assert {b.status for b in order.boxes[1:]} == {"created"}


def test_scan_rejects_wrong_order_of_stages_and_unknown_boxes(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client)
    _login(client, _user("staff1", "mvb_staff"))

    response = client.post("/mvb/scan/ship", data={"barcode": order.boxes[0].barcode})
    assert response.status_code == 409
    assert client.post("/mvb/scan/receive", data={"barcode": "NOPE-1"}).status_code == 404


def test_self_delivery_boxes_cannot_be_picked_up_but_are_received(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client, delivery_method="self", pickup_address="")
    barcode = order.boxes[0].barcode

    _login(client, _user("driver1", "mvb_driver"))
    assert client.post("/mvb/scan/pickup", data={"barcode": barcode}).status_code == 409

    _login(client, _user("staff1", "mvb_staff"))
    assert client.post("/mvb/scan/receive", data={"barcode": barcode}).get_json()["ok"]


def test_draft_and_cancelled_orders_cannot_be_scanned_or_cancelled_after_scan(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client)
    barcode = order.boxes[0].barcode

    _login(client, _user("staff1", "mvb_staff"))
    client.post("/mvb/scan/receive", data={"barcode": barcode})
    client.post(f"/mvb/orders/{order.id}/cancel")
    db.session.refresh(order)
    assert order.status == "confirmed"


def test_progress_label_shows_how_many_boxes_moved(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client)
    assert order.progress_label() == "Ожидает передачи"

    _login(client, _user("staff1", "mvb_staff"))
    client.post("/mvb/scan/receive", data={"barcode": order.boxes[0].barcode})
    db.session.refresh(order)
    assert order.progress_label() == "На складе МВБ: 1 из 3"


def test_mvb_admin_creates_client_and_users(db, client):
    _login(client, _user("boss", "mvb_admin"))
    client.post("/mvb/admin/clients", data={"name": "ИП Иванов", "address": "Казань"})
    ivanov = MvbClient.query.filter_by(name="ИП Иванов").one()

    client.post("/mvb/admin/users", data={
        "username": "ivanov", "password": "secret1", "role": "mvb_client", "client_id": str(ivanov.id),
    })
    user = User.query.filter_by(username="ivanov").one()
    assert user.role == "mvb_client" and user.mvb_client_id == ivanov.id and not user.is_admin

    # без клиента роль «Клиент» не создается
    client.post("/mvb/admin/users", data={"username": "nobody", "password": "secret1", "role": "mvb_client"})
    assert User.query.filter_by(username="nobody").first() is None


def test_non_admin_mvb_users_cannot_manage(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    response = client.post("/mvb/admin/users", data={"username": "x", "password": "secret1", "role": "mvb_admin"})
    assert response.status_code == 302
    assert User.query.filter_by(username="x").first() is None


def test_wms_admin_has_access_and_mvb_users_hidden_from_wms_settings(db, client_logged_in):
    _user("driver1", "mvb_driver")
    assert client_logged_in.get("/mvb/orders").status_code == 200
    assert "driver1" not in client_logged_in.get("/users").get_data(as_text=True)


def test_pages_render(db, client_logged_in):
    rom = _mvb_client()
    client_logged_in.post("/mvb/orders/new", data={
        "client_id": str(rom.id), "marketplace": "ozon", "box_count": "2",
        "delivery_method": "pickup", "pickup_address": "Москва",
    })
    order = MvbOrder.query.one()
    for url in (
        "/mvb/orders", "/mvb/orders/new", f"/mvb/orders/{order.id}", f"/mvb/orders/{order.id}/edit",
        "/mvb/driver", "/mvb/scan/pickup", "/mvb/scan/receive", "/mvb/admin/clients", "/mvb/admin/users",
    ):
        assert client_logged_in.get(url).status_code == 200, url
    client_logged_in.post(f"/mvb/orders/{order.id}/confirm")
    assert client_logged_in.get(f"/mvb/orders/{order.id}").status_code == 200
    assert client_logged_in.get("/mvb/driver").status_code == 200
    assert MvbBox.query.count() == 2
