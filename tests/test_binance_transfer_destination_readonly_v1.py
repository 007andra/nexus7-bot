"""Offline, credential-free verification for the one-shot SAPI destination probe."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from bot import binance_transfer_destination_readonly_v1 as proof


def run(coro):
    return asyncio.run(coro)


def income(amount="-5.31768526", tran="redacted-id", when=1_000_000):
    return [{"incomeType": "TRANSFER", "asset": "USDT", "income": amount,
             "time": when, "tranId": tran, "info": "secret-do-not-report"}]


def histories(route="UMFUTURE_MAIN", tran="redacted-id", amount="5.31768526",
              when=1_000_100, status="CONFIRMED"):
    rows = {kind: [] for kind in proof.ROUTES}
    rows[route] = [{
        "asset": "USDT", "amount": Decimal(amount), "timestamp": when,
        "type": route, "status": status, "tranId": tran,
    }]
    return rows


def test_correlated_id_and_amount_proves_binance_route_not_credit():
    result = proof.compare_income_and_routes(income(), histories())
    assert result["status"] == "EXACT_ID_AND_AMOUNT_TIME"
    assert result["destination"] == "SPOT"
    assert result["destination_credit_confirmed"] is False
    assert result["transfer_initiator_verified"] is False
    assert result["execution_effect"] == "NONE"
    assert "redacted-id" not in str(result)
    assert "secret" not in str(result)


def test_amount_time_only_stays_unverified():
    r = proof.compare_income_and_routes(income(), histories(tran="another-private-id"))
    assert r["status"] == "AMOUNT_TIME_ONLY"
    assert r["destination"] == "UNVERIFIED"
    assert r["candidate_route"] == "UMFUTURE_MAIN"
    assert "private-id" not in str(r)


def test_other_destination_routes_are_bounded_and_unambiguous():
    for kind, destination in proof.ROUTES.items():
        r = proof.compare_income_and_routes(income(), histories(route=kind))
        assert r["status"] == "EXACT_ID_AND_AMOUNT_TIME"
        assert r["destination"] == destination


def test_no_foreign_asset_or_pending_or_wrong_amount_match():
    for route_data in [
        histories(status="PENDING"),
        histories(amount="5.31768527"),
        histories(when=1_000_000 + proof.MATCH_TOLERANCE_MS + 1),
    ]:
        r = proof.compare_income_and_routes(income(), route_data)
        assert r["destination"] == "UNVERIFIED"
        assert r["status"] == "UNMATCHED"
    rows = histories()
    rows["UMFUTURE_MAIN"][0]["asset"] = "BNB"
    assert proof.compare_income_and_routes(income(), rows)["status"] == "UNMATCHED"


def test_multiple_exact_candidates_are_ambiguous_and_no_risk_authority():
    records = histories()
    records["UMFUTURE_FUNDING"] = [dict(records["UMFUTURE_MAIN"][0],
                                           type="UMFUTURE_FUNDING")]
    r = proof.compare_income_and_routes(income(), records)
    assert r == {"status": "AMBIGUOUS_MULTIPLE_MATCHES",
                 "destination": "UNVERIFIED"}


def test_missing_route_and_missing_income_fail_closed():
    assert proof.compare_income_and_routes(income(), {})["status"] == "INCOMPLETE_ROUTE_COVERAGE"
    assert proof.compare_income_and_routes([], histories())["status"] == "INCONCLUSIVE_INCOME_CARDINALITY"
    assert proof.compare_income_and_routes(income(amount="1"), histories())["status"] == "INCONCLUSIVE_INCOME_CARDINALITY"


def test_invalid_window_amount_and_timezone_are_rejected():
    for times in [(1, 1), (-1, 2), (1, proof.MAX_WINDOW_MS + 2)]:
        with TestCase().assertRaises(ValueError):
            proof._window(*times)
    for value in ["NaN", "Infinity", "invalid"]:
        with TestCase().assertRaises(ValueError):
            proof._amount(value)
    with TestCase().assertRaises(ValueError):
        proof._parse_utc("2026-10-11T16:39:00")
    assert proof._parse_utc("2026-10-11T19:40:00Z") == 1791747600000


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.calls = []
        self.body = body

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.body)


class FakeClient:
    def __init__(self, body):
        self._session = FakeSession(body)
        self._rate_sem = asyncio.Semaphore(1)
        self.signed_count = 0

    async def _ensure_session(self):
        pass

    async def _throttle(self):
        pass

    def _signed_params(self, params):
        self.signed_count += 1
        return dict(params, timestamp=12345, signature="fake-signature")


def test_signed_sapi_path_is_strictly_get_and_redacted():
    rows = [{
        "type": "UMFUTURE_MAIN", "asset": "USDT", "amount": "5.31768526",
        "status": "CONFIRMED", "timestamp": 1010000, "tranId": "secret-id",
    }]
    fake = FakeClient({"rows": rows, "total": 1})
    with patch("bot.binance.API_KEY", "placeholder-key"):
        returned = run(proof.read_route(fake, "UMFUTURE_MAIN", 1000000, 1100000))
    assert len(returned) == 1
    assert fake.signed_count == 1
    assert len(fake._session.calls) == 1
    method, url, kwargs = fake._session.calls[0]
    assert method == "GET"
    assert url == "https://api.binance.com/sapi/v1/asset/transfer"
    assert kwargs["params"]["type"] == "UMFUTURE_MAIN"
    assert kwargs["headers"]["X-MBX-APIKEY"] == "placeholder-key"
    assert kwargs["params"]["signature"] == "fake-signature"
    with TestCase().assertRaises(ValueError):
        run(proof.read_route(fake, "MAIN_UMFUTURE", 1000000, 1100000))
    assert len(fake._session.calls) == 1


def test_pagination_cannot_claim_complete_if_truncated():
    fake = FakeClient({"rows": [], "total": 1})
    with patch("bot.binance.API_KEY", "placeholder"):
        with TestCase().assertRaisesRegex(RuntimeError, "PAGINATION_UNCONFIRMED"):
            run(proof.read_route(fake, "UMFUTURE_FUNDING", 1000000, 1100000))


def test_invalid_sapi_response_fails_with_no_raw_error_leakage():
    fake = FakeClient({"rows": [{"account_id": "PRIVATE"}], "total": 1})
    with patch("bot.binance.API_KEY", "placeholder"):
        with TestCase().assertRaisesRegex(RuntimeError, "UNREADABLE"):
            run(proof.read_route(fake, "UMFUTURE_OPTION", 1000000, 1100000))


def test_probe_calls_each_allowlisted_route_once_without_mutation():
    # Fake the pre-existing income endpoint to avoid any network access.
    import bot.binance_transfer_destination_readonly_v1 as module
    client = SimpleNamespace()
    queried = []
    async def read(_, kind, start_ms, end_ms):
        queried.append(kind)
        return histories()[kind]
    async def frozen_income(_, start_ms, end_ms):
        return income()
    with patch.object(module, "collect_income", frozen_income):
        result = run(module.probe(client, 1000000, 1100000, reader=read))
    assert result["destination"] == "SPOT"
    assert queried == list(proof.ROUTES)


def test_probe_missing_permission_fails_closed_instead_of_guessing():
    client = SimpleNamespace()
    async def reader(_, kind, start_ms, end_ms):
        if kind == "UMFUTURE_MARGIN":
            raise RuntimeError("not-authorized")
        return histories()[kind]
    async def income_reader(_, start_ms, end_ms):
        return income()
    with patch.object(proof, "collect_income", income_reader):
        r = run(proof.probe(client, 1000000, 1100000, reader=reader))
    assert r["status"] == "INCOMPLETE_ROUTE_COVERAGE"
    assert r["destination"] == "UNVERIFIED"
    assert "not-authorized" not in str(r)
