from sqlalchemy import create_engine, inspect

from tradingagent.persistence.models import Base


def test_foundation_schema_declares_versioned_configuration_and_ledger_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    tables = set(inspect(engine).get_table_names())
    assert {
        "schema_versions",
        "configuration_snapshots",
        "cash_ledger",
        "positions",
        "orders",
        "fills",
        "audit_events",
    } <= tables


def test_signal_decision_hash_identifiers_fit_the_relational_schema() -> None:
    decisions = Base.metadata.tables["signal_decisions"]
    orders = Base.metadata.tables["orders"]

    assert decisions.c.id.type.length == 64
    assert orders.c.decision_id.type.length == 64
