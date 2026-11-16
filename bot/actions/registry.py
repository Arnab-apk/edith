"""Action registry - the single source of truth shared by slash commands AND Edith.

CONTRACT (every file in bot/actions/ follows this):

    from .registry import action, ActionResult, ActionFile

    @action("procs", "List the top processes by CPU or memory usage.",
            category="system", params={"sort": "cpu or mem", "limit": "how many rows"})
    async def procs(sort: Literal["cpu", "mem"] = "cpu", limit: int = 15) -> ActionResult:
        rows = await run_blocking(_collect, sort, limit)
        return ActionResult(text=rows)

Rules:
  * Actions are PURE async functions: no `telegram` imports, no Update/context objects.
    Blocking work goes through bot.utils.run_blocking.
  * Parameters must be keyword-friendly and type-annotated with str/int/float/bool,
    Literal[...], list[str], or Optional of those. Defaults make a parameter optional.
    The JSON schema for Claude tools is generated from the signature automatically.
  * dangerous=True for anything destructive or that touches input/power/processes/files
    (shell, delete, kill, power, move, send-keys/type, ...). The ACTION never asks for
    confirmation itself - callers (modules via bot.confirm, Edith brain) must confirm
    before calling `run_action` on a dangerous action.
  * Return ActionResult. `text` is what the user AND the model see; keep it concise.
    Put screenshots/zips/recordings in `files`. On expected failure, return
    ActionResult(text="...", ok=False) instead of raising.
  * Action names are unique, lowercase snake_case: [a-z][a-z0-9_]{0,63}.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import re
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

log = logging.getLogger("actions")
_audit = logging.getLogger("audit")  # handler (rotating, secret-redacting) is set up in bot.security

FileKind = Literal["document", "photo", "video", "voice", "audio"]


@dataclass
class ActionFile:
    """A file produced by an action. Provide either `path` or `data`."""
    path: Path | None = None
    data: bytes | None = None
    filename: str = "file.bin"
    kind: FileKind = "document"
    caption: str = ""
    delete_after_send: bool = False  # True for temp files the sender should remove

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("ActionFile needs exactly one of path or data")
        if self.path is not None:
            self.path = Path(self.path)
            if self.filename == "file.bin":
                self.filename = self.path.name


@dataclass
class ActionResult:
    text: str = ""
    files: list[ActionFile] = field(default_factory=list)
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)  # optional structured payload
    code: bool = False  # render text as monospace <pre> in Telegram (shell output etc.)


ActionFunc = Callable[..., Awaitable[ActionResult]]


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    func: ActionFunc
    dangerous: bool
    category: str
    input_schema: dict[str, Any]

    def tool_schema(self) -> dict[str, Any]:
        """Anthropic tool definition ({name, description, input_schema})."""
        desc = self.description
        if self.dangerous:
            desc += " (DANGEROUS: the owner will be asked to confirm before it runs.)"
        return {"name": self.name, "description": desc, "input_schema": self.input_schema}


REGISTRY: dict[str, Action] = {}
_EVER: dict[str, Action] = {}  # every real action ever registered (survives REGISTRY.clear())

# Modules under bot/actions/ that load_all() imports. Missing ones are skipped with a warning.
ACTION_MODULES = ["system", "power", "shell", "files", "desktop", "media", "apps", "automation",
                  "windows", "pc_settings", "reader", "software", "web", "notes", "everyday", "devices"]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# ---------------------------------------------------------------- schema generation
def _type_schema(tp: Any) -> dict[str, Any]:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is Literal:
        vals = list(args)
        base = {str: "string", int: "integer", float: "number", bool: "boolean"}[type(vals[0])]
        return {"type": base, "enum": vals}
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_schema(non_none[0])
        raise TypeError(f"unsupported union {tp!r}")
    if origin in (list, tuple):
        item = args[0] if args else str
        return {"type": "array", "items": _type_schema(item)}
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    raise TypeError(f"unsupported parameter type {tp!r}")


def build_schema(func: Callable, params: dict[str, str] | None = None) -> dict[str, Any]:
    params = params or {}
    hints = typing.get_type_hints(func)
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, p in inspect.signature(func).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(f"{func.__name__}: *args/**kwargs not allowed in actions")
        if pname not in hints:
            raise TypeError(f"{func.__name__}: parameter {pname!r} needs a type annotation")
        sch = _type_schema(hints[pname])
        if pname in params:
            sch["description"] = params[pname]
        if p.default is inspect.Parameter.empty:
            required.append(pname)
        elif p.default is not None:
            sch["default"] = p.default
        props[pname] = sch
    unknown = set(params) - set(props)
    if unknown:
        raise TypeError(f"{func.__name__}: params docs for unknown parameters {sorted(unknown)}")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ---------------------------------------------------------------- decorator / lookup
def action(name: str, description: str, *, dangerous: bool = False, category: str = "",
           params: dict[str, str] | None = None) -> Callable[[ActionFunc], ActionFunc]:
    """Register an async action. `params` maps parameter name -> description for the model."""
    if not _NAME_RE.match(name):
        raise ValueError(f"bad action name {name!r}")

    def deco(func: ActionFunc) -> ActionFunc:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"action {name!r} must be async def")
        if name in REGISTRY and REGISTRY[name].func is not func:
            raise ValueError(f"duplicate action name {name!r}")
        REGISTRY[name] = Action(name=name, description=description.strip(), func=func,
                                dangerous=dangerous, category=category,
                                input_schema=build_schema(func, params))
        if func.__module__.startswith("bot.actions."):
            _EVER[name] = REGISTRY[name]
        return func

    return deco


def get_action(name: str) -> Action:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown action {name!r}") from None


def _coerce(value: Any, sch: dict[str, Any]) -> Any:
    t = sch.get("type")
    if t == "integer" and isinstance(value, str):
        value = int(value)
    elif t == "number" and isinstance(value, (str, int)) and not isinstance(value, bool):
        value = float(value)
    elif t == "boolean" and isinstance(value, str):
        value = value.strip().lower() in ("1", "true", "yes", "on", "y")
    if "enum" in sch and value not in sch["enum"]:
        raise ValueError(f"must be one of {sch['enum']}")
    return value


def validate_args(act: Action, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Check/coerce kwargs against the action's schema. Raises ValueError with a readable message."""
    props = act.input_schema["properties"]
    extra = set(kwargs) - set(props)
    if extra:
        raise ValueError(f"{act.name}: unexpected argument(s) {sorted(extra)}")
    missing = [r for r in act.input_schema["required"] if r not in kwargs]
    if missing:
        raise ValueError(f"{act.name}: missing argument(s) {missing}")
    out = {}
    for k, v in kwargs.items():
        try:
            out[k] = v if v is None else _coerce(v, props[k])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{act.name}: bad value for {k!r}: {e}") from None
    return out


