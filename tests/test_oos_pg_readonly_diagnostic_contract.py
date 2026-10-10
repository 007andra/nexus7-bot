"""Contract tests for the manual OOS PostgreSQL diagnostic (no real DB)."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "oos_pg_readonly_diagnostic.py"


def test_diagnostic_is_manual_and_not_executed_on_import():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
               and isinstance(n.test.left, ast.Name)
               and n.test.left.id == "__name__" for n in tree.body)
    assert "if not args.confirm_read_only:" in SOURCE.read_text()


def test_diagnostic_has_no_mutating_sql_or_analyze():
    text = SOURCE.read_text()
    assert 'EXPLAIN (FORMAT JSON)' in text
    assert 'EXPLAIN (ANALYZE' not in text.upper()
    assert 'conn.transaction(readonly=True)' in text
    assert '"default_transaction_read_only": "on"' in text
    assert '"statement_timeout": "2500"' in text
    assert '"lock_timeout": "500"' in text


def test_diagnostic_requires_explicit_readonly_dsn():
    text = SOURCE.read_text()
    assert 'PG_DIAGNOSTIC_DSN' in text
    assert 'if not dsn:' in text
    assert 'if not dsn.startswith(("postgresql://", "postgres://")):' in text


def test_diagnostic_output_is_aggregate_only():
    text = SOURCE.read_text()
    assert '"connect_ms"' in text
    assert '"select_1_ms"' in text
    assert '"estimated_total_cost"' in text
    assert '"node_types"' in text
    assert 'print(json.dumps(asyncio.run(diagnose(dsn)), sort_keys=True))' in text
    assert '"sql":' not in text
    assert '"dsn":' not in text
    assert '"plan":' not in text


def test_diagnostic_closes_connection_even_on_error():
    tree = ast.parse(SOURCE.read_text())
    diagnose = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "diagnose")
    assert any(isinstance(n, ast.Try) and n.finalbody for n in ast.walk(diagnose))
    assert "await conn.close()" in SOURCE.read_text()
