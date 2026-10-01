"""«МВБ Логистика»: отдельный вход, заявки клиентов, индивидуальный штрихкод
на каждый короб и поштучные сканы (забор → склад МВБ → СЦ)."""

from flask import g

from wms.extensions import db
from datetime import datetime

from wms.models import (
    Box, MovementDocument, MovementLine, MvbBox, MvbClient, MvbOrder, MvbPallet, MvbPriceTier, MvbTrip,
    MvbVehicle,
    User, Warehouse,
)


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


def _new_trip(http, *directions):
    """Рейс-маршрут по направлениям [(маркетплейс, СЦ), ...] — как кнопка
    «Сформировать рейс» на странице «К отправке»."""
    http.post("/mvb/trips/new", data={"dir": [f"{m}|{d}" for m, d in directions]})
    return MvbTrip.query.order_by(MvbTrip.id.desc()).first()


def _deliver_all(http, trip):
    for stop in list(trip.stops):
        http.post(f"/mvb/trips/{trip.id}/stops/{stop.id}/deliver")


def _trip(http, marketplace="wb", destination="Коледино", driver=None, capacity=10):
    """Рейс (вызывать под пользователем склада): создан и авто назначено."""
    _new_trip(http, (marketplace, destination))
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
    _deliver_all(client, trip)

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

    trip = _new_trip(client, ("wb", "Коледино"))
    assert trip.status == "searching" and trip.planned_boxes == 3
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
    _deliver_all(client, trip)
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


def test_vehicles_page_staff_only(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    client.post("/mvb/vehicles", data={"plate": "а001аа77", "capacity_boxes": "40"})
    assert MvbVehicle.query.one().plate == "А001АА77"
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    assert client.get("/mvb/vehicles").status_code == 302


def test_stage2_pages_render(db, client_logged_in):
    for url in ("/mvb/dispatch", "/mvb/trips", "/mvb/trips?status=all", "/mvb/pallets", "/mvb/vehicles"):
        assert client_logged_in.get(url).status_code == 200, url
    trip = _new_trip(client_logged_in, ("wb", "Коледино"), ("ozon", "Хоругвино"))
    assert len(trip.stops) == 2
    assert client_logged_in.get(f"/mvb/trips/{trip.id}").status_code == 200


# ---------- этап 3: маршрут по точкам, лента водителя, короба из WMS ----------


def test_multi_stop_route_driver_delivers_each_point(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    driver = _user("driver1", "mvb_driver")
    wb = _received_order(client, client_user, staff, box_count="2")
    oz = _received_order(client, client_user, staff, marketplace="ozon", destination="Хоругвино", box_count="1")
    other = _received_order(client, client_user, staff, marketplace="wb", destination="Казань", box_count="1")

    trip = _new_trip(client, ("wb", "Коледино"), ("ozon", "Хоругвино"))
    assert [s.label() for s in trip.stops] == ["Wildberries · Коледино", "Ozon · Хоругвино"]
    vehicle = _vehicle(10, driver)
    client.post(f"/mvb/trips/{trip.id}/plan", data={"vehicle_id": str(vehicle.id)})
    for box in wb.boxes + oz.boxes:
        assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode}).get_json()["ok"]
    # направления нет в маршруте — отказ с подсказкой
    r = client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": other.boxes[0].barcode})
    assert r.status_code == 409 and "добавьте точку" in r.get_json()["message"]
    # точку можно добавить и тогда короб грузится; пустая точка убирается при отправке
    client.post(f"/mvb/trips/{trip.id}/stops", data={"marketplace": "wb", "destination": "Электросталь"})
    db.session.refresh(trip)
    assert len(trip.stops) == 3
    client.post(f"/mvb/trips/{trip.id}/depart")
    db.session.refresh(trip)
    assert len(trip.stops) == 2 and trip.status == "departed"

    first, second = trip.stops
    _login(client, driver)
    client.get("/mvb/driver")
    client.post(f"/mvb/trips/{trip.id}/stops/{first.id}/deliver")
    db.session.refresh(trip)
    assert trip.status == "departed"
    assert {b.status for b in wb.boxes} == {"delivered"} and oz.boxes[0].status == "shipped"
    client.post(f"/mvb/trips/{trip.id}/stops/{second.id}/deliver")
    db.session.refresh(trip)
    assert trip.status == "delivered" and oz.boxes[0].status == "delivered"


