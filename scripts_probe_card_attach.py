"""Reproduce the three Stripe calls that saving a card makes, one at a time.

    python scripts_probe_card_attach.py                 # makes its own customer
    python scripts_probe_card_attach.py cus_ABC123      # uses an existing one

Saving a card is retrieve -> attach -> set default. When the server reports
"the customer does not have a payment method with the ID pm_...", the third
call is refusing what the second was supposed to have done, and nothing in the
HTTP response says which of them misbehaved.

This runs them in isolation and prints what Stripe says after each, so the step
that breaks is visible rather than inferred. Test mode only: it refuses to run
against a live key, and it cleans up anything it created.
"""
import asyncio
import sys

import stripe

from app.core.config import settings
from app.services import stripe_service


async def main(customer_id: str = "") -> None:
    if not stripe_service.configured():
        print("Stripe is not configured - set STRIPE_SECRET_KEY.")
        raise SystemExit(1)
    key = settings.STRIPE_SECRET_KEY
    if not key.startswith("sk_test_"):
        print("This probe attaches real cards; run it against a test key only.")
        raise SystemExit(1)
    stripe.api_key = key
    print(f"stripe-python {stripe.VERSION}, key {key[:12]}...")

    made_customer, existing = False, None
    if not customer_id:
        customer = await stripe.Customer.create_async(
            email="probe@example.com", name="Card attach probe")
        customer_id, made_customer = customer.id, True
        print(f"1. created customer      {customer_id}")
    else:
        customer = await stripe.Customer.retrieve_async(customer_id)
        print(f"1. using customer        {customer_id} "
              f"(email {getattr(customer, 'email', None)!r})")
        # Whatever this customer already bills to has to survive the probe:
        # leaving a real subscription pointed at a card we then detach would
        # break the next renewal.
        # StripeObject is not a dict in stripe>=12 - it has no .get at all.
        invoice_settings = getattr(customer, "invoice_settings", None)
        existing = getattr(invoice_settings, "default_payment_method", None)
        existing = getattr(existing, "id", existing)
        print(f"   currently bills to     {existing!r}")

    # tok_visa is Stripe's test token; a raw card number would be rejected.
    method = await stripe.PaymentMethod.create_async(
        type="card", card={"token": "tok_visa"})
    print(f"2. created payment method {method.id}")
    print(f"   attached to            {method.customer!r}   <- null until attached")

    attached = await stripe.PaymentMethod.attach_async(method.id, customer=customer_id)
    landed = getattr(attached, "customer", None)
    landed = getattr(landed, "id", landed)
    print(f"3. attach returned        customer={landed!r}")
    print(f"   {'OK' if landed == customer_id else 'WRONG - this is the bug'}")

    # Read it back rather than trusting the response, in case the two disagree.
    fresh = await stripe.PaymentMethod.retrieve_async(method.id)
    fresh_owner = getattr(fresh, "customer", None)
    fresh_owner = getattr(fresh_owner, "id", fresh_owner)
    print(f"4. re-read says           customer={fresh_owner!r}")

    try:
        await stripe.Customer.modify_async(
            customer_id, invoice_settings={"default_payment_method": method.id})
        print("5. set as default         OK - saving a card works on this account")
    except Exception as exc:  # noqa: BLE001
        print(f"5. set as default         FAILED: {exc}")

    if not made_customer and existing:
        await stripe.Customer.modify_async(
            customer_id, invoice_settings={"default_payment_method": existing})
        print(f"   restored the default to {existing}")
    await stripe.PaymentMethod.detach_async(method.id)
    print("   cleaned up the payment method")
    if made_customer:
        await stripe.Customer.delete_async(customer_id)
        print("   cleaned up the customer")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
