import pytest

from crm_sync import create_app
from crm_sync.extensions import db
from crm_sync.models import AbstractCustomer, CrmEntityIndex
from crm_sync.sync import CrmSource, collect_leads_customers


class FakeCrm(CrmSource):
    def __init__(self, customers=(), leads=(), fail_on_leads=False):
        self.customers = customers
        self.leads = leads
        self.fail_on_leads = fail_on_leads

    def iter_customers(self):
        yield from self.customers

    def iter_leads(self):
        if self.fail_on_leads:
            raise RuntimeError("CRM export failed")
        yield from self.leads


@pytest.fixture()
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def customer(crm_id, login, name=None, contacts=None):
    return {
        "id": crm_id,
        "name": name or "Customer {}".format(crm_id),
        "custom_fields_values": {"Логин": {"value": login}},
        "_embedded": {"contacts": contacts or [], "companies": []},
    }


def lead(crm_id, login, name=None):
    return {
        "id": crm_id,
        "name": name or "Lead {}".format(crm_id),
        "custom_fields_values": {"Логин": {"value": login}},
        "_embedded": {"contacts": [], "companies": []},
    }


def add_group(customer_items=None, lead_items=None):
    group = AbstractCustomer(
        amo_customers=customer_items or [],
        amo_leads=lead_items or [],
        amo_contacts=[],
        amo_companies=[],
        send_methods=[],
    )
    db.session.add(group)
    db.session.flush()
    entity_groups = (
        ("customer", customer_items or []),
        ("lead", lead_items or []),
    )
    for entity_type, items in entity_groups:
        for item in items:
            general = item["general"]
            db.session.add(
                CrmEntityIndex(
                    entity_type=entity_type,
                    crm_id=general["id"],
                    login=general.get("login"),
                    name=general.get("name"),
                    abstract_customer_id=group.id,
                )
            )
    db.session.commit()
    return group


def test_customer_and_lead_with_same_login_share_group(app):
    with app.app_context():
        result = collect_leads_customers(
            source=FakeCrm([customer(10, "acme")], [lead(50, "acme")]),
            is_full=False,
            batch_size=1,
        )
        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        assert len(groups) == 1
        assert [item["general"]["id"] for item in groups[0].amo_customers] == [10]
        assert [item["general"]["id"] for item in groups[0].amo_leads] == [50]
        assert result.customers_processed == 1
        assert result.leads_processed == 1
        assert db.session.query(CrmEntityIndex).count() == 2


def test_customer_without_login_is_skipped(app):
    with app.app_context():
        result = collect_leads_customers(
            source=FakeCrm([customer(10, "")], []), is_full=False
        )
        assert result.customers_skipped == 1
        assert db.session.query(AbstractCustomer).count() == 0


def test_duplicate_login_is_not_used_as_match(app):
    with app.app_context():
        item = {"general": {"id": 1, "login": "same", "name": "A"}, "api_value": {}}
        add_group(lead_items=[item])
        item2 = {"general": {"id": 2, "login": "same", "name": "B"}, "api_value": {}}
        add_group(lead_items=[item2])

        collect_leads_customers(
            source=FakeCrm([customer(10, "same")], []),
            is_full=False,
        )
        assert db.session.query(AbstractCustomer).count() == 3


def test_lead_id_match_wins_and_groups_are_merged(app):
    with app.app_context():
        customer_item = {
            "general": {"id": 10, "login": "acme", "name": "Customer"},
            "api_value": {},
        }
        customer_group = add_group(customer_items=[customer_item])
        lead_item = {
            "general": {"id": 50, "login": "old", "name": "Existing lead"},
            "api_value": {},
        }
        lead_group = add_group(lead_items=[lead_item])

        result = collect_leads_customers(
            source=FakeCrm([customer(10, "acme")], [lead(50, "acme")]),
            is_full=False,
        )
        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        assert len(groups) == 1
        assert groups[0].id == lead_group.id
        assert result.groups_merged == 1
        assert customer_group.id != lead_group.id
        assert all(
            row.abstract_customer_id == lead_group.id
            for row in db.session.scalars(db.select(CrmEntityIndex)).all()
        )


def test_full_sync_removes_stale_groups_and_delete_marked_items(app):
    with app.app_context():
        add_group(
            customer_items=[
                {
                    "general": {"id": 99, "login": "old", "name": "УДАЛИТЬ старого"},
                    "api_value": {},
                }
            ]
        )
        add_group(
            customer_items=[
                {
                    "general": {"id": 98, "login": "stale", "name": "Old customer"},
                    "api_value": {},
                }
            ]
        )
        result = collect_leads_customers(
            source=FakeCrm([customer(10, "current")], []), is_full=True, batch_size=1
        )
        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        assert len(groups) == 1
        assert groups[0].amo_customers[0]["general"]["id"] == 10
        assert result.groups_deleted == 2
        assert db.session.query(CrmEntityIndex).count() == 1


def test_failed_collection_does_not_start_cleanup(app):
    with app.app_context():
        old_group = add_group(
            customer_items=[
                {"general": {"id": 98, "login": "stale", "name": "Old customer"}, "api_value": {}}
            ]
        )
        with pytest.raises(RuntimeError, match="CRM export failed"):
            collect_leads_customers(
                source=FakeCrm([customer(10, "current")], fail_on_leads=True),
                is_full=True,
                batch_size=1,
            )
        assert db.session.get(AbstractCustomer, old_group.id) is not None
        assert db.session.query(AbstractCustomer).count() == 2


def test_nested_entities_are_kept_only_for_their_current_parent(app):
    with app.app_context():
        old_contact = {
            "general": {"id": 700, "name": "Old contact"},
            "api_value": {"id": 700},
        }
        stale_group = add_group(
            customer_items=[
                {"general": {"id": 90, "login": "old", "name": "Old"}, "api_value": {}}
            ]
        )
        stale_group.amo_contacts = [old_contact]
        db.session.commit()

        live_contact = {
            "id": 701,
            "name": "Current contact",
            "custom_fields_values": [],
        }
        collect_leads_customers(
            source=FakeCrm([customer(10, "current", contacts=[live_contact])], []),
            is_full=True,
        )
        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        assert len(groups) == 1
        assert [item["general"]["id"] for item in groups[0].amo_contacts] == [701]