def test_stop_order_can_be_changed(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    trip = _new_trip(client, ("wb", "Коледино"), ("ozon", "Хоругвино"))
    second = trip.stops[1]
    client.post(f"/mvb/trips/{trip.id}/stops/{second.id}/up")
    db.session.refresh(trip)
    assert trip.stops[0].id == second.id


def test_driver_feed_shows_free_space_and_take(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    driver = _user("driver1", "mvb_driver")
    _vehicle(capacity=5, driver=driver)
    _login(client, client_user)
    small = _confirmed_order(client, box_count="2")
    big = _confirmed_order(client, box_count="8")

    _login(client, driver)
    client.get("/mvb/driver")
    html = client.get("/mvb/driver").get_data(as_text=True)
    assert "свободно <b>5</b>" in html
    assert "✓ помещается" in html and "✗ не помещается" in html
    assert "новая" in html

    client.post(f"/mvb/driver/orders/{small.id}/take")
    assert db.session.get(MvbOrder, small.id).driver_id == driver.id
    for box in small.boxes:
        data = client.post("/mvb/scan/pickup", data={"barcode": box.barcode}).get_json()
    assert data["warning"] == "В машине 2 из 5 кор."
    client.get("/mvb/driver")
    assert "свободно <b>3</b>" in client.get("/mvb/driver").get_data(as_text=True)
    assert big.number in html


def test_driver_scan_of_free_order_assigns_it(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    order = _confirmed_order(client)
    driver = _user("driver1", "mvb_driver")
    _login(client, driver)
    client.post("/mvb/scan/pickup", data={"barcode": order.boxes[0].barcode})
    assert db.session.get(MvbOrder, order.id).driver_id == driver.id


def _wms_movement(number="PER-000777", boxes=2, request_number="WB-1"):
    sender = Warehouse(code=f"S-{number}", name="Основной склад", address="Москва, Складская 5")
    dest = Warehouse(code=f"D-{number}", name="WB Коледино", marketplace="wb", marketplace_city="Коледино")
    db.session.add_all([sender, dest])
    db.session.flush()
    doc = MovementDocument(
        number=number, from_warehouse_id=sender.id, to_warehouse_id=dest.id, status="completed",
        completed_at=datetime.utcnow(),
        marketplace_request_number=request_number,
        marketplace_request_created_at=datetime.utcnow() if request_number else None,
    )
    db.session.add(doc)
    db.session.flush()
    wms_boxes = []
    for i in range(boxes):
        box = Box(box_number=f"BOX-{number[-3:]}{i:03d}", warehouse_id=sender.id)
        db.session.add(box)
        db.session.flush()
        db.session.add(MovementLine(document_id=doc.id, box_id=box.id))
        wms_boxes.append(box)
    db.session.commit()
    return doc, wms_boxes


def test_wms_movement_import_keeps_wms_barcodes(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    doc, wms_boxes = _wms_movement()
    assert doc.number in client.get("/mvb/wms").get_data(as_text=True)

    client.post(f"/mvb/wms/{doc.id}/import", data={"delivery_method": "self"})
    order = MvbOrder.query.filter_by(wms_movement_id=doc.id).one()
    assert order.client.is_internal and order.marketplace == "wb" and order.destination == "Коледино"
    assert [b.barcode for b in order.boxes] == [b.barcode_value for b in wms_boxes]
    # повторно не импортируется
    client.post(f"/mvb/wms/{doc.id}/import")
    assert MvbOrder.query.filter_by(wms_movement_id=doc.id).count() == 1
    # скан этикетки WMS (цифры) и ручной ввод номера BOX- находят короб МВБ
    assert client.post("/mvb/scan/receive", data={"barcode": wms_boxes[0].barcode_value}).get_json()["ok"]
    assert client.post("/mvb/scan/receive", data={"barcode": wms_boxes[1].box_number}).get_json()["ok"]


def test_receive_scan_of_wms_box_auto_imports_movement_and_trip_marks_wms_shipped(db, client):
    staff = _user("staff1", "mvb_staff")
    _login(client, staff)
    doc, wms_boxes = _wms_movement()

    data = client.post("/mvb/scan/receive", data={"barcode": wms_boxes[0].barcode_value}).get_json()
    assert data["ok"] and doc.number in data["message"]
    order = MvbOrder.query.filter_by(wms_movement_id=doc.id).one()
    client.post("/mvb/scan/receive", data={"barcode": wms_boxes[1].barcode_value})

    trip = _trip(client)
    for box in wms_boxes:
        assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode_value}).get_json()["ok"]
    client.post(f"/mvb/trips/{trip.id}/depart")
    assert db.session.get(MovementDocument, doc.id).shipped_at is not None
    assert {b.status for b in order.boxes} == {"shipped"}


def test_wms_shipped_not_set_without_marketplace_request_or_partial(db, client):
    _login(client, _user("staff1", "mvb_staff"))
    doc, wms_boxes = _wms_movement(request_number=None)
    client.post(f"/mvb/wms/{doc.id}/import")
    for box in wms_boxes:
        client.post("/mvb/scan/receive", data={"barcode": box.barcode_value})
    trip = _trip(client)
    for box in wms_boxes:
        client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode_value})
    client.post(f"/mvb/trips/{trip.id}/depart")
    assert db.session.get(MovementDocument, doc.id).shipped_at is None

    doc2, boxes2 = _wms_movement(number="PER-000888")
    client.post(f"/mvb/wms/{doc2.id}/import")
    for box in boxes2:
        client.post("/mvb/scan/receive", data={"barcode": box.barcode_value})
    trip2 = _trip(client)
    client.post(f"/mvb/trips/{trip2.id}/scan", data={"barcode": boxes2[0].barcode_value})
    client.post(f"/mvb/trips/{trip2.id}/depart")
    assert db.session.get(MovementDocument, doc2.id).shipped_at is None  # уехал не весь


