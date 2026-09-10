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


class StripeObject:
    """What stripe>=12 actually returns: attributes and `[...]`, no `.get`.

    Modelling it as a dict - which it was, years ago - is what let
    `.get("current_period_end")` reach production and raise
    `AttributeError: get` on the first real call.
    """

    def __init__(self, fields):
        self._fields = dict(fields)

    def __getattr__(self, name):
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name):
        return self._fields[name]

    def __setitem__(self, name, value):
        self._fields[name] = value


class ListObject(StripeObject):
    """`.data` yes, `.get` never - the wrapper raises on it deliberately."""

    def __init__(self, data):
        super().__init__({"data": data})

    def get(self, *args, **kwargs):
        raise TypeError(
            "'get' is a dict method, but a ListObject is not a dict. "
            "Use .to_dict() to convert it."
        )


class Subscription(StripeObject):
    """A subscription as the SDK hands it over."""


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


class TestCachedIdsBelongToOneAccount:
    """`No such price: price_...`, reported from a customer's payment screen.

    Products and Prices are created on demand and their ids cached in Mongo, so
    a second signup on the same plan reuses them instead of piling up duplicates
    in Stripe. But an id only means anything inside the account that issued it,
    and the cache key was the plan alone - so pointing STRIPE_SECRET_KEY at a
    different account left it confidently handing back ids from the old one.

    Nothing in the failure said "stale cache": the message names a price that
    looks perfectly real, and the keys, the currency and the plan are all
    correct. Scoping the key to the account is what makes the swap a cache miss
    instead of a lie.
    """

    def _scope_for(self, key, monkeypatch):
        from app.services import stripe_service

        monkeypatch.setattr(stripe_service.settings, "STRIPE_SECRET_KEY", key)
        return stripe_service._account_scope()

    def test_two_accounts_do_not_share_a_cache_entry(self, monkeypatch):
        first = self._scope_for("sk_test_aaaaaaaaaaaaaaaa", monkeypatch)
        second = self._scope_for("sk_test_bbbbbbbbbbbbbbbb", monkeypatch)

        assert first != second

    def test_the_same_account_keeps_reusing_its_own(self, monkeypatch):
        # Otherwise every signup would create another Product and Price, which
        # is what the cache exists to prevent.
        once = self._scope_for("sk_test_aaaaaaaaaaaaaaaa", monkeypatch)
        again = self._scope_for("sk_test_aaaaaaaaaaaaaaaa", monkeypatch)

        assert once == again

    def test_the_secret_key_is_not_stored_in_the_id(self, monkeypatch):
        # These ids are written to the database and read back by the admin
        # screens; the key must not travel with them.
        key = "sk_test_51ThisIsTheActualSecret"
        scope = self._scope_for(key, monkeypatch)

        assert key not in scope
        assert "sk_test" not in scope
        assert len(scope) == 12
