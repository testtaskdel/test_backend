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


def test_customer_without_login_is_skipped_when_no_saved_record_exists(app):
    with app.app_context():
        result = collect_leads_customers(
            source=FakeCrm([customer(10, "")], []),
            is_full=False,
        )

        assert result.customers_skipped == 1
        assert db.session.query(AbstractCustomer).count() == 0


def test_customer_without_login_keeps_existing_record_during_full_sync(app):
    with app.app_context():
        group = add_group(
            customer_items=[
                {
                    "general": {
                        "id": 10,
                        "login": "saved-login",
                        "name": "Saved customer",
                    },
                    "api_value": {},
                }
            ]
        )
        group.amo_contacts = [
            {
                "general": {"id": 701, "name": "Current contact"},
                "api_value": {},
            }
        ]
        db.session.commit()

        result = collect_leads_customers(
            source=FakeCrm(
                [customer(10, "", contacts=[{"id": 701, "name": "Current contact"}])],
                [],
            ),
            is_full=True,
            batch_size=1,
        )

        db.session.expire_all()
        saved_group = db.session.get(AbstractCustomer, group.id)

        assert saved_group is not None
        assert saved_group.amo_customers[0]["general"]["login"] == "saved-login"
        assert [item["general"]["id"] for item in saved_group.amo_contacts] == [701]
        assert result.customers_skipped == 1
        assert db.session.query(CrmEntityIndex).count() == 1


def test_customer_without_login_and_delete_marker_is_removed(app):
    with app.app_context():
        add_group(
            customer_items=[
                {
                    "general": {
                        "id": 10,
                        "login": "saved-login",
                        "name": "Saved customer",
                    },
                    "api_value": {},
                }
            ]
        )

        collect_leads_customers(
            source=FakeCrm(
                [customer(10, "", name="Удалить customer")],
                [],
            ),
            is_full=True,
        )

        assert db.session.query(AbstractCustomer).count() == 0
        assert db.session.query(CrmEntityIndex).count() == 0


def test_duplicate_login_is_not_used_as_match_across_groups(app):
    with app.app_context():
        first_item = {
            "general": {"id": 1, "login": "same", "name": "Lead 1"},
            "api_value": {},
        }
        second_item = {
            "general": {"id": 2, "login": "same", "name": "Lead 2"},
            "api_value": {},
        }
        add_group(lead_items=[first_item])
        add_group(lead_items=[second_item])

        collect_leads_customers(
            source=FakeCrm([customer(10, "same")], []),
            is_full=False,
        )

        assert db.session.query(AbstractCustomer).count() == 3


def test_duplicate_lead_login_in_same_group_is_ambiguous(app):
    with app.app_context():
        lead_group = add_group(
            lead_items=[
                {
                    "general": {"id": 1, "login": "same", "name": "Lead 1"},
                    "api_value": {},
                },
                {
                    "general": {"id": 2, "login": "same", "name": "Lead 2"},
                    "api_value": {},
                },
            ]
        )

        collect_leads_customers(
            source=FakeCrm([customer(10, "same")], []),
            is_full=False,
        )

        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        customer_groups = [group for group in groups if group.amo_customers]

        assert len(groups) == 2
        assert len(customer_groups) == 1
        assert customer_groups[0].id != lead_group.id
        assert len(lead_group.amo_leads) == 2


def test_duplicate_customer_login_in_same_group_is_ambiguous(app):
    with app.app_context():
        customer_group = add_group(
            customer_items=[
                {
                    "general": {"id": 10, "login": "same", "name": "Customer 10"},
                    "api_value": {},
                },
                {
                    "general": {"id": 11, "login": "same", "name": "Customer 11"},
                    "api_value": {},
                },
            ]
        )

        collect_leads_customers(
            source=FakeCrm([], [lead(50, "same")]),
            is_full=False,
        )

        groups = db.session.scalars(db.select(AbstractCustomer)).all()
        lead_groups = [group for group in groups if group.amo_leads]

        assert len(groups) == 2
        assert len(lead_groups) == 1
        assert lead_groups[0].id != customer_group.id
        assert len(customer_group.amo_customers) == 2


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
            source=FakeCrm([customer(10, "current")], []),
            is_full=True,
            batch_size=1,
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
                {
                    "general": {"id": 98, "login": "stale", "name": "Old customer"},
                    "api_value": {},
                }
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
                {
                    "general": {"id": 90, "login": "old", "name": "Old"},
                    "api_value": {},
                }
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