# ---------- сквозная цепочка (как описал владелец) ----------


def test_full_chain_seller_to_sc(db, client):
    """Селлер создает заявку → оператор видит её и назначает водителя из
    списка → водитель по дороге видит другие заявки и берет ту, что влезает
    → короба сканируются при заборе → приемка на складе → программа считает
    машины и формирует рейсы по наполненности (статус «Поиск авто») →
    погрузка сканом каждого короба → водитель на точках отмечает
    «Сдано» / «Не сдано»."""
    seller = _user("seller", "mvb_client", _mvb_client("ИП Селлер"))
    seller2 = _user("seller2", "mvb_client", _mvb_client("ООО Второй"))
    operator = _user("operator", "mvb_staff")
    driver = _user("driver", "mvb_driver")
    _vehicle(capacity=6, driver=driver)

    # 1. селлеры создают заявки на забор
    _login(client, seller)
    first = _confirmed_order(client, box_count="3", destination="Коледино")
    _login(client, seller2)
    second = _confirmed_order(client, box_count="2", marketplace="ozon", destination="Хоругвино")
    too_big = _confirmed_order(client, box_count="9", destination="Коледино")

    # 2. оператор видит заявки без водителя и назначает водителя из списка
    _login(client, operator)
    client.get("/mvb/orders")
    html = client.get("/mvb/orders").get_data(as_text=True)
    assert "без водителя: <b>3</b>" in html and driver.display_name() in html
    client.post(f"/mvb/orders/{first.id}/driver", data={"driver_id": str(driver.id), "back": "orders"})
    assert db.session.get(MvbOrder, first.id).driver_id == driver.id

    # 3. водитель в пути: видит свою и чужие свободные заявки, что влезает
    _login(client, driver)
    client.get("/mvb/driver")
    feed = client.get("/mvb/driver").get_data(as_text=True)
    assert first.number in feed and second.number in feed and too_big.number in feed
    for box in first.boxes:
        client.post("/mvb/scan/pickup", data={"barcode": box.barcode})
    client.get("/mvb/driver")
    feed = client.get("/mvb/driver").get_data(as_text=True)
    assert "свободно <b>3</b>" in feed  # 3 из 6 заняты
    # вторая заявка (2 кор.) помещается — берет её по дороге
    client.post(f"/mvb/driver/orders/{second.id}/take")
    for box in second.boxes:
        data = client.post("/mvb/scan/pickup", data={"barcode": box.barcode}).get_json()
    assert data["warning"] == "В машине 5 из 6 кор."

    # 4. приемка на складе
    _login(client, operator)
    for box in first.boxes + second.boxes:
        assert client.post("/mvb/scan/receive", data={"barcode": box.barcode}).get_json()["ok"]

    # 5. программа считает машины и компонует рейсы по наполненности,
    # не разбивая заявки: 3 + 2 кор. в машины по 4 — две машины
    html = client.get("/mvb/dispatch?capacity=4").get_data(as_text=True)
    assert "нужно машин по 4 кор.: <b>2</b>" in html
    client.post("/mvb/trips/new", data={
        "dir": ["wb|Коледино", "ozon|Хоругвино"], "mode": "fill", "capacity": "4",
    })
    trips = MvbTrip.query.order_by(MvbTrip.id).all()
    assert len(trips) == 2 and {t.status for t in trips} == {"searching"}
    assert [t.planned_boxes for t in trips] == [3, 2]
    assert [[s.label() for s in t.stops] for t in trips] == [["Wildberries · Коледино"], ["Ozon · Хоругвино"]]
    assert db.session.get(MvbOrder, first.id).planned_trip_id == trips[0].id
    assert db.session.get(MvbOrder, second.id).planned_trip_id == trips[1].id
    assert "Поиск авто: 2" in client.get("/mvb/trips").get_data(as_text=True)

    # 6. авто найдено — наемный водитель без регистрации, погрузка сканом
    # каждого короба; водителю уходит ссылка
    links = []
    for trip, order in zip(trips, (first, second)):
        client.post(f"/mvb/trips/{trip.id}/plan", data={
            "car_plate": "в777ор77", "driver_name": "Случайный Водитель", "driver_phone": "+79990000000",
        })
        db.session.refresh(trip)
        assert trip.status == "assigned" and trip.car_plate == "В777ОР77" and trip.driver_id is None
        for box in order.boxes:
            assert client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode}).get_json()["ok"]
        client.post(f"/mvb/trips/{trip.id}/depart")
        links.append(f"/mvb/t/{trip.access_token}")

    # 7. водитель по ссылке (без входа): Коледино сдано, Хоругвино не сдано
    client.post("/mvb/logout")
    wb_trip, oz_trip = trips
    page = client.get(links[0]).get_data(as_text=True)
    assert wb_trip.number in page and "Сдано на СЦ" in page
    client.post(f"{links[0]}/stops/{wb_trip.stops[0].id}/deliver")
    client.post(f"{links[1]}/stops/{oz_trip.stops[0].id}/reject", data={"comment": "СЦ не принял: нет слота"})
    db.session.refresh(wb_trip)
    db.session.refresh(oz_trip)
    assert wb_trip.status == "delivered" and oz_trip.status == "delivered"
    assert {b.status for b in first.boxes} == {"delivered"}
    assert {b.status for b in second.boxes} == {"not_delivered"}
    oz_stop = oz_trip.stops[0]
    assert oz_stop.result == "rejected" and oz_stop.delivery_comment == "СЦ не принял: нет слота"

    # не сданный короб возвращается на склад и снова готов к отправке
    _login(client, operator)
    assert client.post("/mvb/scan/receive", data={"barcode": second.boxes[0].barcode}).get_json()["ok"]
    box = db.session.get(MvbBox, second.boxes[0].id)
    assert box.status == "received" and box.trip_id is None
    assert "Хоругвино" in client.get("/mvb/dispatch").get_data(as_text=True)


