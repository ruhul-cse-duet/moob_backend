"""stripe_service · saving and choosing a card.

Stripe will not make a card the default until it is attached to that customer:

    The customer does not have a payment method with the ID pm_...
    The payment method must be attached to the customer.

A card the browser has just tokenised is attached to nobody, so a screen that
saves and selects in one gesture walks straight into it. The fake below
enforces that rule the way Stripe does, because a fake that simply accepts
everything would let the bug back in.
"""
import pytest

from app.services import stripe_service


class StripeError(Exception):
    pass


class FakeStripe:
    """Enough of Stripe to enforce the attach-before-default rule."""

    def __init__(self, *, methods=None, customers=("cus_1",)):
        # payment method id -> the customer it is attached to, or None
        self.methods = dict(methods or {"pm_new": None})
        self.customers = {c: {"invoice_settings": {}} for c in customers}
        self.attached_calls = []
        # Set to make attach land somewhere other than the customer asked for,
        # which is the one thing the caller cannot detect from a success.
        self.attach_lands_on = None
        # Stripe's test cards: using one produces a new PaymentMethod id.
        self.aliases = {}

    @property
    def PaymentMethod(self):
        outer = self

        class _PM:
            @staticmethod
            async def retrieve_async(pm_id):
                if pm_id not in outer.methods:
                    raise StripeError(f"No such PaymentMethod: {pm_id}")
                return type("PM", (), {"id": pm_id,
                                       "customer": outer.methods[pm_id],
                                       "card": {"brand": "visa", "last4": "4242",
                                                "exp_month": 2, "exp_year": 2028}})()

            @staticmethod
            async def attach_async(pm_id, customer=None):
                owner = outer.methods.get(pm_id)
                if owner and owner != customer:
                    raise StripeError("The payment method you supplied is "
                                      "already attached to a customer.")
                # A test alias mints a fresh PaymentMethod every time it is
                # used, so what comes back is not what went in.
                real_id = outer.aliases.get(pm_id, pm_id)
                if pm_id in outer.aliases:
                    outer.methods[real_id] = None
                outer.methods[real_id] = outer.attach_lands_on or customer
                outer.attached_calls.append((real_id, customer))
                return await _PM.retrieve_async(real_id)

        return _PM

    @property
    def Customer(self):
        outer = self

        class _C:
            @staticmethod
            async def modify_async(customer_id, invoice_settings=None, **kwargs):
                wanted = (invoice_settings or {}).get("default_payment_method")
                # The rule that produced the bug report.
                if wanted and outer.methods.get(wanted) != customer_id:
                    raise StripeError(
                        f"The customer does not have a payment method with the "
                        f"ID {wanted}. The payment method must be attached to "
                        f"the customer."
                    )
                outer.customers[customer_id]["invoice_settings"] = invoice_settings
                return outer.customers[customer_id]

            @staticmethod
            async def retrieve_async(customer_id):
                return type("C", (), outer.customers[customer_id])()

        return _C


@pytest.fixture
def stripe_ready(monkeypatch):
    monkeypatch.setattr(stripe_service, "configured", lambda: True)

    def install(fake):
        monkeypatch.setattr(stripe_service, "_client", lambda: fake)
        return fake

    return install


async def test_choosing_a_freshly_tokenised_card_attaches_it_first(stripe_ready):
    """The reported bug: save-and-select on a card Stripe has never seen saved."""
    fake = stripe_ready(FakeStripe(methods={"pm_new": None}))

    result = await stripe_service.set_default_payment_method("cus_1", "pm_new")

    assert result["success"] is True, result["message"]
    assert fake.attached_calls == [("pm_new", "cus_1")]
    assert fake.customers["cus_1"]["invoice_settings"] == {
        "default_payment_method": "pm_new"}


async def test_choosing_a_card_already_on_file_does_not_reattach_it(stripe_ready):
    fake = stripe_ready(FakeStripe(methods={"pm_saved": "cus_1"}))

    result = await stripe_service.set_default_payment_method("cus_1", "pm_saved")

    assert result["success"] is True
    assert fake.attached_calls == []


