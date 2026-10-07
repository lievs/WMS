"""«Выполнение плана» на дашборде плана отгрузок (см. чат): колонка «Дата
плана» — дата, к которой нужно отгрузить план по конкретному городу,
проставляется вручную через календарь и хранится в
ShipmentPlanCityDeadline. Плюс разбивка вклада каждого склада-отправителя
(в штуках и процентах) в то, что уже уехало на этот город."""

from datetime import date, timedelta

from wms.extensions import db
from wms.models import (
    Box,
    BoxItem,
    MovementDocument,
    MovementLine,
    Nomenclature,
    ShipmentPlan,
    ShipmentPlanCityDeadline,
    ShipmentPlanLine,
    Warehouse,
)


def _setup(planned_qty=30):
    sender = Warehouse(code="WH-SPD1", name="Основной склад")
    city = Warehouse(code="WH-SPD2", name="ОЗОН: Город", marketplace="ozon", marketplace_city="Город")
    db.session.add_all([sender, city])
    db.session.commit()

    item = Nomenclature(sku="SKU-SPD1", barcode="7770005001", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()

    plan = ShipmentPlan(marketplace="ozon")
    db.session.add(plan)
    db.session.commit()
    line = ShipmentPlanLine(
        plan_id=plan.id, warehouse_id=city.id, nomenclature_id=item.id,
        barcode=item.barcode, article="ART-SPD1", planned_qty=planned_qty, fulfilled_qty=0,
    )
    db.session.add(line)
    db.session.commit()
    return plan, sender, city, item


def _ship_box(sender, city, item, qty, box_number, client):
    box = Box(box_number=box_number, warehouse_id=sender.id, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()

    doc = MovementDocument(number=f"PER-{box_number}", from_warehouse_id=sender.id, to_warehouse_id=city.id)
    db.session.add(doc)
    db.session.commit()
    db.session.add(
        MovementLine(document_id=doc.id, box_id=box.id, from_warehouse_id=box.warehouse_id, from_cell_id=box.cell_id)
    )
    db.session.commit()

    client.post(f"/movement/{doc.id}/complete")
    doc.marketplace_request_number = f"REQ-{box_number}"
    db.session.commit()
    client.post(f"/movement/{doc.id}/mark-marketplace-request")
    client.post(f"/movement/{doc.id}/mark-shipped")
    return doc


def test_set_deadline_date_shows_on_dashboard(db, client_logged_in):
    plan, sender, city, item = _setup()

    target = (date.today() + timedelta(days=5)).isoformat()
    resp = client_logged_in.post(
        f"/shipment-plan/{plan.id}/cities/{city.id}/deadline", data={"ship_by_date": target}
    )
    assert resp.status_code == 302

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)
    assert f'value="{target}"' in html

    deadline = ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first()
    assert deadline is not None
    assert deadline.ship_by_date.isoformat() == target


def test_update_deadline_date_overwrites_previous(db, client_logged_in):
    plan, sender, city, item = _setup()
    first = (date.today() + timedelta(days=3)).isoformat()
    second = (date.today() + timedelta(days=10)).isoformat()

    client_logged_in.post(f"/shipment-plan/{plan.id}/cities/{city.id}/deadline", data={"ship_by_date": first})
    client_logged_in.post(f"/shipment-plan/{plan.id}/cities/{city.id}/deadline", data={"ship_by_date": second})

    assert ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).count() == 1
    deadline = ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first()
    assert deadline.ship_by_date.isoformat() == second


def test_clearing_deadline_date_removes_it(db, client_logged_in):
    plan, sender, city, item = _setup()
    client_logged_in.post(
        f"/shipment-plan/{plan.id}/cities/{city.id}/deadline",
        data={"ship_by_date": (date.today() + timedelta(days=1)).isoformat()},
    )

    client_logged_in.post(f"/shipment-plan/{plan.id}/cities/{city.id}/deadline", data={"ship_by_date": ""})

    assert ShipmentPlanCityDeadline.query.filter_by(plan_id=plan.id, warehouse_id=city.id).first() is None


def test_sender_warehouse_breakdown_shows_qty_and_percent(db, client_logged_in):
    plan, sender1, city, item = _setup(planned_qty=100)
    sender2 = Warehouse(code="WH-SPD3", name="Склад №2 (Шоссейная 167)")
    db.session.add(sender2)
    db.session.commit()

    _ship_box(sender1, city, item, qty=30, box_number="BOX-SPD-S1", client=client_logged_in)
    _ship_box(sender2, city, item, qty=10, box_number="BOX-SPD-S2", client=client_logged_in)

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    city_idx = html.find("<td>Город</td>")
    snippet = html[city_idx : city_idx + 1500]
    assert "30" in snippet and "75%" in snippet  # 30 из 40 суммарно уехавших
    assert "10" in snippet and "25%" in snippet  # 10 из 40


def test_sender_breakdown_empty_when_nothing_shipped(db, client_logged_in):
    plan, sender, city, item = _setup()

    html = client_logged_in.get("/shipment-plan/").get_data(as_text=True)

    city_idx = html.find("<td>Город</td>")
    snippet = html[city_idx : city_idx + 1500]
    assert snippet.count(">—<") >= 2  # оба склада-отправителя без вклада