def test_driver_on_sc_trip_does_not_see_free_pickups(db, client):
    """Водителя можно назначить и на забор, и на рейс на СЦ; в рейсе на СЦ
    функция «забрать по дороге» ему не нужна."""
    staff = _user("staff1", "mvb_staff")
    driver = _user("driver1", "mvb_driver")
    client_user = _user("client1", "mvb_client", _mvb_client())
    received = _received_order(client, client_user, staff, box_count="1")
    _login(client, client_user)
    free = _confirmed_order(client, box_count="2")
    mine = _confirmed_order(client, box_count="1")

    _login(client, staff)
    client.post(f"/mvb/orders/{mine.id}/driver", data={"driver_id": str(driver.id)})
    trip = _trip(client, driver=driver)
    assert trip.driver_id == driver.id
    client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": received.boxes[0].barcode})
    client.post(f"/mvb/trips/{trip.id}/depart")

    _login(client, driver)
    client.get("/mvb/driver")
    html = client.get("/mvb/driver").get_data(as_text=True)
    assert trip.number in html and "Сдано на СЦ" in html
    assert mine.number in html and free.number not in html and "Заберу" not in html
    client.post(f"/mvb/driver/orders/{free.id}/take")
    assert db.session.get(MvbOrder, free.id).driver_id is None

    # рейс закрыт — снова видит свободные заявки и может взять
    _deliver_all(client, trip)
    client.get("/mvb/driver")
    html = client.get("/mvb/driver").get_data(as_text=True)
    assert free.number in html and "Заберу" in html
    client.post(f"/mvb/driver/orders/{free.id}/take")
    assert db.session.get(MvbOrder, free.id).driver_id == driver.id


