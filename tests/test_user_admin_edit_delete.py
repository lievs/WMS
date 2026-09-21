"""Администратор может сменить логин и пароль пользователя, а также
удалить пользователя целиком (см. auth.update_username, auth.set_password,
auth.delete_user)."""

from wms.extensions import db
from wms.models import User


def _make_user(username, suffix=""):
    user = User(username=username, full_name=f"Складской{suffix}", role="warehouse")
    user.set_password("password")
    db.session.add(user)
    db.session.commit()
    return user


def test_admin_can_change_username(db, client_logged_in):
    user = _make_user("old-login")

    resp = client_logged_in.post(
        f"/users/{user.id}/username", data={"username": "new-login"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert User.query.get(user.id).username == "new-login"


def test_username_change_rejects_duplicate(db, client_logged_in):
    user1 = _make_user("worker-a")
    user2 = _make_user("worker-b")

    resp = client_logged_in.post(
        f"/users/{user2.id}/username", data={"username": "worker-a"}, follow_redirects=True
    )

    assert "уже занят" in resp.get_data(as_text=True)
    assert User.query.get(user2.id).username == "worker-b"
    assert User.query.get(user1.id).username == "worker-a"


def test_username_change_rejects_empty(db, client_logged_in):
    user = _make_user("worker-c")

    resp = client_logged_in.post(f"/users/{user.id}/username", data={"username": "  "}, follow_redirects=True)

    assert "Укажите логин" in resp.get_data(as_text=True)
    assert User.query.get(user.id).username == "worker-c"


def test_admin_can_set_explicit_password(db, client_logged_in):
    user = _make_user("worker-d")

    resp = client_logged_in.post(
        f"/users/{user.id}/set-password", data={"password": "hunter22"}, follow_redirects=True
    )

    assert resp.status_code == 200
    user = User.query.get(user.id)
    assert user.check_password("hunter22")
    assert not user.check_password("password")


def test_set_password_rejects_too_short(db, client_logged_in):
    user = _make_user("worker-e")

    resp = client_logged_in.post(f"/users/{user.id}/set-password", data={"password": "abc"}, follow_redirects=True)

    assert "слишком короткий" in resp.get_data(as_text=True)
    user = User.query.get(user.id)
    assert user.check_password("password")


def test_admin_can_delete_user(db, client_logged_in):
    user = _make_user("worker-f")

    resp = client_logged_in.post(f"/users/{user.id}/delete", follow_redirects=True)

    assert resp.status_code == 200
    assert User.query.get(user.id) is None


def test_admin_cannot_delete_self(db, client_logged_in, admin_user):
    resp = client_logged_in.post(f"/users/{admin_user.id}/delete", follow_redirects=True)

    assert "Нельзя удалить самого себя" in resp.get_data(as_text=True)
    assert User.query.get(admin_user.id) is not None


def test_non_admin_cannot_edit_or_delete_users(db, client):
    admin_target = _make_user("worker-g")
    staffer = User(username="staffer-noadmin", role="warehouse")
    staffer.set_password("x")
    db.session.add(staffer)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(staffer.id)
        sess["_fresh"] = True

    client.post(f"/users/{admin_target.id}/username", data={"username": "hacked"})
    client.post(f"/users/{admin_target.id}/set-password", data={"password": "hacked123"})
    client.post(f"/users/{admin_target.id}/delete")

    admin_target = User.query.get(admin_target.id)
    assert admin_target is not None
    assert admin_target.username == "worker-g"
    assert admin_target.check_password("password")
