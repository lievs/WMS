"""«МВБ Логистика»: отдельный вход, заявки клиентов, индивидуальный штрихкод
на каждый короб и поштучные сканы (забор → склад МВБ → СЦ)."""

from flask import g

from wms.extensions import db
from wms.models import MvbBox, MvbClient, MvbOrder, MvbPallet, MvbTrip, MvbVehicle, User


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


def _vehicle(capacity=10, driver=None):
    v = MvbVehicle(plate="А123ВС77", capacity_boxes=capacity, driver_id=driver.id if driver else None)
    db.session.add(v)
    db.session.commit()
    return v


def _trip(http, marketplace="wb", destination="Коледино", driver=None, capacity=10):
    """Рейс (вызывать под пользователем склада): создан и авто назначено."""
    http.post("/mvb/trips/new", data={"marketplace": marketplace, "destination": destination})
    trip = MvbTrip.query.order_by(MvbTrip.id.desc()).first()
    vehicle = _vehicle(capacity, driver)
    http.post(f"/mvb/trips/{trip.id}/plan", data={
        "vehicle_id": str(vehicle.id), "planned_arrival_at": "2026-10-06T09:00",
    })
    db.session.refresh(trip)
    return trip


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
    trip = _trip(client, driver=driver)
    assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode}).get_json()["ok"]
    client.post(f"/mvb/trips/{trip.id}/depart")

    _login(client, driver)
    client.post(f"/mvb/trips/{trip.id}/deliver")

    db.session.refresh(box)
    assert box.status == "delivered"
    assert box.picked_up_at and box.received_at and box.loaded_at and box.shipped_at and box.delivered_at
    assert [e.status for e in box.events] == ["picked_up", "received", "loaded", "shipped", "delivered"]
    # остальные короба заявки не тронуты — видно, какие именно забрали
    assert {b.status for b in order.boxes[1:]} == {"created"}


def test_scan_rejects_wrong_order_of_stages_and_unknown_boxes(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client)
    _login(client, _user("staff1", "mvb_staff"))

    # короб еще не принят на складе — в рейс его не погрузить
    trip = _trip(client)
    response = client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[0].barcode})
    assert response.status_code == 409
    assert client.post("/mvb/scan/ship", data={"barcode": order.boxes[0].barcode}).status_code == 404
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


# ---------- этап 2: транспорт, паллеты, рейсы, пропуск ----------


def _received_order(http, client_user, staff, **overrides):
    _login(http, client_user)
    order = _confirmed_order(http, **overrides)
    _login(http, staff)
    for box in order.boxes:
        http.post("/mvb/scan/receive", data={"barcode": box.barcode})
    return order


def test_trip_lifecycle_with_plan_and_fact(db, client):
    rom = _mvb_client()
    client_user = _user("client1", "mvb_client", rom)
    staff = _user("staff1", "mvb_staff")
    driver = _user("driver1", "mvb_driver")
    order = _received_order(client, client_user, staff)

    client.post("/mvb/trips/new", data={"marketplace": "wb", "destination": "Коледино", "planned_boxes": "3"})
    trip = MvbTrip.query.one()
    assert trip.status == "searching"
    # без авто погрузка закрыта
    assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[0].barcode}).status_code == 409

    vehicle = _vehicle(capacity=2, driver=driver)
    client.post(f"/mvb/trips/{trip.id}/plan", data={
        "vehicle_id": str(vehicle.id), "planned_arrival_at": "2026-10-06T09:00",
        "planned_load_start_at": "2026-10-06T09:15", "planned_load_end_at": "2026-10-06T10:00",
    })
    db.session.refresh(trip)
    assert trip.status == "assigned"
    assert trip.driver_id == driver.id  # водитель подставлен из авто
    assert trip.planned_arrival_at.hour == 6  # 09:00 МСК хранится как 06:00 UTC

    client.post(f"/mvb/trips/{trip.id}/arrive")
    db.session.refresh(trip)
    assert trip.status == "arrived" and trip.arrived_at

    r1 = client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[0].barcode}).get_json()
    assert r1["ok"] and r1["count"] == 1 and r1["warning"] is None
    db.session.refresh(trip)
    assert trip.status == "loading" and trip.load_started_at
    client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[1].barcode})
    r3 = client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[2].barcode}).get_json()
    assert r3["ok"] and "вместимости" in r3["warning"]

    client.post(f"/mvb/trips/{trip.id}/depart")
    db.session.refresh(trip)
    assert trip.status == "departed" and trip.load_finished_at and trip.departed_at
    assert {b.status for b in order.boxes} == {"shipped"}

    _login(client, driver)
    assert client.get("/mvb/driver").status_code == 200
    client.post(f"/mvb/trips/{trip.id}/deliver")
    db.session.refresh(trip)
    assert trip.status == "delivered"
    assert {b.status for b in order.boxes} == {"delivered"}


