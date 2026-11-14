from typing import Literal, Optional

import pytest

from bot.actions import registry as r


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(r.REGISTRY)
    r.REGISTRY.clear()
    yield
    r.REGISTRY.clear()
    r.REGISTRY.update(saved)


def test_schema_and_run():
    @r.action("demo", "Demo action.", params={"n": "a number"})
    async def demo(n: int, mode: Literal["a", "b"] = "a", tag: Optional[str] = None) -> r.ActionResult:
        return r.ActionResult(text=f"{n}{mode}{tag}")

    sch = r.REGISTRY["demo"].input_schema
    assert sch["required"] == ["n"]
    assert sch["properties"]["mode"]["enum"] == ["a", "b"]
    assert sch["properties"]["n"]["description"] == "a number"
    assert r.REGISTRY["demo"].tool_schema()["name"] == "demo"


async def test_run_action_validation_and_errors():
    @r.action("boom", "Fails.", dangerous=True)
    async def boom(x: int) -> r.ActionResult:
        raise RuntimeError("kaboom")

    res = await r.run_action("boom", x="3")
    assert not res.ok and "kaboom" in res.text
    res = await r.run_action("boom", y=1)
    assert not res.ok and "unexpected" in res.text
    assert "DANGEROUS" in r.REGISTRY["boom"].tool_schema()["description"]


def test_rejects_bad_defs():
    with pytest.raises(TypeError):
        @r.action("sync_one", "x")
        def sync_one() -> r.ActionResult: ...
    with pytest.raises(ValueError):
        r.action("Bad-Name", "x")
