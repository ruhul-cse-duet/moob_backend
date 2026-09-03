"""Clear the tax that was recorded on subscriptions but never collected.

    python scripts_fix_subscription_tax.py            # report only, writes nothing
    python scripts_fix_subscription_tax.py --apply    # write the corrections

Every subscription row stored `tax` and `total` from the order summary, which
added a flat 20% the checkout displayed. Stripe was only ever sent the
subtotal, so on a recurring subscription that tax was never charged - and the
platform's revenue figures counted money that never arrived.

Two kinds of row, and only one of them is wrong:

* **Recurring subscriptions** were billed the subtotal. Their `tax` and `total`
  overstate what the customer paid, so they are corrected here.
* **One-off legacy charges** really were charged `total_due_today`, tax
  included, by the old raw-card path. Those rows are accurate and are left
  exactly as they are - rewriting them would misstate history in the other
  direction.

A row is treated as a real one-off charge only when it says so *and* carries
the charge reference to prove it. Anything else - including the rows the old
plan-change route wrote without charging anything at all - is corrected.
"""
import asyncio
import sys
from typing import Any, Dict, List

from app.db.mongo import close, connect, platform_db


def _is_settled_one_off(row: Dict[str, Any]) -> bool:
    """True when the customer really was charged the tax-inclusive total."""
    if row.get("recurring") or row.get("stripe_subscription_id"):
        return False
    # The old raw-card path recorded the Stripe charge id it got back. No
    # reference means no charge was ever taken for this row.
    return bool(row.get("payment_reference"))


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


async def main(apply: bool = False) -> None:
    connect()
    try:
        db = platform_db()
        rows = [row async for row in db.subscriptions.find({"tax": {"$gt": 0}})]
        if not rows:
            print("No subscription rows carry a tax figure. Nothing to do.")
            return

        to_fix: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        for row in rows:
            (kept if _is_settled_one_off(row) else to_fix).append(row)

        overstated = sum(_money(r.get("tax")) for r in to_fix)
        print(f"{len(rows)} row(s) carry a tax figure.")
        print(f"  {len(kept)} settled one-off charge(s) left alone - the tax was "
              f"genuinely collected.")
        print(f"  {len(to_fix)} recurring/uncharged row(s) overstate revenue by "
              f"{overstated:.2f} in total.")

        for row in to_fix[:10]:
            print(f"    {row.get('tenant_id')} {row.get('plan_code')} "
                  f"{row.get('billing_cycle')} status={row.get('status')} "
                  f"amount={_money(row.get('amount'))} tax={_money(row.get('tax'))} "
                  f"-> total {_money(row.get('total'))} becomes "
                  f"{_money(row.get('amount'))}")
        if len(to_fix) > 10:
            print(f"    ... and {len(to_fix) - 10} more")

        if not apply:
            print("\nReport only. Re-run with --apply to write these corrections.")
            return

        # One update per row: `total` becomes that row's own amount, which a
        # single update_many cannot express.
        fixed = 0
        for row in to_fix:
            result = await db.subscriptions.update_one(
                {"_id": row["_id"]},
                {"$set": {"tax": 0.0, "total": _money(row.get("amount"))}},
            )
            fixed += result.modified_count
        print(f"\nCorrected {fixed} row(s).")
    finally:
        await close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv[1:]))