def test_reject_requires_reason(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    order = _received_order(client, client_user, staff, box_count="1")
    trip = _trip(client)
    client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": order.boxes[0].barcode})
    client.post(f"/mvb/trips/{trip.id}/depart")
    db.session.refresh(trip)
    client.post(f"/mvb/trips/{trip.id}/stops/{trip.stops[0].id}/reject", data={"comment": ""})
    db.session.refresh(trip)
    assert trip.stops[0].result is None and order.boxes[0].status == "shipped"


def test_fill_mode_packs_whole_orders(db, client):
    """Компоновка рейсов не разбивает заявки: 3 + 3 + 2 кор. в машины по 5 —
    [3 + 2] и [3]; заявка больше машины едет отдельно целиком."""
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    a = _received_order(client, client_user, staff, box_count="3")
    b = _received_order(client, client_user, staff, box_count="3")
    c = _received_order(client, client_user, staff, marketplace="ozon", destination="Хоругвино", box_count="2")
    html = client.get("/mvb/dispatch?capacity=5").get_data(as_text=True)
    assert "нужно машин по 5 кор.: <b>2</b>" in html and "Предложение по рейсам" in html
    client.post("/mvb/trips/new", data={"dir": ["wb|Коледино", "ozon|Хоругвино"], "mode": "fill", "capacity": "5"})
    trips = MvbTrip.query.order_by(MvbTrip.id).all()
    assert [t.planned_boxes for t in trips] == [5, 3]
    assert [o.number for o in trips[0].planned_orders] == [a.number, c.number]
    assert [o.number for o in trips[1].planned_orders] == [b.number]

    # погрузка короба заявки из другого рейса — предупреждение
    client.post(f"/mvb/trips/{trips[1].id}/plan", data={"car_plate": "А1"})
    data = client.post(f"/mvb/trips/{trips[1].id}/scan", data={"barcode": a.boxes[0].barcode}).get_json()
    assert data["ok"] and trips[0].number in data["warning"]

    # отмена рейса освобождает заявки
    client.post(f"/mvb/trips/{trips[0].id}/cancel")
    assert db.session.get(MvbOrder, c.id).planned_trip_id is None


def test_order_bigger_than_truck_goes_whole(db, client):
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    _received_order(client, client_user, staff, box_count="7")
    client.post("/mvb/trips/new", data={"dir": ["wb|Коледино"], "mode": "fill", "capacity": "3"})
    trips = MvbTrip.query.order_by(MvbTrip.id).all()
    assert [t.planned_boxes for t in trips] == [7]


def test_sc_driver_link_without_login(db, client):
    """Наемный водитель на СЦ не регистрируется: по ссылке он отмечает подачу
    и итог на точках; по чужому/неверному токену — 404, служебное закрыто."""
    client_user = _user("client1", "mvb_client", _mvb_client())
    staff = _user("staff1", "mvb_staff")
    order = _received_order(client, client_user, staff, box_count="2")
    trip = _new_trip(client, ("wb", "Коледино"))
    client.post(f"/mvb/trips/{trip.id}/plan", data={"car_plate": "е555кх77", "capacity_boxes": "10"})
    db.session.refresh(trip)
    assert trip.status == "assigned" and trip.capacity() == 10
    detail = client.get(f"/mvb/trips/{trip.id}").get_data(as_text=True)
    link = f"/mvb/t/{trip.access_token}"
    assert link in detail and "wa.me" in detail

    client.post("/mvb/logout")
    assert client.get("/mvb/t/wrong-token").status_code == 404
    assert client.get(link).status_code == 200
    assert client.get(f"/mvb/trips/{trip.id}").status_code == 302  # служебное — только со входом
    client.post(f"{link}/arrive")
    assert db.session.get(MvbTrip, trip.id).status == "arrived"
    # до отправки «Сдано» не принимается
    client.post(f"{link}/stops/{trip.stops[0].id}/deliver")
    assert db.session.get(MvbTrip, trip.id).stops[0].result is None

    _login(client, staff)
    for box in order.boxes:
        client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode})
    client.post(f"/mvb/trips/{trip.id}/depart")
    client.post("/mvb/logout")
    client.post(f"{link}/stops/{trip.stops[0].id}/reject", data={"comment": ""})
    assert db.session.get(MvbTrip, trip.id).stops[0].result is None  # без причины нельзя
    client.post(f"{link}/stops/{trip.stops[0].id}/deliver")
    trip = db.session.get(MvbTrip, trip.id)
    assert trip.status == "delivered" and trip.stops[0].delivered_by_id is None
    assert {b.status for b in order.boxes} == {"delivered"}
    assert "Рейс завершен" in client.get(link).get_data(as_text=True)


