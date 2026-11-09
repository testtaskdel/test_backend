from datetime import datetime, timezone

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .extensions import db


# В тестах SQLite использует Integer вместо PostgreSQL BigInteger.
_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")
_JSON_TYPE = db.JSON().with_variant(JSONB(), "postgresql")


class AbstractCustomer(db.Model):
    """Общая запись покупателя и связанных сущностей CRM."""

    __tablename__ = "abstract_customer"

    id = db.Column(_ID_TYPE, primary_key=True, autoincrement=True)
    amo_customers = db.Column(_JSON_TYPE, nullable=False, default=list)
    amo_leads = db.Column(_JSON_TYPE, nullable=False, default=list)
    amo_companies = db.Column(_JSON_TYPE, nullable=False, default=list)
    amo_contacts = db.Column(_JSON_TYPE, nullable=False, default=list)
    send_methods = db.Column(_JSON_TYPE, nullable=False, default=list)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CrmEntityIndex(db.Model):
    """Индекс CRM ID, логинов и названий."""

    __tablename__ = "crm_entity_index"
    __table_args__ = (
        UniqueConstraint("entity_type", "crm_id", name="uq_crm_entity_type_id"),
        Index("ix_crm_entity_type_login", "entity_type", "login"),
        Index("ix_crm_entity_type_name", "entity_type", "name"),
    )

    id = db.Column(_ID_TYPE, primary_key=True, autoincrement=True)
    entity_type = db.Column(String(16), nullable=False)
    crm_id = db.Column(BigInteger, nullable=False)
    login = db.Column(Text, nullable=True)
    name = db.Column(Text, nullable=True)
    abstract_customer_id = db.Column(
        _ID_TYPE,
        db.ForeignKey("abstract_customer.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    abstract_customer = db.relationship("AbstractCustomer")


class SyncRun(db.Model):
    """Хранит результат и ошибки запуска синхронизации."""

    __tablename__ = "crm_sync_run"

    id = db.Column(_ID_TYPE, primary_key=True, autoincrement=True)
    started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    is_full = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(String(16), nullable=False, default="running")
    customers_processed = db.Column(db.Integer, nullable=False, default=0)
    leads_processed = db.Column(db.Integer, nullable=False, default=0)
    customers_skipped = db.Column(db.Integer, nullable=False, default=0)
    groups_created = db.Column(db.Integer, nullable=False, default=0)
    groups_merged = db.Column(db.Integer, nullable=False, default=0)
    groups_deleted = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(Text, nullable=True)
