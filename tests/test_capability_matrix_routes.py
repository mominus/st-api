import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import app.services.pricing as pricing_module
from app.routers import admin as admin_router_module
from app.routers import openai as openai_router_module


class _FakeScalarCollection:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _FakeExecuteResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _FakeScalarCollection(self._scalars)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        return self._results.pop(0)


class _FakeAdminAccountPool:
    async def get_accounts_by_model_groups(self, _session, _groups):
        return {
            "claude-sonnet-4-5": [
                SimpleNamespace(status="active", daily_quota=12000, daily_used=2000)
            ]
        }


class _FakeModelGroupStatsPool:
    async def get_model_group_stats(self, _session, _group_name):
        return {"accounts": 1, "active_accounts": 1}


class _FakePricingService:
    def get_pricing(self, _group_name):
        return {"input": 1.0, "output": 2.0}


class _FakeAPIKeyService:
    def __init__(self, api_key):
        self._api_key = api_key

    async def get_key_by_raw(self, _session, raw_key):
        if raw_key == "sk-test":
            return self._api_key
        return None

    def get_model_groups(self, _api_key):
        return ["claude-sonnet-4-5"]


class _FakeCallLogger:
    async def get_api_key_usage_by_model(self, _session, _key_id, **_kwargs):
        return {
            "claude-sonnet-4-5": {
                "tokens": 4321,
            }
        }


class _FakeRuntimeAccountPool:
    async def get_accounts_by_model_groups(self, _session, _groups):
        return {
            "claude-sonnet-4-5": [
                SimpleNamespace(status="active", daily_quota=12000, daily_used=2000)
            ]
        }


class _FakeRuntime:
    def __init__(self):
        self.account_pool = _FakeRuntimeAccountPool()

    async def run_db_guarded(self, _session, func):
        return await func()


def test_admin_list_model_groups_exposes_capability_matrix(monkeypatch):
    group = SimpleNamespace(
        id="group-1",
        name="claude-sonnet-4-5",
        description="Claude-compatible workflow",
        input_mapping=json.dumps(
            {
                "system_prompt": "in-system",
                "chat_history": "in-history",
            }
        ),
        capability_overrides=json.dumps(
            {
                "image_input": {
                    "status": "simulated",
                    "detail": "Image blocks are adapted by a workflow-side bridge.",
                }
            }
        ),
        created_at=datetime(2026, 4, 20, 12, 0, 0),
    )
    session = _FakeSession([_FakeExecuteResult(scalars=[group])])

    monkeypatch.setattr(
        admin_router_module,
        "get_account_pool_service",
        lambda: _FakeAdminAccountPool(),
    )
    monkeypatch.setattr(
        pricing_module,
        "get_pricing_service",
        lambda: _FakePricingService(),
    )

    response = asyncio.run(
        admin_router_module.list_model_groups(
            admin={"username": "tester"},
            session=session,
        )
    )

    item = response["groups"][0]
    matrix = item["capability_matrix"]

    assert response["success"] is True
    assert item["capability_overrides"]["image_input"]["status"] == "simulated"
    assert matrix["model"] == "claude-sonnet-4-5"
    assert matrix["capabilities"]["system_prompt"]["status"] == "native"
    assert matrix["capabilities"]["system_prompt"]["mapped_field"] == "in-system"
    assert matrix["capabilities"]["chat_history"]["status"] == "native"
    assert matrix["capabilities"]["tool_use"]["status"] == "simulated"
    assert sum(matrix["summary"].values()) == len(matrix["capabilities"])


def test_openai_key_info_exposes_capability_matrix_from_model_group_settings(monkeypatch):
    api_key = SimpleNamespace(
        id="key-1",
        status="active",
        quota=None,
        used=0,
        request_quota=None,
        total_requests=0,
        token_quota=None,
        total_tokens=4321,
        cost_limit=None,
        total_cost="0",
        expires_at=None,
        created_at=datetime(2026, 4, 20, 12, 0, 0),
    )
    group = SimpleNamespace(
        name="claude-sonnet-4-5",
        input_mapping=json.dumps(
            {
                "system_prompt": "in-system",
                "metadata": "in-meta",
                "thinking": "in-thinking",
            }
        ),
        capability_overrides=json.dumps(
            {
                "advanced_tool_use": {
                    "status": "simulated",
                    "detail": "Advanced tool orchestration remains gateway-managed.",
                }
            }
        ),
    )
    session = _FakeSession([_FakeExecuteResult(scalars=[group])])

    monkeypatch.setattr(
        openai_router_module,
        "get_api_key_service",
        lambda: _FakeAPIKeyService(api_key),
    )
    monkeypatch.setattr(
        openai_router_module,
        "get_call_logger_service",
        lambda: _FakeCallLogger(),
    )
    monkeypatch.setattr(
        openai_router_module,
        "get_gateway_runtime",
        lambda: _FakeRuntime(),
    )

    response = asyncio.run(
        openai_router_module.get_current_key_info(
            authorization=None,
            x_api_key=None,
            api_key_query=None,
            key_query="sk-test",
            session=session,
        )
    )

    model = response["models"][0]
    matrix = model["capability_matrix"]

    assert model["model"] == "claude-sonnet-4-5"
    assert matrix["model"] == "claude-sonnet-4-5"
    assert matrix["capabilities"]["system_prompt"]["status"] == "native"
    assert matrix["capabilities"]["system_prompt"]["mapped_field"] == "in-system"
    assert matrix["capabilities"]["metadata"]["status"] == "native"
    assert matrix["capabilities"]["metadata"]["mapped_field"] == "in-meta"
    assert matrix["capabilities"]["thinking"]["status"] == "simulated"
    assert matrix["capabilities"]["thinking"]["mapped_field"] == "in-thinking"
    assert matrix["capabilities"]["advanced_tool_use"]["status"] == "simulated"
    assert "gateway-managed" in matrix["capabilities"]["advanced_tool_use"]["detail"]
