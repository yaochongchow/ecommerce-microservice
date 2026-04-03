import sys, os
import pytest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_svc = os.path.join(_root, "services", "inventory")

_BARE_NAMES = ["models", "handler", "saga", "compensation", "service", "repository",
               "idempotency", "stripe_client", "email_client"]


@pytest.fixture(autouse=True)
def _inventory_sys_path():
    """Ensure services/inventory is first on sys.path and bare modules are fresh."""
    # Put this service dir at the front so bare imports resolve here
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

    # Clear any stale bare module names from other service test suites
    for _name in _BARE_NAMES:
        sys.modules.pop(_name, None)

    yield

    # Clean up after the test to avoid polluting other service suites
    for _name in _BARE_NAMES:
        sys.modules.pop(_name, None)
    try:
        sys.path.remove(_svc)
    except ValueError:
        pass
