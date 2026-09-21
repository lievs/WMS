from flask import g

from wms.extensions import db
from wms.models import User


def _make_user(username):
    user = User(username=username, role="warehouse")
    user.set_password("password")
    db.session.add(user)
    db.session.commit()
    return user


def test_admin_can_revoke_another_users_sessions(app, client_logged_in):
    user = _make_user("session-worker")
    other_device = app.test_client()
    g.pop("_login_user", None)
    response = other_device.post(
        "/login", data={"username": user.username, "password": "password"}
    )
    assert response.status_code == 302
    assert other_device.get("/").status_code == 200

    g.pop("_login_user", None)
    response = client_logged_in.post(f"/users/{user.id}/revoke-sessions")
    assert response.status_code == 302

    # db fixture держит один app context на весь тест; реальный сервер
    # очищает ORM-сессию после каждого запроса автоматически.
    db.session.expire_all()
    g.pop("_login_user", None)
    response = other_device.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_keeps_current_session_when_revoking_own_other_sessions(
    app, client_logged_in, admin_user
):
    second_device = app.test_client()
    with second_device.session_transaction() as sess:
        sess["_user_id"] = str(admin_user.id)
        sess["_fresh"] = True
        sess["session_version"] = admin_user.session_version or 0

    g.pop("_login_user", None)
    response = client_logged_in.post(f"/users/{admin_user.id}/revoke-sessions")
    assert response.status_code == 302
    g.pop("_login_user", None)
    assert client_logged_in.get("/").status_code == 200

    g.pop("_login_user", None)
    response = second_device.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