# ---------- прайс, стоимость, отчет ----------


def _set_prices(http):
    for kind, min_boxes, price in [("pickup", 1, "50"), ("sc", 1, "120"), ("sc", 10, "100"), ("sc", 50, "80,5")]:
        http.post("/mvb/prices", data={"kind": kind, "min_boxes": str(min_boxes), "price_per_box": price})


def test_price_tiers_and_order_cost(db, client):
    staff = _user("staff1", "mvb_staff")
    client_user = _user("client1", "mvb_client", _mvb_client())
    _login(client, staff)
    _set_prices(client)
    assert MvbPriceTier.price_for("sc", 9) == 120
    assert MvbPriceTier.price_for("sc", 10) == 100
    assert MvbPriceTier.price_for("sc", 60) == 80.5
    # та же ступень второй раз не дублируется, а обновляется
    client.post("/mvb/prices", data={"kind": "sc", "min_boxes": "10", "price_per_box": "95"})
    assert MvbPriceTier.query.filter_by(kind="sc", min_boxes=10).one().price_per_box == 95
    assert "Отправка на СЦ" in client.get("/mvb/prices").get_data(as_text=True)

    _login(client, client_user)
    pickup = _confirmed_order(client, box_count="12")
    self_order = _confirmed_order(client, box_count="3", delivery_method="self", pickup_address="")
    db.session.refresh(pickup)
    assert pickup.pickup_cost == 600 and pickup.sc_cost == 1140 and pickup.total_cost == 1740
    db.session.refresh(self_order)
    assert self_order.pickup_cost is None and self_order.sc_cost == 360

    # клиент видит стоимость, но поправить не может
    assert "1740.00" in client.get(f"/mvb/orders/{pickup.id}").get_data(as_text=True)
    client.post(f"/mvb/orders/{pickup.id}/costs", data={"pickup_cost": "0", "sc_cost": "0"})
    assert db.session.get(MvbOrder, pickup.id).total_cost == 1740

    _login(client, staff)
    client.post(f"/mvb/orders/{pickup.id}/costs", data={"pickup_cost": "500", "sc_cost": "1 000,50"})
    order = db.session.get(MvbOrder, pickup.id)
    assert order.pickup_cost == 500 and order.sc_cost == 1000.5
    client.post(f"/mvb/orders/{pickup.id}/costs", data={"action": "recalc"})
    assert db.session.get(MvbOrder, pickup.id).sc_cost == 1140
    client.post(f"/mvb/orders/{pickup.id}/costs", data={"pickup_cost": "-5"})
    assert db.session.get(MvbOrder, pickup.id).pickup_cost == 600


def test_prices_page_staff_only(db, client):
    _login(client, _user("client1", "mvb_client", _mvb_client()))
    client.post("/mvb/prices", data={"kind": "sc", "min_boxes": "1", "price_per_box": "1"})
    assert MvbPriceTier.query.count() == 0
    assert client.get("/mvb/reports").status_code == 302


