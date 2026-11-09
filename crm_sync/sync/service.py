import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, delete, or_, select, update

from ..extensions import db
from ..models import AbstractCustomer, CrmEntityIndex, SyncRun
from .records import CrmRecord
from .source import CrmSource

logger = logging.getLogger(__name__)

ENTITY_CUSTOMER = "customer"
ENTITY_LEAD = "lead"


@dataclass
class SyncResult:
    customers_processed: int = 0
    leads_processed: int = 0
    customers_skipped: int = 0
    groups_created: int = 0
    groups_merged: int = 0
    groups_deleted: int = 0


class _SyncContext:
    """Кэш записей, нужных текущей пачке."""

    def __init__(
        self, session, records: List[CrmRecord], exact_type: str, fallback_type: str
    ):
        self.session = session
        self.index_by_key: Dict[Tuple[str, int], CrmEntityIndex] = {}
        self.login_parents = defaultdict(dict)
        self.parent_by_key: Dict[Tuple[str, int], AbstractCustomer] = {}

        # Загружаем совпадения пачки, а не весь индекс.
        crm_ids = {record.crm_id for record in records}
        logins = {record.login for record in records if record.login}
        conditions = [
            and_(
                CrmEntityIndex.entity_type == exact_type,
                CrmEntityIndex.crm_id.in_(crm_ids),
            )
        ]
        if logins:
            conditions.append(
                and_(
                    CrmEntityIndex.entity_type == fallback_type,
                    CrmEntityIndex.login.in_(logins),
                )
            )
        indexes = session.scalars(select(CrmEntityIndex).where(or_(*conditions))).all()
        parent_ids = {item.abstract_customer_id for item in indexes}
        if parent_ids:
            parent_by_id = {
                parent.id: parent
                for parent in session.scalars(
                    select(AbstractCustomer).where(AbstractCustomer.id.in_(parent_ids))
                ).all()
            }
        else:
            parent_by_id = {}
        for item in indexes:
            parent = parent_by_id.get(item.abstract_customer_id)
            if parent is not None:
                self._remember(item, parent)

    def _remember(self, item: CrmEntityIndex, parent: AbstractCustomer) -> None:
        """Добавляет индекс и родителя в кэш пачки."""
        key = (item.entity_type, int(item.crm_id))
        self.index_by_key[key] = item
        self.parent_by_key[key] = parent
        item.abstract_customer_id = parent.id
        item.abstract_customer = parent
        if item.login:
            self.login_parents[(item.entity_type, item.login)][id(parent)] = parent

    def find_exact(self, entity_type: str, crm_id: int) -> Optional[AbstractCustomer]:
        """Ищет сущность по точному CRM ID."""
        item = self.index_by_key.get((entity_type, crm_id))
        if item is None:
            return None
        return self.parent_by_key.get((entity_type, crm_id))

    def find_unique_login(
        self, entity_type: str, login: Optional[str]
    ) -> Optional[AbstractCustomer]:
        """Возвращает группу при одном совпадении логина."""
        if not login:
            return None
        matches = self.login_parents.get((entity_type, login), {})
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))

    def add_or_update_index(
        self, entity_type: str, record: CrmRecord, parent: AbstractCustomer
    ) -> None:
        """Обновляет индекс для следующих поисков."""
        key = (entity_type, record.crm_id)
        item = self.index_by_key.get(key)
        if item is None:
            item = CrmEntityIndex(entity_type=entity_type, crm_id=record.crm_id)
            self.session.add(item)
            self.index_by_key[key] = item
        else:
            previous_parent = self.parent_by_key.get(key)
            if item.login and previous_parent is not None:
                self.login_parents[(entity_type, item.login)].pop(
                    id(previous_parent), None
                )

        item.login = record.login
        item.name = record.name
        item.abstract_customer_id = parent.id
        item.abstract_customer = parent
        self.parent_by_key[key] = parent
        if record.login:
            self.login_parents[(entity_type, record.login)][id(parent)] = parent

    def merge(self, target: AbstractCustomer, source: AbstractCustomer) -> None:
        """Переносит данные и ссылки в целевую группу."""
        if target is source:
            return
        # Группа, найденная по ID сделки, остаётся основной.
        _merge_parent_data(target, source)

        if target.id is None:
            self.session.flush()
        if source.id is None:
            for key, current_parent in list(self.parent_by_key.items()):
                if current_parent is source:
                    item = self.index_by_key[key]
                    item.abstract_customer_id = target.id
                    item.abstract_customer = target
                    self.parent_by_key[key] = target
            self.session.expunge(source)
        else:
            self.session.execute(
                update(CrmEntityIndex)
                .where(CrmEntityIndex.abstract_customer_id == source.id)
                .values(abstract_customer_id=target.id)
                .execution_options(synchronize_session=False)
            )
            for key, current_parent in list(self.parent_by_key.items()):
                if current_parent is source:
                    item = self.index_by_key[key]
                    item.abstract_customer_id = target.id
                    item.abstract_customer = target
                    self.parent_by_key[key] = target
            self.session.delete(source)

        self._rebuild_login_cache()

    def _rebuild_login_cache(self) -> None:
        self.login_parents.clear()
        for key, item in self.index_by_key.items():
            parent = self.parent_by_key.get(key)
            if parent is not None and item.login:
                self.login_parents[(item.entity_type, item.login)][id(parent)] = parent


