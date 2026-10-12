"""Разбор актов приёмки маркетплейсов (xlsx), которые прикладываются при
кнопке «Принято на складе» в перемещении.

Wildberries («Акт приемки товара № …», файл act-income-*.xlsx): одна
строка = одна единица товара, колонки «баркод», «ШК короба» (это наш
собственный номер короба Box.box_number, он же в выгрузке для WB),
«кол-во» (как правило 1). Ozon: акт приёмки содержит грузоместа (ГМ) —
сопоставление ГМ → наш короб запоминается при выгрузке «Состав грузовых
мест» (MovementCargoPlace); колонки ищутся по синонимам заголовков.

Парсер возвращает {ключ_короба: {баркод: количество}}; что за ключ
(номер нашего короба или штрихкод ГМ) — решает вызывающая сторона.
"""

import io
import re
from collections import OrderedDict

from openpyxl import load_workbook

MAX_ACT_BYTES = 10 * 1024 * 1024

_BOX_HEADERS = (
    "шк короба", "штрихкод короба", "номер короба", "короб",
    "шк грузоместа", "штрихкод грузоместа", "номер грузоместа", "грузоместо", "грузовое место", "гм",
)
_BARCODE_HEADERS = ("баркод", "штрихкод товара", "штрихкод", "шк товара", "barcode")
_QTY_HEADERS = ("кол-во", "количество", "кол во", "принято", "qty")


class ActParseError(ValueError):
    pass


def normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def normalize_code(value):
    """Штрихкод/номер из ячейки как строка: числа без «.0», без пробелов."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _find_column(headers, names):
    for name in names:
        for idx, header in enumerate(headers):
            if header == name:
                return idx
    return None


def _is_column_numbering_row(row):
    """Строка нумерации колонок под шапкой («1 | 2 | 3 …») — в актах WB она
    есть, и без пропуска превратилась бы в «короб 7 с баркодом 3»."""
    cells = [normalize_code(c) for c in row if normalize_code(c)]
    return len(cells) >= 3 and all(c.isdigit() and len(c) <= 3 for c in cells)


def parse_act(data):
    """Возвращает (boxes, total_qty): boxes = OrderedDict {ключ: {баркод: qty}}.

    Шапка ищется среди первых 40 строк (в акте WB она двухстрочная — над
    ней группа «Фактически принято»): нужна строка, где есть и колонка
    короба, и колонка баркода."""
    if not data:
        raise ActParseError("Файл пустой")
    if len(data) > MAX_ACT_BYTES:
        raise ActParseError("Файл слишком большой (больше 10 МБ)")
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception:
        raise ActParseError("Не удалось прочитать файл — нужен Excel (.xlsx)")

    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        header_idx = None
        box_col = bc_col = qty_col = None
        for i, row in enumerate(rows[:40]):
            headers = [normalize_header(c) for c in row]
            box_col = _find_column(headers, _BOX_HEADERS)
            bc_col = _find_column(headers, _BARCODE_HEADERS)
            if box_col is not None and bc_col is not None and box_col != bc_col:
                qty_col = _find_column(headers, _QTY_HEADERS)
                header_idx = i
                break
        if header_idx is None:
            continue

        boxes = OrderedDict()
        total = 0
        for row in rows[header_idx + 1:]:
            if len(row) <= max(box_col, bc_col):
                continue
            if _is_column_numbering_row(row):
                continue
            box = normalize_code(row[box_col])
            barcode = normalize_code(row[bc_col])
            if not box or not barcode:
                continue
            qty = 1.0
            if qty_col is not None and qty_col < len(row) and row[qty_col] not in (None, ""):
                try:
                    qty = float(row[qty_col])
                except (TypeError, ValueError):
                    continue
            if qty <= 0:
                continue
            items = boxes.setdefault(box, {})
            items[barcode] = items.get(barcode, 0) + qty
            total += qty
        if not boxes:
            raise ActParseError("В акте не найдено ни одной строки с коробом и баркодом")
        return boxes, total

    raise ActParseError(
        "Не найдены колонки «ШК короба» (или грузоместо) и «баркод» — "
        "загрузите акт приёмки в том виде, как его выдал маркетплейс"
    )
