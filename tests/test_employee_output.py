"""Дашборд «Выработка сотрудников»: журнал упаковки/сборки (WorkEvent),
планы (WorkPlan) и страница reports.employee_output."""

from datetime import date, datetime

from wms.extensions import db
from wms.models import (
    Box, BoxItem, MovementDocument, MovementLine, Nomenclature, PlacementDocument,
    UnplacedStock, User, Warehouse, WorkEvent, WorkPlan,
)
from wms.utils.work_log import work_day


def _warehouses():
    sender = Warehouse(code="WH-EO1", name="Основной")
    dest = Warehouse(code="WH-EO2", name="Склад МП", marketplace="wb", marketplace_city="Коледино")
    db.session.add_all([sender, dest])
    db.session.commit()
    return sender, dest


def _item(suffix="1"):
    item = Nomenclature(sku=f"EO-{suffix}", barcode=f"777000{suffix}", name=f"Шапка {suffix}", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def test_packing_into_box_via_placement_is_logged_per_user_and_day(db, client_logged_in, admin_user):
    sender, _ = _warehouses()
    item = _item()
    UnplacedStock.add(sender.id, item.id, 50)
    doc = PlacementDocument(number="PL-EO1", warehouse_id=sender.id)
    box = Box(box_number="BOX-EO-1", warehouse_id=sender.id, status="open")
    db.session.add_all([doc, box])
    db.session.commit()

    for qty in (10, 5):
        resp = client_logged_in.post(
            f"/placement/{doc.id}/boxes/{box.id}/items/add-by-barcode",
            json={"barcode": item.barcode, "qty": qty},
        )
        assert resp.status_code == 200

    events = WorkEvent.query.filter_by(kind="pack").all()
    assert [(e.user_id, e.work_date, e.qty, e.box_id) for e in events] == [
        (admin_user.id, work_day(), 10, box.id), (admin_user.id, work_day(), 5, box.id),
    ]


def test_picking_box_into_movement_is_logged_and_dropped_on_removal(db, client_logged_in, admin_user):
    sender, dest = _warehouses()
    item = _item()
    box = Box(box_number="BOX-EO-2", warehouse_id=sender.id, status="open")
    doc = MovementDocument(number="PER-EO1", from_warehouse_id=sender.id, to_warehouse_id=dest.id)
    db.session.add_all([box, doc])
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=40))
    db.session.commit()

    client_logged_in.post(f"/movement/{doc.id}/boxes/add", data={"box_number": box.box_number})
    picks = WorkEvent.query.filter_by(kind="pick").all()
    assert len(picks) == 1 and picks[0].qty == 40 and picks[0].ref_id == doc.id

    line = MovementLine.query.filter_by(document_id=doc.id).first()
    client_logged_in.post(f"/movement/{doc.id}/lines/{line.id}/delete")
    assert WorkEvent.query.filter_by(kind="pick").count() == 0


def test_dashboard_aggregates_facts_and_applies_plans(db, client_logged_in, admin_user):
    today = work_day()
    worker = User(username="packer1", full_name="Иванова Анна", role="user")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    for box_id, qty in ((1, 30), (1, 20), (2, 50)):
        db.session.add(WorkEvent(user_id=worker.id, work_date=today, kind="pack", box_id=box_id, qty=qty))
    db.session.add(WorkEvent(user_id=worker.id, work_date=today, kind="pick", box_id=9, qty=12))
    db.session.add(WorkPlan(user_id=worker.id, kind="pack", plan_date=None, qty_plan=200, boxes_plan=4))
    db.session.commit()

    html = client_logged_in.get("/reports/employee-output").get_data(as_text=True)
    assert "Иванова Анна" in html
    assert ">100<" in html          # 30+20+50 шт
    assert "50%" in html            # 100 из плана 200 (и 2 из 4 коробов)

    # план на конкретную дату перекрывает план по умолчанию
    client_logged_in.post("/reports/employee-output/plans", data={
        f"day__pack__{worker.id}__{today.isoformat()}__qty": "100",
        f"day__pack__{worker.id}__{today.isoformat()}__boxes": "",
        f"def__pick__{worker.id}__boxes": "3",
    })
    html = client_logged_in.get("/reports/employee-output").get_data(as_text=True)
    assert "100%" in html
    override = WorkPlan.query.filter_by(user_id=worker.id, kind="pack", plan_date=today).first()
    assert override.qty_plan == 100 and override.boxes_plan is None
    assert WorkPlan.query.filter_by(user_id=worker.id, kind="pick", plan_date=None).first().boxes_plan == 3


def test_blank_plan_removes_it_and_only_admin_can_edit(db, client_logged_in, admin_user):
    worker = User(username="picker1", full_name="Петров", role="user")
    worker.set_password("x")
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkPlan(user_id=worker.id, kind="pick", plan_date=None, boxes_plan=5))
    db.session.commit()

    client_logged_in.post("/reports/employee-output/plans", data={f"def__pick__{worker.id}__boxes": ""})
    assert WorkPlan.query.count() == 0


def test_non_admin_cannot_change_plans(db, client):
    worker = User(username="viewer1", full_name="Смотрящий", role="user")
    worker.set_password("pw12345")
    db.session.add(worker)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(worker.id)
        sess["_fresh"] = True
    resp = client.post("/reports/employee-output/plans", data={f"def__pick__{worker.id}__boxes": "9"})
    assert resp.status_code == 302
    assert WorkPlan.query.count() == 0


def test_dashboard_date_filter_and_user_filter(db, client_logged_in, admin_user):
    a = User(username="ua", full_name="Анна", role="user"); a.set_password("x")
    b = User(username="ub", full_name="Борис", role="user"); b.set_password("x")
    db.session.add_all([a, b])
    db.session.commit()
    day = date(2026, 9, 1)
    db.session.add_all([
        WorkEvent(user_id=a.id, work_date=day, kind="pack", box_id=1, qty=7),
        WorkEvent(user_id=b.id, work_date=day, kind="pack", box_id=2, qty=9),
    ])
    db.session.commit()
    html = client_logged_in.get("/reports/employee-output?date_from=2026-09-01&date_to=2026-09-02").get_data(as_text=True)
    assert "Анна" in html and "Борис" in html
    html = client_logged_in.get(f"/reports/employee-output?date_from=2026-09-01&date_to=2026-09-02&user_id={a.id}").get_data(as_text=True)
    assert "01.09.2026" in html and ">7<" in html and ">9<" not in html
    html = client_logged_in.get("/reports/employee-output?date_from=2026-09-05&date_to=2026-09-06").get_data(as_text=True)
    assert "Нет данных за выбранный период" in html