def test_client_report_for_period(db, client):
    staff = _user("staff1", "mvb_staff")
    a = _user("ca", "mvb_client", _mvb_client("Альфа"))
    b = _user("cb", "mvb_client", _mvb_client("Бета"))
    _login(client, staff)
    _set_prices(client)
    order_a = _received_order(client, a, staff, box_count="3")
    _received_order(client, b, staff, box_count="2", delivery_method="self", pickup_address="")

    trip = _trip(client)
    for box in order_a.boxes[:2]:
        client.post(f"/mvb/trips/{trip.id}/scan", data={"barcode": box.barcode})
    client.post(f"/mvb/trips/{trip.id}/depart")
    db.session.refresh(trip)
    client.post(f"/mvb/trips/{trip.id}/stops/{trip.stops[0].id}/deliver")

    today = datetime.utcnow().date().isoformat()
    html = client.get(f"/mvb/reports?date_from={today}&date_to={today}").get_data(as_text=True)
    assert "Альфа" in html and "Бета" in html
    from wms.blueprints.mvb import _report_rows

    rows, totals = _report_rows(datetime.utcnow().date(), datetime.utcnow().date())
    by_name = {r["client"].name: r for r in rows}
    assert by_name["Альфа"]["boxes"] == 3 and by_name["Альфа"]["shipped"] == 2 and by_name["Альфа"]["delivered"] == 2
    assert by_name["Альфа"]["total"] == 3 * 50 + 3 * 120
    assert by_name["Бета"]["shipped"] == 0 and by_name["Бета"]["pickup_cost"] == 0
    assert totals["boxes"] == 5

    xlsx = client.get(f"/mvb/reports.xlsx?date_from={today}&date_to={today}")
    assert xlsx.status_code == 200 and xlsx.data[:2] == b"PK"
    # другой период — пусто
    assert _report_rows(datetime(2020, 1, 1).date(), datetime(2020, 1, 31).date())[0] == []


# ---------- регистрация клиента ----------


def _register(http, **overrides):
    data = {
        "name": "ИП Новый", "inn": "7701234567", "contact_name": "Анна", "phone": "+7 900 123-45-67",
        "email": "a@example.com", "address": "Москва, ул. Ленина 1", "username": "newclient",
        "password": "secret1", "password2": "secret1",
    }
    data.update(overrides)
    return http.post("/mvb/register", data=data)


def test_client_self_registration_needs_operator_approval(db, client):
    assert client.get("/mvb/register").status_code == 200
    assert "Зарегистрироваться" in client.get("/mvb/login").get_data(as_text=True)
    response = _register(client)
    assert response.status_code == 302 and response.headers["Location"].endswith("/mvb/login")
    new = MvbClient.query.filter_by(name="ИП Новый").one()
    assert new.approval == "pending" and new.users[0].username == "newclient"

    # до подтверждения войти нельзя
    html = client.post("/mvb/login", data={"username": "newclient", "password": "secret1"}).get_data(as_text=True)
    assert "на проверке" in html
    assert client.get("/mvb/orders").status_code == 302

    # оператор видит новую регистрацию и подтверждает
    staff = _user("staff1", "mvb_staff")
    _login(client, staff)
    client.get("/mvb/registrations")
    page = client.get("/mvb/registrations").get_data(as_text=True)
    assert "ИП Новый" in page and "7701234567" in page
    assert "Новые клиенты <span" in page
    client.post(f"/mvb/registrations/{new.id}/approve")
    assert db.session.get(MvbClient, new.id).approval == "approved"

    client.post("/mvb/logout")
    response = client.post("/mvb/login", data={"username": "newclient", "password": "secret1"})
    assert response.status_code == 302
    form = client.get("/mvb/orders/new").get_data(as_text=True)
    assert "Москва, ул. Ленина 1" in form  # адрес из регистрации подставляется в заявку


def test_registration_validation_and_reject(db, client):
    _user("taken", "mvb_client", _mvb_client())
    for overrides, message in [
        ({"inn": "123"}, "ИНН"),
        ({"username": "Taken"}, "логин уже занят"),
        ({"password2": "other"}, "Пароли не совпадают"),
        ({"phone": "12"}, "телефон"),
    ]:
        html = _register(client, **overrides).get_data(as_text=True)
        assert message in html
    assert MvbClient.query.filter_by(name="ИП Новый").count() == 0

    _register(client)
    new = MvbClient.query.filter_by(name="ИП Новый").one()
    staff = _user("staff1", "mvb_staff")
    _login(client, staff)
    client.post(f"/mvb/registrations/{new.id}/reject")
    client.post("/mvb/logout")
    html = client.post("/mvb/login", data={"username": "newclient", "password": "secret1"}).get_data(as_text=True)
    assert "отклонена" in html

    # клиент не может подтверждать регистрации
    other = _user("client2", "mvb_client", _mvb_client("Другой"))
    _login(client, other)
    client.post(f"/mvb/registrations/{new.id}/approve")
    assert db.session.get(MvbClient, new.id).approval == "rejected"
