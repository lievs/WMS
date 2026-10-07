"""Доп. штрихкод (Nomenclature.barcode2, см. чат): второй код, по которому
тот же товар находится наравне с основным barcode — везде, где товар ищут
по штрихкоду (сканер), не только в текстовом поиске по номенклатуре."""

from wms.extensions import db
from wms.models import Nomenclature


def _make_item(barcode, barcode2=None, sku=None, name="Товар"):
    item = Nomenclature(
        sku=sku or barcode, barcode=barcode, barcode2=barcode2, name=name, unit="шт",
    )
    db.session.add(item)
    db.session.commit()
    return item


def test_find_by_barcode_matches_primary_barcode(db):
    item = _make_item("1110000000001")
    assert Nomenclature.find_by_barcode("1110000000001").id == item.id


def test_find_by_barcode_matches_secondary_barcode(db):
    item = _make_item("1110000000002", barcode2="2220000000002")
    assert Nomenclature.find_by_barcode("2220000000002").id == item.id


def test_find_by_barcode_returns_none_for_unknown_code(db):
    assert Nomenclature.find_by_barcode("no-such-code") is None


def test_find_by_barcode_returns_none_for_empty_code(db):
    assert Nomenclature.find_by_barcode("") is None
    assert Nomenclature.find_by_barcode(None) is None


def test_create_nomenclature_accepts_barcode2(db, client_logged_in):
    client_logged_in.post(
        "/nomenclature/create",
        data={"barcode": "3330000000001", "barcode2": "4440000000001", "name": "Товар с доп. штрихкодом"},
    )

    item = Nomenclature.query.filter_by(barcode="3330000000001").first()
    assert item is not None
    assert item.barcode2 == "4440000000001"


def test_create_nomenclature_rejects_barcode2_colliding_with_existing_barcode(db, client_logged_in):
    _make_item("5550000000001")

    resp = client_logged_in.post(
        "/nomenclature/create",
        data={"barcode": "6660000000001", "barcode2": "5550000000001", "name": "Товар 2"},
        follow_redirects=True,
    )

    assert "уже используется" in resp.get_data(as_text=True)
    assert Nomenclature.query.filter_by(barcode="6660000000001").first() is None


def test_update_barcode2_sets_value(db, client_logged_in):
    item = _make_item("7770000000001")

    client_logged_in.post(f"/nomenclature/{item.id}/barcode2", data={"barcode2": "8880000000001"})

    assert Nomenclature.query.get(item.id).barcode2 == "8880000000001"


def test_update_barcode2_clears_value_when_empty(db, client_logged_in):
    item = _make_item("7770000000002", barcode2="8880000000002")

    client_logged_in.post(f"/nomenclature/{item.id}/barcode2", data={"barcode2": "  "})

    assert Nomenclature.query.get(item.id).barcode2 is None


def test_update_barcode2_rejects_duplicate_against_another_barcode(db, client_logged_in):
    item1 = _make_item("9990000000001")
    item2 = _make_item("9990000000002")

    resp = client_logged_in.post(
        f"/nomenclature/{item2.id}/barcode2", data={"barcode2": "9990000000001"}, follow_redirects=True
    )

    assert "уже используется" in resp.get_data(as_text=True)
    assert Nomenclature.query.get(item2.id).barcode2 is None


def test_update_barcode2_blocked_without_permission(db, client):
    from tests.test_nomenclature_edit_permission import _login_as, _make_staff_user

    item = _make_item("1230000000001")
    user = _make_staff_user(nomenclature_edit_allowed=False)
    _login_as(client, user)

    client.post(f"/nomenclature/{item.id}/barcode2", data={"barcode2": "4560000000001"})

    assert Nomenclature.query.get(item.id).barcode2 is None


def test_list_search_finds_item_by_barcode2(db, client_logged_in):
    _make_item("1110000000010", barcode2="2220000000010", name="Уникальное имя для поиска")

    html = client_logged_in.get("/nomenclature/?q=2220000000010").get_data(as_text=True)

    assert "Уникальное имя для поиска" in html


def test_autocomplete_api_finds_item_by_barcode2(db, client_logged_in):
    _make_item("1110000000020", barcode2="2220000000020", name="Товар для автокомплита")

    resp = client_logged_in.get("/api/nomenclature/search?q=2220000000020")

    data = resp.get_json()
    assert any(row["barcode"] == "1110000000020" for row in data)


def test_by_barcode_api_finds_item_by_barcode2(db, client_logged_in):
    _make_item("1110000000030", barcode2="2220000000030")

    resp = client_logged_in.get("/api/nomenclature/by-barcode/2220000000030")

    data = resp.get_json()
    assert data["found"] is True
    assert data["barcode"] == "1110000000030"


def test_locate_finds_item_by_barcode2(db, client_logged_in):
    item = _make_item("1110000000040", barcode2="2220000000040", name="Товар для locate")

    html = client_logged_in.get("/nomenclature/locate?barcode=2220000000040").get_data(as_text=True)

    assert "Товар для locate" in html


def test_manual_add_form_has_no_description_or_article_fields(db, client_logged_in):
    """Форма "Добавить товар вручную" упрощена (см. чат): описание и
    артикул убраны из формы создания (артикул по-прежнему можно поправить
    позже прямо в списке через update_sku, описание нигде больше не
    используется) — вырезаем именно фрагмент формы создания, а не всю
    страницу, чтобы не зацепить поле "Артикул" из строк списка ниже."""
    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)
    form_start = html.index("Добавить товар вручную")
    form_end = html.index("</form>", form_start)
    form_html = html[form_start:form_end]

    assert 'name="description"' not in form_html
    assert 'name="sku"' not in form_html


def test_manual_add_form_has_barcode2_field(db, client_logged_in):
    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)
    assert 'name="barcode2"' in html
