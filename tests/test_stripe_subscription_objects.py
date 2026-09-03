"""stripe_service · reading the objects Stripe actually hands back.

The SDK's objects are dict-like right up until they are not. A subscription's
``items`` is a ``ListObject``, which deliberately raises on ``.get`` to stop
exactly the mistake that reads like ordinary dict access:

    'get' is a dict method, but a ListObject is not a dict.

That error only ever appears against a real Stripe account, so the fakes here
copy the behaviour rather than the shape - a plain dict would let the bug
through.
"""
import pytest

from app.core.enums import BillingCycle, PlanCode
from app.services import stripe_service


class ListObject(dict):
    """A stand-in for stripe.ListObject: `.data` yes, `.get` never."""

    def __init__(self, data):
        super().__init__(data=data)
        self.data = data

    def get(self, *args, **kwargs):
        raise TypeError(
            "'get' is a dict method, but a ListObject is not a dict. "
            "Use .to_dict() to convert it."
        )


class Subscription(dict):
    """StripeObject is a dict subclass, so `.get` here is genuinely fine."""


def _subscription(price_id="price_old", *, period_end=1893456000, on_item=False):
    item = Subscription({"id": "si_1", "price": {"id": price_id}})
    if on_item:
        item["current_period_end"] = period_end
    sub = Subscription({"id": "sub_1", "status": "active",
                        "items": ListObject([item])})
    if not on_item:
        sub["current_period_end"] = period_end
    return sub


class FakeStripe:
    def __init__(self, current, updated=None):
        self._current, self._updated = current, updated
        self.modify_kwargs = None

    class _Sub:
        pass

    @property
    def Subscription(self):
        outer = self

        class _S:
            @staticmethod
            async def retrieve_async(subscription_id):
                return outer._current

            @staticmethod
            async def modify_async(subscription_id, **kwargs):
                outer.modify_kwargs = kwargs
                return outer._updated

        return _S


@pytest.fixture
def stripe_ready(monkeypatch):
    monkeypatch.setattr(stripe_service, "configured", lambda: True)
    monkeypatch.setattr(stripe_service, "price_id",
                        lambda plan_code, billing_cycle: "price_new")

    def install(fake):
        monkeypatch.setattr(stripe_service, "_client", lambda: fake)
        return fake

    return install


async def test_a_list_object_subscription_item_is_read_not_get(stripe_ready):
    """The regression: `.get("data")` on items blew up before reaching Stripe."""
    updated = _subscription("price_new")
    updated["latest_invoice"] = {"id": "in_1"}
    fake = stripe_ready(FakeStripe(_subscription("price_old"), updated))

    result = await stripe_service.change_subscription_plan(
        subscription_id="sub_1", plan_code=PlanCode.ENTERPRISE,
        billing_cycle=BillingCycle.ANNUAL)

    assert result["success"] is True
    assert result["latest_invoice_id"] == "in_1"
    assert result["current_period_end"] == 1893456000
    # The item id came off the ListObject, and the upgrade was billed.
    assert fake.modify_kwargs["items"] == [{"id": "si_1", "price": "price_new"}]
    assert fake.modify_kwargs["proration_behavior"] == "always_invoice"


async def test_the_period_is_read_off_the_item_when_stripe_moved_it(stripe_ready):
    """Newer API versions carry the period on the item, not the subscription."""
    updated = _subscription("price_new", period_end=1900000000, on_item=True)
    updated["latest_invoice"] = {"id": "in_2"}
    stripe_ready(FakeStripe(_subscription("price_old"), updated))

    result = await stripe_service.change_subscription_plan(
        subscription_id="sub_1", plan_code=PlanCode.ENTERPRISE,
        billing_cycle=BillingCycle.ANNUAL)

    assert result["current_period_end"] == 1900000000


async def test_an_override_switches_the_price_without_billing(stripe_ready):
    updated = _subscription("price_new")
    fake = stripe_ready(FakeStripe(_subscription("price_old"), updated))

    await stripe_service.change_subscription_plan(
        subscription_id="sub_1", plan_code=PlanCode.ENTERPRISE,
        billing_cycle=BillingCycle.ANNUAL, charge_now=False)

    assert fake.modify_kwargs["proration_behavior"] == "none"


async def test_moving_to_the_price_it_is_already_on_invoices_nothing(stripe_ready):
    fake = stripe_ready(FakeStripe(_subscription("price_new")))

    result = await stripe_service.change_subscription_plan(
        subscription_id="sub_1", plan_code=PlanCode.ENTERPRISE,
        billing_cycle=BillingCycle.ANNUAL)

    assert result["success"] is True
    assert result["message"] == "Already on this price"
    assert fake.modify_kwargs is None


async def test_a_subscription_with_no_items_is_reported_not_crashed(stripe_ready):
    empty = Subscription({"id": "sub_1", "status": "active", "items": ListObject([])})
    stripe_ready(FakeStripe(empty))

    result = await stripe_service.change_subscription_plan(
        subscription_id="sub_1", plan_code=PlanCode.ENTERPRISE,
        billing_cycle=BillingCycle.ANNUAL)

    assert result["success"] is False
    assert "no billable item" in result["message"]
