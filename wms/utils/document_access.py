"""Ограничение рабочих документов их автором.

Обычный пользователь работает только со своими документами. Администратор
видит все документы, включая старые записи без автора.
"""

from flask import abort
from flask_login import current_user


def owned_query(model):
    """Запрос документов, доступных текущему пользователю."""
    query = model.query
    if current_user.is_admin:
        return query
    return query.filter(model.created_by_id == current_user.id)


def get_owned_or_404(model, document_id):
    """Находит документ и скрывает чужой как несуществующий."""
    document = model.query.get_or_404(document_id)
    if not current_user.is_admin and document.created_by_id != current_user.id:
        abort(404)
    return document


def ensure_view_document_access(model):
    """Проверка blueprint-маршрута, если в URL передан ``doc_id``."""
    from flask import request

    document_id = (request.view_args or {}).get("doc_id")
    if document_id is not None:
        get_owned_or_404(model, document_id)
