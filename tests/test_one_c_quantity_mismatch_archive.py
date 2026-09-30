"""Архив расхождений "WMS и 1С" (см. чат): расхождение по документу,
который нельзя исправить (например, тестовый документ, реально никогда не
будет пересверен с 1С), администратор переносит в архив — оно перестает
показываться в основном списке /reports/one-c-quantity-mismatches."""

import pytest

from wms.extensions import db
from wms.models import OneCQuantityCheck


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
