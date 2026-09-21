"""Подсказка ячейки (placement.suggest_cell) для "ходового" товара —
коробов с ним на складе больше, чем вмещает одна ячейка (CELL_CAPACITY).
Для такого товара система заводит под него отдельную (пустую) ячейку и
старается не смешивать ее с другим товаром, пока он не закончится (не
останется неразмещенных коробов) — после этого ячейка снова доступна для
любого товара наравне с остальными."""

from wms.blueprints.placement import suggest_cell
from wms.extensions import db
from wms.models import CELL_CAPACITY, Box, BoxItem, Cell, Nomenclature, Warehouse, Zone


def _make_warehouse(suffix):
    warehouse = Warehouse(code=f"WH-BULK{suffix}", name="Склад")
    db.session.add(warehouse)
    db.session.commit()
    return warehouse


def _make_item(suffix):
    item = Nomenclature(sku=f"SKU-BULK{suffix}", barcode=f"77703000{suffix}", name="Товар", unit="шт")
    db.session.add(item)
    db.session.commit()
    return item


def _make_cell(warehouse, code, zone=None):
    cell = Cell(warehouse_id=warehouse.id, code=code, zone_id=zone.id if zone else None)
    db.session.add(cell)
    db.session.commit()
    return cell


def _make_box(warehouse, item, box_number, cell=None, qty=1):
    box = Box(box_number=box_number, warehouse_id=warehouse.id, cell_id=cell.id if cell else None, status="open")
    db.session.add(box)
    db.session.commit()
    db.session.add(BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty))
    db.session.commit()
    return box


def test_bulk_item_prefers_empty_cell_over_cell_with_other_items(db):
    warehouse = _make_warehouse("1")
    bulk_item = _make_item("1")
    other_item = _make_item("1b")

    # Много коробов bulk_item на складе (больше, чем вмещает одна ячейка) —
    # часть уже размещена (не важно куда), часть еще нет.
    mixed_cell = _make_cell(warehouse, "A-01")
    empty_cell = _make_cell(warehouse, "A-02")
    _make_box(warehouse, other_item, "BOX-BULK101", cell=mixed_cell)

    for i in range(CELL_CAPACITY + 1):
        _make_box(warehouse, bulk_item, f"BOX-BULK-STOCK-{i}")

    target_box = _make_box(warehouse, bulk_item, "BOX-BULK-TARGET")

    suggestion = suggest_cell(warehouse.id, target_box)

    assert suggestion is not None
    assert suggestion["cell"].id == empty_cell.id
    assert "отдельную ячейку" in suggestion["reason"]


def test_non_bulk_item_avoids_cell_reserved_for_bulk_item_with_remaining_stock(db):
    warehouse = _make_warehouse("2")
    bulk_item = _make_item("2")
    other_item = _make_item("2b")

    reserved_cell = _make_cell(warehouse, "A-01")
    empty_cell = _make_cell(warehouse, "A-02")
    _make_box(warehouse, bulk_item, "BOX-BULK201", cell=reserved_cell)

    # Еще много неразмещенных коробов bulk_item — значит товар не закончился.
    for i in range(CELL_CAPACITY):
        _make_box(warehouse, bulk_item, f"BOX-BULK-STOCK2-{i}")

    target_box = _make_box(warehouse, other_item, "BOX-BULK-TARGET2")

    suggestion = suggest_cell(warehouse.id, target_box)

    assert suggestion is not None
    assert suggestion["cell"].id == empty_cell.id


def test_cell_becomes_available_once_bulk_item_is_exhausted(db):
    warehouse = _make_warehouse("3")
    bulk_item = _make_item("3")
    other_item = _make_item("3b")

    dedicated_cell = _make_cell(warehouse, "A-01")
    _make_cell(warehouse, "A-02")
    _make_box(warehouse, bulk_item, "BOX-BULK301", cell=dedicated_cell)
    # Никаких неразмещенных коробов bulk_item больше нет — товар закончился.

    target_box = _make_box(warehouse, other_item, "BOX-BULK-TARGET3")

    suggestion = suggest_cell(warehouse.id, target_box)

    assert suggestion is not None
    assert suggestion["cell"].id == dedicated_cell.id
    assert "частично заполнена" in suggestion["reason"]


def test_direct_match_still_wins_even_for_bulk_item(db):
    warehouse = _make_warehouse("4")
    bulk_item = _make_item("4")

    started_cell = _make_cell(warehouse, "A-01")
    _make_cell(warehouse, "A-02")
    _make_box(warehouse, bulk_item, "BOX-BULK401", cell=started_cell)

    for i in range(CELL_CAPACITY):
        _make_box(warehouse, bulk_item, f"BOX-BULK-STOCK4-{i}")

    target_box = _make_box(warehouse, bulk_item, "BOX-BULK-TARGET4")

    suggestion = suggest_cell(warehouse.id, target_box)

    assert suggestion is not None
    assert suggestion["cell"].id == started_cell.id
    assert "уже есть такой же товар" in suggestion["reason"]


def test_bulk_empty_cell_reason_wins_over_row_match_phrasing(db):
    """Регрессия: пустая ячейка в том же ряду, что и заполненная под
    завязку ячейка с этим же ходовым товаром, выбирается из-за bulk-логики
    (см. mix_penalty) — причина должна отражать именно это, а не
    формально верное, но неинформативное "товар уже есть в этом ряду"."""
    warehouse = _make_warehouse("5")
    bulk_item = _make_item("5")
    other_item = _make_item("5b")
    zone = Zone(warehouse_id=warehouse.id, code="A")
    db.session.add(zone)
    db.session.commit()

    full_cell = _make_cell(warehouse, "A-01", zone=zone)
    empty_cell = _make_cell(warehouse, "A-02", zone=zone)
    mixed_cell = _make_cell(warehouse, "A-03", zone=zone)

    for i in range(CELL_CAPACITY):
        _make_box(warehouse, bulk_item, f"BOX-BULK-FULL5-{i}", cell=full_cell)
    _make_box(warehouse, other_item, "BOX-BULK-MIXED5", cell=mixed_cell)
    _make_box(warehouse, bulk_item, "BOX-BULK-STOCK5")  # неразмещенный — для is_bulk

    target_box = _make_box(warehouse, bulk_item, "BOX-BULK-TARGET5")

    suggestion = suggest_cell(warehouse.id, target_box)

    assert suggestion is not None
    assert suggestion["cell"].id == empty_cell.id
    assert "отдельную ячейку" in suggestion["reason"]