def _merge_parent_data(target: AbstractCustomer, source: AbstractCustomer) -> None:
    """Объединяет списки сущностей без дублей по CRM ID."""
    attributes = (
        "amo_customers",
        "amo_leads",
        "amo_contacts",
        "amo_companies",
        "send_methods",
    )
    for attribute in attributes:
        target_items = getattr(target, attribute) or []
        source_items = getattr(source, attribute) or []
        setattr(target, attribute, _merge_items(target_items, source_items))


def _merge_items(left: List[dict], right: List[dict]) -> List[dict]:
    """Объединяет элементы по ID, остальные сохраняет."""
    merged = {}
    unkeyed = []
    for item in left + right:
        general = item.get("general") or {}
        item_id = general.get("id")
        if item_id is None:
            unkeyed.append(item)
            continue
        merged[item_id] = item
    return list(merged.values()) + unkeyed


def _upsert_json_item(parent: AbstractCustomer, attribute: str, item: dict) -> None:
    """Обновляет JSONB-элемент по CRM ID."""
    items = list(getattr(parent, attribute) or [])
    item_id = (item.get("general") or {}).get("id")
    for position, current in enumerate(items):
        if (current.get("general") or {}).get("id") == item_id:
            items[position] = item
            setattr(parent, attribute, items)
            return
    items.append(item)
    setattr(parent, attribute, items)


def _remember_children(parent: AbstractCustomer, record: CrmRecord) -> None:
    """Обновляет контакты и компании из записи CRM."""
    for child in record.contacts:
        _upsert_json_item(parent, "amo_contacts", child)
    for child in record.companies:
        _upsert_json_item(parent, "amo_companies", child)


def _new_parent(session, result: SyncResult) -> AbstractCustomer:
    """Создаёт группу, если подходящей записи ещё нет."""
    parent = AbstractCustomer(
        amo_customers=[],
        amo_leads=[],
        amo_companies=[],
        amo_contacts=[],
        send_methods=[],
    )
    session.add(parent)
    result.groups_created += 1
    return parent


