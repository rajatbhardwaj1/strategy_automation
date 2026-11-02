"""
Import-time stubs for the offline unit tests.

src.strategy / src.utils pull in the broker SDKs (SmartApi, NorenRestApiPy),
yfinance and python-telegram-bot at module load. None of them are needed to
exercise the pure sizing / formatting logic, and they are not installed in
every environment, so any module that is genuinely missing is replaced by a
stub before the test modules import src.*. Where the real package IS installed
it is left completely alone, so the live integration tests are unaffected.
"""
import sys
import types
from unittest.mock import MagicMock

_OPTIONAL_MODULES = (
    "yfinance",
    "pyotp",
    "telegram",
    "SmartApi",
    "NorenRestApiPy",
    "NorenRestApiPy.NorenApi",
)


class _StubModule(types.ModuleType):
    """Module whose every attribute resolves to a harmless mock."""

    def __getattr__(self, name):
        return MagicMock(name=f"{self.__name__}.{name}")


for _name in _OPTIONAL_MODULES:
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = _StubModule(_name)
