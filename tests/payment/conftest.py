import sys, os
import pytest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_svc = os.path.join(_root, "services", "payment")

_BARE_NAMES = ["models", "handler", "saga", "compensation", "service", "repository",
               "idempotency", "stripe_client", "email_client"]


@pytest.fixture(autouse=True)
def _payment_sys_path():
    """Ensure services/payment is first on sys.path and bare modules are fresh."""
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

    for _name in _BARE_NAMES:
        sys.modules.pop(_name, None)

    yield

    for _name in _BARE_NAMES:
        sys.modules.pop(_name, None)
    try:
        sys.path.remove(_svc)
    except ValueError:
        pass
