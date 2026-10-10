"""Operator CLI for the external cash-flow ledger (Binance USD-M).

Read-only by default. Mutating commands require ``--apply`` plus an exact
``--confirm`` token and are idempotent by ``--reconciliation-id``.

    python -m bot.cash_flow_admin show
    python -m bot.cash_flow_admin attest --reconciliation-id ID --tran-ids 1,2 \
        --pre-flow-equity 9.8410 --evidence-ref "..." --reason "..." \
        [--since-ms MS] [--apply --confirm ATTEST:ID]
    python -m bot.cash_flow_admin rebaseline-wallet --reason "..." \
        [--apply --confirm REBASELINE-WALLET]

Amounts, directions and timestamps always come from Binance ``/fapi/v1/income``
rows; they are never typed by the operator and never inferred from a balance
difference. Never prints credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bot import cash_flow_ledger as ledger

_WINDOW_MS = 7 * 86400 * 1000 - 60_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bot.cash_flow_admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print ledger, pending flows and durable HWM")
    attest = sub.add_parser("attest", help="operator attestation for pending/aged-out flows")
    attest.add_argument("--reconciliation-id", required=True)
    attest.add_argument("--tran-ids", required=True, help="comma-separated Binance tranIds")
    attest.add_argument("--pre-flow-equity", required=True, type=float)
    attest.add_argument("--evidence-ref", required=True)
    attest.add_argument("--reason", required=True)
    attest.add_argument("--since-ms", type=int, default=None, help="income window start (<=7d window)")
    attest.add_argument("--apply", action="store_true")
    attest.add_argument("--confirm", default="")
    rebase = sub.add_parser("rebaseline-wallet", help="re-anchor the wallet completeness invariant")
    rebase.add_argument("--reason", required=True)
    rebase.add_argument("--apply", action="store_true")
    rebase.add_argument("--confirm", default="")
    return parser


async def _client():
    from bot.binance import BinanceClient

    client = BinanceClient()
    await client.sync_time()
    return client


async def _run(args) -> dict:
    from bot import database as db

    await db.init()
    client = None
    try:
        if args.command == "show":
            return await ledger.ledger_snapshot(strict=True)
        client = await _client()
        now_ms = client._now_ms()
        account = await client.get_account_state()
        if args.command == "attest":
            if args.apply and args.confirm != f"ATTEST:{args.reconciliation_id}":
                raise SystemExit("refused: --confirm must be ATTEST:<reconciliation-id>")
            from bot.binance_accounting_evidence import collect_income

            start = args.since_ms if args.since_ms is not None else now_ms - _WINDOW_MS
            end = min(now_ms, int(start) + _WINDOW_MS)
            rows = await collect_income(client, int(start), end)
            return await ledger.attest_flows(
                rows=rows,
                reconciliation_id=args.reconciliation_id,
                tran_ids=args.tran_ids.split(","),
                pre_flow_equity=args.pre_flow_equity,
                current_equity=float(account["equity"]),
                reason=args.reason,
                evidence_ref=args.evidence_ref,
                apply=bool(args.apply),
                now_ms=now_ms,
            )
        if args.command == "rebaseline-wallet":
            if args.apply and args.confirm != "REBASELINE-WALLET":
                raise SystemExit("refused: --confirm must be REBASELINE-WALLET")
            return await ledger.rebaseline_wallet(
                wallet=float(account["walletBalance"]),
                anchor_ms=now_ms,
                reason=args.reason,
                apply=bool(args.apply),
            )
        raise SystemExit("unknown command")
    finally:
        if client is not None:
            await client.close()
        await db.close()


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