async def run_action(name: str, /, **kwargs: Any) -> ActionResult:
    """Validate and run an action. Does NOT confirm dangerous actions - the caller must.
    Unexpected exceptions are turned into ActionResult(ok=False) so callers never crash."""
    from . import policy  # local import: policy type-checks against Action from this module

    act = get_action(name)
    try:
        clean = validate_args(act, kwargs)
    except ValueError as e:
        return ActionResult(text=str(e), ok=False)
    blocked = policy.check(act, clean)
    if blocked:
        _audit.warning("BLOCKED %s reason=%r", _short_call(name, clean), blocked)
        return ActionResult(text=blocked, ok=False, data={"blocked": True})
    try:
        res = await act.func(**clean)
    except Exception as e:  # noqa: BLE001 - surface every failure to the user/model
        log.exception("action %s failed", name)
        res = ActionResult(text=f"{name} failed: {type(e).__name__}: {e}", ok=False)
    if not isinstance(res, ActionResult):
        res = ActionResult(text=str(res))
    _audit.info("RAN %s ok=%s%s", _short_call(name, clean), res.ok, " DANGEROUS" if act.dangerous else "")
    return res


def _short_call(name: str, kwargs: dict[str, Any], limit: int = 200) -> str:
    s = describe_call(name, kwargs)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def describe_call(name: str, kwargs: dict[str, Any]) -> str:
    """Human summary used in confirmation prompts, e.g. 'kill(pid=1234)'."""
    args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return f"{name}({args})"


def tool_schemas(*, include_dangerous: bool = True) -> list[dict[str, Any]]:
    return [a.tool_schema() for a in sorted(REGISTRY.values(), key=lambda a: a.name)
            if include_dangerous or not a.dangerous]


def load_all() -> dict[str, Action]:
    """Import every module in ACTION_MODULES so their @action decorators register."""
    for mod in ACTION_MODULES:
        try:
            importlib.import_module(f"bot.actions.{mod}")
        except ModuleNotFoundError as e:
            if e.name == f"bot.actions.{mod}":
                log.warning("action module %s not present yet - skipping", mod)
            else:
                log.exception("action module %s failed to import", mod)
        except Exception:
            log.exception("action module %s failed to import", mod)
    for name, act in _EVER.items():  # modules import only once: restore anything cleared since
        REGISTRY.setdefault(name, act)
    return REGISTRY