def _process_customer_batch(
    session, records: List[CrmRecord], result: SyncResult, seen: dict
) -> None:
    """Обрабатывает одну пачку покупателей."""
    context = _SyncContext(session, records, ENTITY_CUSTOMER, ENTITY_LEAD)

    for record in records:
        # Покупателей без логина пропускаем.
        if not record.login:
            result.customers_skipped += 1
            continue
        # Сначала ищем по ID, затем — по логину сделки.
        parent = context.find_exact(ENTITY_CUSTOMER, record.crm_id)
        if parent is None:
            parent = context.find_unique_login(ENTITY_LEAD, record.login)
        if parent is None:
            parent = _new_parent(session, result)
        _upsert_json_item(parent, "amo_customers", record.customer_json())
        _remember_children(parent, record)
        context.add_or_update_index(ENTITY_CUSTOMER, record, parent)
        seen[ENTITY_CUSTOMER].add(record.crm_id)
        _mark_parent_entity(parent, "customer_groups", record.crm_id, seen)
        _add_child_ids(parent, record, seen)
        result.customers_processed += 1
    session.flush()
    _finalize_pending_parent_keys(seen)
    session.commit()


def _process_lead_batch(
    session, records: List[CrmRecord], result: SyncResult, seen: dict
) -> None:
    """Обрабатывает одну пачку сделок."""
    context = _SyncContext(session, records, ENTITY_LEAD, ENTITY_CUSTOMER)

    for record in records:
        lead_parent = context.find_exact(ENTITY_LEAD, record.crm_id)
        customer_parent = context.find_unique_login(ENTITY_CUSTOMER, record.login)

        # При конфликте оставляем группу сделки.
        if (
            lead_parent is not None
            and customer_parent is not None
            and lead_parent is not customer_parent
        ):
            _transfer_seen(customer_parent, lead_parent, seen)
            context.merge(lead_parent, customer_parent)
            result.groups_merged += 1
        parent = lead_parent or customer_parent
        if parent is None:
            parent = _new_parent(session, result)

        _upsert_json_item(parent, "amo_leads", record.lead_json())
        _remember_children(parent, record)
        context.add_or_update_index(ENTITY_LEAD, record, parent)
        seen[ENTITY_LEAD].add(record.crm_id)
        _mark_parent_entity(parent, "lead_groups", record.crm_id, seen)
        _add_child_ids(parent, record, seen)
        result.leads_processed += 1
    session.flush()
    _finalize_pending_parent_keys(seen)
    session.commit()


def _parent_seen_key(parent: AbstractCustomer, seen: dict):
    """Возвращает ID группы или временный ключ."""
    if parent.id is not None:
        return parent.id
    key = ("pending", id(parent))
    seen["pending_parent_refs"][key] = parent
    return key


def _finalize_pending_parent_keys(seen: dict) -> None:
    """Заменяет временные ключи групп на ID после flush."""
    for pending_key, parent in list(seen["pending_parent_refs"].items()):
        if parent.id is None:
            continue
        for entity_type in ("customer_groups", "lead_groups", "contacts", "companies"):
            pending_ids = seen[entity_type].pop(pending_key, set())
            if pending_ids:
                seen[entity_type].setdefault(parent.id, set()).update(pending_ids)
        del seen["pending_parent_refs"][pending_key]


def _mark_parent_entity(
    parent: AbstractCustomer, entity_type: str, crm_id: int, seen: dict
) -> None:
    """Отмечает CRM ID, который встретился в выгрузке."""
    parent_key = _parent_seen_key(parent, seen)
    seen[entity_type].setdefault(parent_key, set()).add(crm_id)


def _add_child_ids(parent: AbstractCustomer, record: CrmRecord, seen: dict) -> None:
    """Запоминает ID вложенных контактов и компаний."""
    parent_key = _parent_seen_key(parent, seen)
    for child in record.contacts:
        seen["contacts"].setdefault(parent_key, set()).add(child["general"]["id"])
    for child in record.companies:
        seen["companies"].setdefault(parent_key, set()).add(child["general"]["id"])


