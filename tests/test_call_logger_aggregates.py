import asyncio
from decimal import Decimal
from types import SimpleNamespace

from app.services.call_logger import CallLoggerService, build_api_key_usage_by_model


class _FakeResult:
    def __init__(self, *, one_row=None, all_rows=None):
        self._one_row = one_row
        self._all_rows = list(all_rows or [])

    def one(self):
        return self._one_row

    def all(self):
        return list(self._all_rows)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.execute_calls = 0
        self.executed_queries = []

    async def execute(self, query):
        if not self._results:
            raise AssertionError("unexpected execute() call")
        self.execute_calls += 1
        self.executed_queries.append(str(query))
        return self._results.pop(0)


def test_build_api_key_usage_by_model_supports_grouped_rows():
    usage = build_api_key_usage_by_model(
        [
            SimpleNamespace(
                model_group="BookmarkVault",
                requests=3,
                input_tokens=120,
                output_tokens=45,
                total_tokens=165,
                total_cost=Decimal("1.500000"),
            )
        ],
        allowed_models=["BookmarkVault", "ipfs-file-manager"],
    )

    assert usage["BookmarkVault"] == {
        "requests": 3,
        "input_tokens": 120,
        "output_tokens": 45,
        "tokens": 165,
        "cost": "1.5",
    }
    assert usage["ipfs-file-manager"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tokens": 0,
        "cost": "0",
    }


def test_get_log_stats_sums_cost_in_python():
    service = CallLoggerService()
    session = _FakeSession(
        [
            _FakeResult(one_row=(4, 3, 100, 50, 150)),
            _FakeResult(all_rows=[("0.100000",), ("0.250000",), (None,)]),
        ]
    )

    stats = asyncio.run(service.get_log_stats(session, hours=24))

    assert session.execute_calls == 2
    assert stats["total_calls"] == 4
    assert stats["success_calls"] == 3
    assert stats["error_calls"] == 1
    assert stats["total_cost"] == "0.350000"


def test_get_total_cost_sums_all_cost_columns_in_python():
    service = CallLoggerService()
    session = _FakeSession(
        [
            _FakeResult(
                all_rows=[
                    ("1.100000", "2.200000", "3.300000"),
                    ("0.400000", None, "0.400000"),
                ]
            ),
        ]
    )

    cost = asyncio.run(service.get_total_cost(session, hours=24))

    assert session.execute_calls == 1
    assert cost == {
        "input": "1.500000",
        "output": "2.200000",
        "total": "3.700000",
    }


def test_get_api_key_usage_by_model_aggregates_rows_in_python():
    service = CallLoggerService()
    session = _FakeSession(
        [
            _FakeResult(
                all_rows=[
                    SimpleNamespace(
                        model_group="BookmarkVault",
                        input_tokens=400,
                        output_tokens=220,
                        total_tokens=620,
                        total_cost=Decimal("4.560000"),
                    ),
                    SimpleNamespace(
                        model_group="ipfs-file-manager",
                        input_tokens=1500,
                        output_tokens=700,
                        total_tokens=2200,
                        total_cost=Decimal("12.340000"),
                    ),
                    SimpleNamespace(
                        model_group="BookmarkVault",
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                        total_cost=Decimal("0.100000"),
                    ),
                ]
            )
        ]
    )

    usage = asyncio.run(
        service.get_api_key_usage_by_model(
            session,
            "key-123",
            allowed_models=["BookmarkVault", "ipfs-file-manager", "unused-model"],
        )
    )

    assert session.execute_calls == 1
    assert usage["BookmarkVault"] == {
        "requests": 2,
        "input_tokens": 410,
        "output_tokens": 225,
        "tokens": 635,
        "cost": "4.66",
    }
    assert usage["ipfs-file-manager"] == {
        "requests": 1,
        "input_tokens": 1500,
        "output_tokens": 700,
        "tokens": 2200,
        "cost": "12.34",
    }
    assert usage["unused-model"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tokens": 0,
        "cost": "0",
    }
