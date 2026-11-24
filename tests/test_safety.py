"""Phase-1 guard against private Binance USD-M and authenticated API usage."""

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from baqs.api import create_app
from baqs.settings import Settings


_PRIVATE_PATH = re.compile(
    r"/fapi/v\\d+/(?:account|balance|positionrisk|positionside/dual|leverage|margintype|"
    r"order|openorders|allorders|algoorder|openalgoorders|allalgoorders|listenkey|"
    r"usertrades|income|forceorders|commissionrate|positionmargin|apitradingstatus)"
    r"(?:$|[/?#])",
    re.IGNORECASE,
)
_API_KEY_HEADER = re.compile(r"x-mbx-api[-_]?key", re.IGNORECASE)
_SECRET_NAMES = {"api_key", "api_secret", "apikey", "apisecret", "signature"}


def private_api_violations(source: str, filename: str = "<source>") -> list[str]:
    """Return private/auth constructs found in executable Python syntax.

    Comments and docstrings are intentionally excluded. This is a scoped
    source guard, not a complete static-analysis or runtime security boundary.
    """
    tree = ast.parse(source, filename=filename)
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_ids.add(id(node.body[0].value))

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = (
                [alias.name.lower() for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module.lower() if node.module else ""]
            )
            if any(name == "hmac" or name.startswith("binance.client") for name in imported):
                violations.append(f"authenticated/private client import at line {node.lineno}")

        if isinstance(node, ast.Name) and node.id.lower() in _SECRET_NAMES:
            violations.append(f"authentication identifier {node.id!r} at line {node.lineno}")

        if isinstance(node, ast.Attribute) and node.attr.lower() in _SECRET_NAMES:
            violations.append(f"authentication attribute {node.attr!r} at line {node.lineno}")

        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            literal = node.value
            if _PRIVATE_PATH.search(literal):
                violations.append(f"private USD-M endpoint literal at line {node.lineno}")
            if _API_KEY_HEADER.search(literal):
                violations.append(f"Binance API-key header literal at line {node.lineno}")
            if literal.lower() in {"listenkey", "userdata", "userdatastream"}:
                violations.append(f"user-data-stream literal at line {node.lineno}")

    return violations


def test_trading_mode_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_enabled=True)


def test_routes_are_read_only() -> None:
    app = create_app(settings=Settings(_env_file=None, database_url="sqlite:///:memory:"))
    schema = TestClient(app).get("/openapi.json").json()
    assert all(set(methods).issubset({"get"}) for methods in schema["paths"].values())


def test_no_exchange_secrets_or_execution_module() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "baqs"
    assert not (root / "execution").exists()
    assert not (root / "exchange" / "binance_private.py").exists()

    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(
            f"{path.relative_to(root)}: {item}"
            for item in private_api_violations(path.read_text(encoding="utf-8"), str(path))
        )
    assert not violations, "\\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "PATH = '/fapi/v2/account'",
        "PATH = '/fapi/v2/balance'",
        "PATH = '/fapi/v2/positionRisk'",
        "PATH = '/fapi/v1/leverage'",
        "PATH = '/fapi/v1/order'",
        "PATH = '/fapi/v1/algoOrder'",
        "PATH = '/fapi/v1/listenKey'",
        "HEADERS = {'X-MBX-APIKEY': token}",
        "api_key = token",
        "api_secret = secret",
        "signature = value",
        "import hmac",
    ],
)
def test_private_and_authenticated_patterns_are_rejected(source: str) -> None:
    assert private_api_violations(source)


@pytest.mark.parametrize(
    "endpoint",
    [
        "/fapi/v1/time",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/klines",
        "/fapi/v1/fundingRate",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/depth",
        "/fapi/v1/trades",
        "/fapi/v1/aggTrades",
    ],
)
def test_public_market_data_routes_remain_allowed(endpoint: str) -> None:
    assert not private_api_violations(f"ENDPOINT = {endpoint!r}")


def test_comments_and_docstrings_do_not_trigger_the_guard() -> None:
    source = '''# /fapi/v1/order
"""Never call /fapi/v1/account or use X-MBX-APIKEY."""
def note() -> str:
    """Do not use /fapi/v1/listenKey."""
    return "/fapi/v1/klines"
'''
    assert not private_api_violations(source)
