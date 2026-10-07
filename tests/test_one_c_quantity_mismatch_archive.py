"""Архив расхождений "WMS и 1С" (см. чат): расхождение по документу,
который нельзя исправить (например, тестовый документ, реально никогда не
будет пересверен с 1С), администратор переносит в архив — оно перестает
показываться в основном списке /reports/one-c-quantity-mismatches."""

import pytest

from wms.extensions import db
from wms.models import AppSetting, OneCQuantityCheck


@pytest.fixture(autouse=True)
def _unhide_management_dashboard(monkeypatch):
    """Раздел временно скрыт в проде (см. test_management_dashboard.py) —
    только для теста алерта на дашборде руководителя, остальным тестам
    файла этот тумблер не мешает."""
    monkeypatch.setattr("wms.blueprints.management.HIDDEN_WORK_IN_PROGRESS", False)


def _make_check(document_number="TEST-1", dismissed_at=None):
    row = OneCQuantityCheck(
        document_type="movement",
        document_id=1,
        document_number=document_number,
        barcode="1234567890123",
        item_name="Тестовый товар",
        wms_qty=5,
        one_c_qty=3,
        dismissed_at=dismissed_at,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_active_list_hides_dismissed_rows(db, client_logged_in):
    _make_check("ACTIVE-1")
    from datetime import datetime
    _make_check("ARCHIVED-1", dismissed_at=datetime.utcnow())

    html = client_logged_in.get("/reports/one-c-quantity-mismatches").get_data(as_text=True)

    assert "ACTIVE-1" in html
    assert "ARCHIVED-1" not in html


def test_archived_view_shows_only_dismissed_rows(db, client_logged_in):
    _make_check("ACTIVE-2")
    from datetime import datetime
    _make_check("ARCHIVED-2", dismissed_at=datetime.utcnow())

    html = client_logged_in.get("/reports/one-c-quantity-mismatches?archived=1").get_data(as_text=True)

    assert "ARCHIVED-2" in html
    assert "ACTIVE-2" not in html


def test_admin_can_dismiss_row(db, client_logged_in):
    row = _make_check("TO-ARCHIVE-1")

    resp = client_logged_in.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/dismiss", follow_redirects=True
    )

    assert "перенесено в архив" in resp.get_data(as_text=True)
    assert OneCQuantityCheck.query.get(row.id).dismissed_at is not None


def test_admin_can_restore_row(db, client_logged_in):
    from datetime import datetime
    row = _make_check("TO-RESTORE-1", dismissed_at=datetime.utcnow())

    resp = client_logged_in.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/restore", follow_redirects=True
    )

    assert "возвращено из архива" in resp.get_data(as_text=True)
    assert OneCQuantityCheck.query.get(row.id).dismissed_at is None


def test_dismiss_requires_admin(db, client):
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    row = _make_check("NO-PERM-1")
    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    resp = client.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/dismiss", follow_redirects=True
    )

    assert "может только администратор" in resp.get_data(as_text=True)
    assert OneCQuantityCheck.query.get(row.id).dismissed_at is None


def test_restore_requires_admin(db, client):
    from datetime import datetime
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    row = _make_check("NO-PERM-2", dismissed_at=datetime.utcnow())
    user = _make_staff_user(nomenclature_edit_allowed=True)
    _login_as(client, user)

    resp = client.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/restore", follow_redirects=True
    )

    assert "может только администратор" in resp.get_data(as_text=True)
    assert OneCQuantityCheck.query.get(row.id).dismissed_at is not None


def test_dashboard_alert_count_excludes_dismissed(db, client_logged_in):
    from datetime import datetime
    _make_check("COUNT-ACTIVE-1")
    _make_check("COUNT-ARCHIVED-1", dismissed_at=datetime.utcnow())

    html = client_logged_in.get("/management/").get_data(as_text=True)

    assert "1С: 1</span>" in html


def _sync_quantity_check(client, document_id=42, wms_qty=10, one_c_qty=9):
    """Эмулирует callback 1С (см. integration_1c.confirm_documents) —
    ровно то, что происходит при каждой обычной синхронизации."""
    payload = {
        "quantity_checks": [{
            "document_type": "movement", "document_id": document_id,
            "document_number": f"PER-{document_id}", "barcode": "123", "name": "Товар",
            "wms_qty": wms_qty, "one_c_qty": one_c_qty,
        }]
    }
    return client.post(
        "/integrations/1c/api/export/confirm", json=payload,
        headers={"X-1C-Token": "test-token"},
    )


def test_dismissed_document_stays_archived_after_resync(db, client_logged_in):
    """Баг из чата: расхождение по тестовому документу, перенесенное в
    архив, при каждой следующей сверке с 1С полностью пересоздавалось
    заново (см. confirm_documents — старые строки документа удаляются, а
    новые создаются "с нуля") и снова вылезало в основном списке. Теперь
    архивная отметка должна переноситься на новую строку того же
    документа, пока ее не снимут вручную."""
    db.session.add(AppSetting(key="api_1c_token", value="test-token"))
    db.session.commit()

    _sync_quantity_check(client_logged_in, document_id=101)
    row = OneCQuantityCheck.query.filter_by(document_id=101).one()
    client_logged_in.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/dismiss", follow_redirects=True
    )
    assert OneCQuantityCheck.query.get(row.id).dismissed_at is not None

    # Повторная (и еще одна) синхронизация с тем же расхождением — как при
    # обычной регулярной сверке 1С, документ так и не исправили.
    _sync_quantity_check(client_logged_in, document_id=101)
    _sync_quantity_check(client_logged_in, document_id=101)

    row_after_resync = OneCQuantityCheck.query.filter_by(document_id=101).one()
    assert row_after_resync.dismissed_at is not None

    html = client_logged_in.get("/reports/one-c-quantity-mismatches").get_data(as_text=True)
    assert "PER-101" not in html
    archived_html = client_logged_in.get(
        "/reports/one-c-quantity-mismatches?archived=1"
    ).get_data(as_text=True)
    assert "PER-101" in archived_html


def test_restored_document_reappears_after_resync(db, client_logged_in):
    """После ручного возврата из архива документ снова ведет себя как
    обычный (пока не исправленный) — очередная сверка с тем же
    расхождением должна показать его в основном списке, а не молчать."""
    db.session.add(AppSetting(key="api_1c_token", value="test-token"))
    db.session.commit()

    _sync_quantity_check(client_logged_in, document_id=102)
    row = OneCQuantityCheck.query.filter_by(document_id=102).one()
    client_logged_in.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/dismiss", follow_redirects=True
    )
    client_logged_in.post(
        f"/reports/one-c-quantity-mismatches/{row.id}/restore", follow_redirects=True
    )

    _sync_quantity_check(client_logged_in, document_id=102)

    row_after_resync = OneCQuantityCheck.query.filter_by(document_id=102).one()
    assert row_after_resync.dismissed_at is None
    html = client_logged_in.get("/reports/one-c-quantity-mismatches").get_data(as_text=True)
    assert "PER-102" in html