async def test_another_workspaces_card_is_refused_in_plain_words(stripe_ready):
    fake = stripe_ready(FakeStripe(methods={"pm_theirs": "cus_other"}))

    result = await stripe_service.set_default_payment_method("cus_1", "pm_theirs")

    assert result["success"] is False
    assert "different billing account" in result["message"]
    # Nothing was attempted at Stripe, so nothing to undo.
    assert fake.attached_calls == []


async def test_saving_a_new_card_attaches_it_and_makes_it_default(stripe_ready):
    fake = stripe_ready(FakeStripe(methods={"pm_new": None}))

    result = await stripe_service.attach_payment_method("cus_1", "pm_new")

    assert result["success"] is True
    assert result["card"]["last4"] == "4242"
    assert result["card"]["is_default"] is True
    assert fake.customers["cus_1"]["invoice_settings"] == {
        "default_payment_method": "pm_new"}


async def test_saving_the_same_card_twice_is_not_an_error(stripe_ready):
    """A double-submitted form should leave one card, not a failure."""
    fake = stripe_ready(FakeStripe(methods={"pm_new": None}))

    first = await stripe_service.attach_payment_method("cus_1", "pm_new")
    second = await stripe_service.attach_payment_method("cus_1", "pm_new")

    assert first["success"] and second["success"]
    assert fake.methods["pm_new"] == "cus_1"


async def test_saving_another_workspaces_card_is_refused_before_stripe_is_asked(
        stripe_ready):
    fake = stripe_ready(FakeStripe(methods={"pm_theirs": "cus_other"}))

    result = await stripe_service.attach_payment_method("cus_1", "pm_theirs")

    assert result["success"] is False
    assert "different billing account" in result["message"]
    assert fake.attached_calls == []


async def test_a_workspace_with_no_billing_account_says_so(stripe_ready):
    stripe_ready(FakeStripe())

    for result in (await stripe_service.set_default_payment_method("", "pm_new"),
                   await stripe_service.attach_payment_method("", "pm_new")):
        assert result["success"] is False
        assert "no billing account" in result["message"]


async def test_a_card_stripe_did_not_actually_attach_is_caught_here(stripe_ready):
    """Rather than letting Stripe refuse the default with a message that names
    neither the customer nor the call that asked."""
    fake = stripe_ready(FakeStripe(methods={"pm_new": None}))
    fake.attach_lands_on = "cus_somewhere_else"

    result = await stripe_service.attach_payment_method("cus_1", "pm_new")

    assert result["success"] is False
    assert "did not save that card to this workspace" in result["message"]


async def test_a_test_alias_is_saved_under_the_id_stripe_gives_back(stripe_ready):
    """`pm_card_visa` mints a new PaymentMethod on every use.

    Passing the alias on to the next call names a card nobody attached, and
    Stripe answers "the customer does not have a payment method with the ID
    pm_..." - naming an id the caller has never seen.
    """
    fake = stripe_ready(FakeStripe(methods={"pm_card_visa": None}))
    fake.aliases = {"pm_card_visa": "pm_1RealCard"}

    result = await stripe_service.attach_payment_method("cus_1", "pm_card_visa")

    assert result["success"] is True, result["message"]
    # The default is the card that exists, not the alias that made it.
    assert fake.customers["cus_1"]["invoice_settings"] == {
        "default_payment_method": "pm_1RealCard"}
    assert result["card"]["id"] == "pm_1RealCard"
    assert result["card"]["is_default"] is True


async def test_choosing_by_alias_also_lands_on_the_real_card(stripe_ready):
    fake = stripe_ready(FakeStripe(methods={"pm_card_visa": None}))
    fake.aliases = {"pm_card_visa": "pm_1RealCard"}

    result = await stripe_service.set_default_payment_method("cus_1", "pm_card_visa")

    assert result["success"] is True, result["message"]
    assert fake.customers["cus_1"]["invoice_settings"] == {
        "default_payment_method": "pm_1RealCard"}