def test_driver_cannot_operate_other_trips_or_staff_actions(db, client):
    staff = _user("staff1", "mvb_staff")
    driver = _user("driver1", "mvb_driver")
    other = _user("driver2", "mvb_driver")
    _login(client, staff)
    trip = _trip(client, driver=driver)

    _login(client, other)
    assert client.get(f"/mvb/trips/{trip.id}").status_code == 404
    _login(client, driver)
    assert client.post(f"/mvb/trips/{trip.id}/start_loading").status_code == 403
    assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": "x"}).status_code == 403


def test_trip_rejects_other_direction_and_cancel_returns_boxes(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    order = _received_order(client, client_user, staff, marketplace="ozon", destination="Хоругвино")
    trip = _trip(client, marketplace="wb", destination="Коледино")
    assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[0].barcode}).status_code == 409

    ozon_trip = _trip(client, marketplace="ozon", destination="хоругвино")
    assert client.post(f"/mvb/trips/{ozon_trip.id}/scan", data={"barcode": order.boxes[0].barcode}).get_json()["ok"]
    client.post(f"/mvb/trips/{ozon_trip.id}/cancel")
    box = db.session.get(MvbBox, order.boxes[0].id)
    assert box.status == "received" and box.trip_id is None and box.loaded_at is None


def test_pallet_scan_and_load_whole_pallet(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    order = _received_order(client, client_user, staff)
    other = _received_order(client, client_user, staff, marketplace="ozon", box_count="1")

    client.post("/mvb/pallets", data={"marketplace": "wb", "destination": "Коледино"})
    pallet = MvbPallet.query.one()
    for box in order.boxes[:2]:
        assert client.post(f"/mvb/pallets/{pallet.id}/scan", data={"barcode": box.barcode}).get_json()["ok"]
    # другое направление на паллету не встает
    assert client.post(f"/mvb/pallets/{pallet.id}/scan", data={"barcode": other.boxes[0].barcode}).status_code == 409
    assert client.get(f"/mvb/pallets/{pallet.id}/label.pdf").mimetype == "application/pdf"
    assert client.get("/mvb/pallets").status_code == 200
    assert client.get(f"/mvb/pallets/{pallet.id}").status_code == 200

    trip = _trip(client)
    data = client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": pallet.number}).get_json()
    assert data["ok"] and data["count"] == 2
    assert {b.status for b in order.boxes[:2]} == {"loaded"}
    assert order.boxes[2].status == "received"


def test_dispatch_groups_ready_boxes_by_direction_fifo(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    _received_order(client, client_user, staff)
    _received_order(client, client_user, staff, marketplace="ozon", destination="Хоругвино", box_count="2")
    html = client.get("/mvb/dispatch").get_data(as_text=True)
    assert "Коледино" in html and "Хоругвино" in html
    assert html.index("Коледино") < html.index("Хоругвино")  # раньше принятые — выше


def test_driver_sees_assigned_and_unassigned_pickups_only(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    d1, d2 = _user("driver1", "mvb_driver"), _user("driver2", "mvb_driver")
    _login(client, client_user)
    mine = _confirmed_order(client)
    theirs = _confirmed_order(client)
    free = _confirmed_order(client)

    _login(client, staff)
    client.post(f"/mvb/orders/{mine.id}/driver", data={"driver_id": str(d1.id)})
    client.post(f"/mvb/orders/{theirs.id}/driver", data={"driver_id": str(d2.id)})

    _login(client, d1)
    client.get("/mvb/driver")  # первый показ забирает накопившиеся flash-сообщения
    html = client.get("/mvb/driver").get_data(as_text=True)
    assert mine.number in html and free.number in html and theirs.number not in html
    # водитель не может назначать
    client.post(f"/mvb/orders/{free.id}/driver", data={"driver_id": str(d1.id)})
    assert db.session.get(MvbOrder, free.id).driver_id is None


def test_self_delivery_pass_link_is_public(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    _login(client, client_user)
    order = _confirmed_order(client, delivery_method="self", pickup_address="")
    assert order.pass_token
    client.post(f"/mvb/orders/{order.id}/pass", data={
        "pass_driver_name": "Петров Петр", "pass_car_plate": "в777ор77", "pass_phone": "+7900",
    })
    client.post("/mvb/logout")
    g.pop("_login_user", None)
    html = client.get(f"/mvb/pass/{order.pass_token}").get_data(as_text=True)
    assert "Петров Петр" in html and "В777ОР77" in html and order.number in html
    assert client.get("/mvb/pass/wrong-token").status_code == 404


def test_vehicles_page_staff_only(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    client.post("/mvb/vehicles", data={"plate": "а001аа77", "capacity_boxes": "40"})
    assert MvbVehicle.query.one().plate == "А001АА77"
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    assert client.get("/mvb/vehicles").status_code == 302


def test_stage2_pages_render(db, client_logged_in):
    for url in ("/mvb/dispatch", "/mvb/trips", "/mvb/trips?status=all", "/mvb/pallets", "/mvb/vehicles"):
        assert client_logged_in.get(url).status_code == 200, url
    client_logged_in.post("/mvb/trips/new", data={"marketplace": "wb", "destination": "Коледино"})
    trip = MvbTrip.query.one()
    assert client_logged_in.get(f"/mvb/trips/{trip.id}").status_code == 200
