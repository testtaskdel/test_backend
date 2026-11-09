from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class CrmRecord:
    """Приводит покупателя или сделку к общему формату."""

    crm_id: int
    name: str
    login: Optional[str]
    payload: Dict[str, Any]
    contacts: List[Dict[str, Any]]
    companies: List[Dict[str, Any]]
    customer_fields: Dict[str, Any]

    @classmethod
    def from_value(cls, value: Any) -> "CrmRecord":
        """Принимает ответ API или объект CRM-клиента."""
        if isinstance(value, cls):
            return value

        raw = _as_payload(value)
        crm_id = raw.get("id", getattr(value, "id", None))
        if crm_id is None:
            raise ValueError("CRM record has no id")

        name = raw.get("name", getattr(value, "name", "")) or ""
        fields = _custom_fields(value, raw)
        login = fields.get("Логин")
        if login is not None:
            login = str(login).strip() or None

        # В amoCRM вложенные сущности приходят в _embedded.
        embedded = raw.get("_embedded") or {}
        contacts = embedded.get("contacts")
        companies = embedded.get("companies")

        if contacts is None:
            contacts = getattr(value, "contacts", []) or []
        if companies is None:
            companies = getattr(value, "companies", []) or []

        return cls(
            crm_id=int(crm_id),
            name=str(name),
            login=login,
            payload=raw,
            contacts=[_child_payload(item, "contact") for item in contacts],
            companies=[_child_payload(item, "company") for item in companies],
            customer_fields=fields,
        )

    def customer_json(self) -> Dict[str, Any]:
        """Готовит покупателя для хранения в JSONB."""
        fields = self.customer_fields

        return {
            "general": {
                "id": self.crm_id,
                "login": self.login,
                "billing_type": fields.get("Вид оплаты"),
                "is_robot": bool(fields.get("Робот")),
                "is_nds": bool(fields.get("Оплата с НДС")),
                "system": fields.get("Система"),
                "host": fields.get("Хост"),
                "name": self.name,
            },
            "api_value": deepcopy(self.payload),
        }

    def lead_json(self) -> Dict[str, Any]:
        """Готовит сделку для хранения в JSONB."""
        return {
            "general": {
                "id": self.crm_id,
                "login": self.login,
                "name": self.name,
            },
            "api_value": deepcopy(self.payload),
        }


def _as_payload(value: Any) -> Dict[str, Any]:
    """Получает словарь API из входного объекта."""
    if isinstance(value, Mapping):
        return deepcopy(dict(value))

    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, Mapping):
            return deepcopy(dict(payload))

    # Без as_dict берём только нужные поля.
    payload = {}
    for key in (
        "id",
        "name",
        "custom_fields_values",
        "_embedded",
        "status_id",
        "created_at",
        "updated_at",
        "price",
        "responsible_user_id",
        "pipeline_id",
        "account_id",
    ):
        if hasattr(value, key):
            payload[key] = getattr(value, key)

    return deepcopy(payload)


def _custom_fields(value: Any, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Читает пользовательские поля amoCRM."""
    get_field = getattr(value, "get_field", None)
    fields = (
        payload.get("custom_fields_values")
        or getattr(value, "custom_fields_values", None)
        or {}
    )
    result: Dict[str, Any] = {}

    if isinstance(fields, Mapping):
        for name, field_value in fields.items():
            if isinstance(field_value, Mapping):
                result[str(name)] = field_value.get("value")
            else:
                result[str(name)] = field_value
    elif isinstance(fields, list):
        for field in fields:
            if not isinstance(field, Mapping):
                continue

            name = field.get("field_name") or field.get("name")
            values = field.get("values") or []
            if name and values:
                first = values[0]
                result[str(name)] = (
                    first.get("value") if isinstance(first, Mapping) else first
                )

    # Некоторые клиенты дают доступ к полям через get_field().
    if callable(get_field):
        field_names = (
            "Логин",
            "Вид оплаты",
            "Робот",
            "Оплата с НДС",
            "Система",
            "Хост",
        )
        for name in field_names:
            try:
                field_value = get_field(name)
            except (KeyError, AttributeError, TypeError):
                continue

            if field_value is not None:
                result[name] = field_value

    return result


def _child_payload(value: Any, entity_type: str) -> Dict[str, Any]:
    """Готовит контакт или компанию для хранения в JSONB."""
    raw = _as_payload(value)
    crm_id = raw.get("id", getattr(value, "id", None))
    if crm_id is None:
        raise ValueError("Embedded CRM entity has no id")

    name = raw.get("name", getattr(value, "name", "")) or ""
    fields = _custom_fields(value, raw)
    general = {"id": int(crm_id), "name": str(name)}

    if entity_type == "contact":
        general["phone"] = fields.get("Телефон")
    else:
        general["inn"] = fields.get("ИНН")

    return {"general": general, "api_value": raw}