def _transfer_seen(
    source: AbstractCustomer, target: AbstractCustomer, seen: dict
) -> None:
    """Переносит отметки обработанных сущностей."""
    source_key = _parent_seen_key(source, seen)
    target_key = _parent_seen_key(target, seen)
    for entity_type in ("customer_groups", "lead_groups", "contacts", "companies"):
        source_ids = seen[entity_type].pop(source_key, set())
        if source_ids:
            seen[entity_type].setdefault(target_key, set()).update(source_ids)
    seen["pending_parent_refs"].pop(source_key, None)


def _batches(values: Iterable, batch_size: int):
    """Разбивает поток CRM на небольшие пачки."""
    batch = []
    for value in values:
        batch.append(CrmRecord.from_value(value))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _clean_items(items: Optional[List[dict]], keep_ids: Set[int]) -> List[dict]:
    """Оставляет элементы без пометки «удалить»."""
    result = []
    for item in items or []:
        general = item.get("general") or {}
        item_id = general.get("id")
        try:
            item_id = int(item_id) if item_id is not None else None
        except (TypeError, ValueError):
            item_id = None
        name = str(general.get("name") or "").casefold()
        if item_id in keep_ids and "удалить" not in name:
            result.append(item)
    return result


def _cleanup(session, seen: dict, batch_size: int) -> int:
    """Удаляет отсутствующие данные пачками."""
    # Идём по ID небольшими страницами и не загружаем всю таблицу.
    last_id = 0
    deleted_groups = 0
    while True:
        page_ids = session.scalars(
            select(AbstractCustomer.id)
            .where(AbstractCustomer.id > last_id)
            .order_by(AbstractCustomer.id)
            .limit(batch_size)
        ).all()
        if not page_ids:
            break
        last_id = page_ids[-1]
        parents = session.scalars(
            select(AbstractCustomer).where(AbstractCustomer.id.in_(page_ids))
        ).all()
        deleted_ids = []
        for parent in parents:
            parent.amo_customers = _clean_items(
                parent.amo_customers, seen["customer_groups"].get(parent.id, set())
            )
            parent.amo_leads = _clean_items(
                parent.amo_leads, seen["lead_groups"].get(parent.id, set())
            )
            parent.amo_contacts = _clean_items(
                parent.amo_contacts, seen["contacts"].get(parent.id, set())
            )
            parent.amo_companies = _clean_items(
                parent.amo_companies, seen["companies"].get(parent.id, set())
            )
            if not parent.amo_customers and not parent.amo_leads:
                deleted_groups += 1
                if parent.id is not None:
                    deleted_ids.append(parent.id)
                session.delete(parent)
        if deleted_ids:
            session.execute(
                delete(CrmEntityIndex).where(
                    CrmEntityIndex.abstract_customer_id.in_(deleted_ids)
                )
            )
        session.flush()

    # Чистим индекс после проверки всех записей.
    last_index_id = 0
    while True:
        rows = session.execute(
            select(
                CrmEntityIndex.id,
                CrmEntityIndex.entity_type,
                CrmEntityIndex.crm_id,
                CrmEntityIndex.name,
            )
            .where(CrmEntityIndex.id > last_index_id)
            .order_by(CrmEntityIndex.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        last_index_id = rows[-1].id
        stale_ids = []
        for row in rows:
            valid_ids = (
                seen[ENTITY_CUSTOMER]
                if row.entity_type == ENTITY_CUSTOMER
                else seen[ENTITY_LEAD]
            )
            is_marked_for_delete = "удалить" in str(row.name or "").casefold()
            if row.crm_id not in valid_ids or is_marked_for_delete:
                stale_ids.append(row.id)
        if stale_ids:
            session.execute(
                delete(CrmEntityIndex).where(CrmEntityIndex.id.in_(stale_ids))
            )
        session.flush()
    return deleted_groups


def collect_leads_customers(
    is_full: bool = True,
    source: Optional[CrmSource] = None,
    batch_size: int = 500,
) -> SyncResult:
    """Синхронизирует покупателей и сделки."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    from flask import current_app

    source = source or current_app.extensions.get("crm_source")
    if source is None:
        raise RuntimeError("CRM source is not configured")

    session = db.session
    result = SyncResult()
    seen = {
        ENTITY_CUSTOMER: set(),
        ENTITY_LEAD: set(),
        "customer_groups": {},
        "lead_groups": {},
        "contacts": {},
        "companies": {},
        "pending_parent_refs": {},
    }
    # Фиксируем старт, чтобы сохранить статус при сбое.
    run = SyncRun(is_full=is_full, status="running")
    session.add(run)
    session.commit()

    try:
        logger.info("CRM sync started; full=%s", is_full)
        # Читаем оба источника до начала очистки.
        for batch in _batches(source.iter_customers(), batch_size):
            _process_customer_batch(session, batch, result, seen)
        for batch in _batches(source.iter_leads(), batch_size):
            _process_lead_batch(session, batch, result, seen)

        # До чтения обоих потоков базу не очищаем.
        if is_full:
            try:
                result.groups_deleted = _cleanup(session, seen, batch_size)
                session.commit()
            except Exception:
                session.rollback()
                raise

        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        _copy_result(run, result)
        session.commit()
        logger.info(
            "CRM sync completed; customers=%s leads=%s merged=%s deleted=%s",
            result.customers_processed,
            result.leads_processed,
            result.groups_merged,
            result.groups_deleted,
        )
        return result
    except Exception as exc:
        # При сбое очистка не запускается.
        session.rollback()
        failed_run = session.get(SyncRun, run.id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.finished_at = datetime.now(timezone.utc)
            failed_run.error = str(exc)[:4000]
            _copy_result(failed_run, result)
            session.commit()
        logger.exception("CRM sync failed; cleanup was not committed")
        raise


def _copy_result(run: SyncRun, result: SyncResult) -> None:
    """Записывает счётчики синхронизации в запуск."""
    run.customers_processed = result.customers_processed
    run.leads_processed = result.leads_processed
    run.customers_skipped = result.customers_skipped
    run.groups_created = result.groups_created
    run.groups_merged = result.groups_merged
    run.groups_deleted = result.groups_deleted


def rebuild_entity_index(batch_size: int = 1000) -> int:
    """Пересоздаёт индекс по данным JSONB."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    session = db.session

    # Перед построением удаляем старый индекс.
    session.execute(delete(CrmEntityIndex))
    session.commit()

    seen = {ENTITY_CUSTOMER: set(), ENTITY_LEAD: set()}
    last_id = 0
    indexed = 0
    while True:
        page_ids = session.scalars(
            select(AbstractCustomer.id)
            .where(AbstractCustomer.id > last_id)
            .order_by(AbstractCustomer.id)
            .limit(batch_size)
        ).all()
        if not page_ids:
            break
        last_id = page_ids[-1]
        parents = session.scalars(
            select(AbstractCustomer).where(AbstractCustomer.id.in_(page_ids))
        ).all()
        rows = []
        for parent in parents:
            for entity_type, attribute in (
                (ENTITY_CUSTOMER, "amo_customers"),
                (ENTITY_LEAD, "amo_leads"),
            ):
                for item in getattr(parent, attribute) or []:
                    general = item.get("general") or {}
                    crm_id = general.get("id")
                    if crm_id is None:
                        continue
                    crm_id = int(crm_id)
                    if crm_id in seen[entity_type]:
                        continue
                    seen[entity_type].add(crm_id)
                    rows.append(
                        CrmEntityIndex(
                            entity_type=entity_type,
                            crm_id=crm_id,
                            login=general.get("login"),
                            name=general.get("name"),
                            abstract_customer_id=parent.id,
                        )
                    )
        if rows:
            session.add_all(rows)
            session.commit()
            indexed += len(rows)
    return indexed
