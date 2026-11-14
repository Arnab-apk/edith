"""Edith brain benchmark: which LLMs actually work as Edith's tool-calling brain?

    .venv\\Scripts\\python tools/bench_models.py --provider ollama --model qwen3:4b
    .venv\\Scripts\\python tools/bench_models.py --provider gemini --model gemini-2.5-flash --pause 4
    .venv\\Scripts\\python tools/bench_models.py --base-url URL --model M --api-key-env NAME
    .venv\\Scripts\\python tools/bench_models.py --provider gemini --list-models

Uses Edith's REAL system prompt and tool list (bot.jarvis.brain.model_tools() -> openai_tools()),
falling back to data/bench/tools_snapshot.json if bot/ cannot be imported. Talks to any
OpenAI-compatible /chat/completions endpoint (60 s timeout, no retries).
SAFETY: tools are NEVER executed. Tool calls are recorded and answered with canned fake results
from tools/bench_cases.json, so multi-step behaviour can be scored without touching the PC.
API keys are read from the environment or .env and never printed or written.

Output: data/bench/<provider>__<model>__<tag>.json (every case) + a row in data/bench/summary.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "data" / "bench"
SNAPSHOT = OUT_DIR / "tools_snapshot.json"
CASES_FILE = Path(__file__).resolve().parent / "bench_cases.json"
TIMEOUT = 60.0
MAX_ROUNDS = 4
INJECTED_PARAMS = ("chat_id",)

PROVIDERS = {  # name -> (base_url, key env var names tried in order, literal fallback key)
    "ollama": ("http://localhost:11434/v1", [], "ollama"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", ["GEMINI_API_KEY", "LLM_API_KEY"], None),
    "groq": ("https://api.groq.com/openai/v1", ["GROQ_API_KEY"], None),
    "nim": ("https://integrate.api.nvidia.com/v1", ["NVIDIA_API_KEY", "NIM_API_KEY"], None),
    "openrouter": ("https://openrouter.ai/api/v1", ["OPENROUTER_API_KEY"], None),
}


# ====================================================================== Edith's real prompt + tools
def load_edith(force_snapshot: bool = False) -> dict[str, Any]:
    """System prompt, OpenAI-format tools, and a validator. Live import first, snapshot fallback."""
    if not force_snapshot:
        try:
            from bot.actions import registry
            from bot.jarvis import brain
            registry.load_all()
            tools = brain.openai_tools(brain.model_tools())
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            SNAPSHOT.write_text(json.dumps({"system_prompt": brain.system_prompt(None), "tools": tools},
                                           indent=1, ensure_ascii=False), encoding="utf-8")

            def validate(name: str, args: dict) -> str | None:
                act = registry.REGISTRY.get(name)
                if act is None:
                    return "unknown tool"
                try:
                    registry.validate_args(act, {k: v for k, v in args.items() if k not in INJECTED_PARAMS})
                except ValueError as e:
                    return str(e)
                return None

            return {"source": "live", "system": brain.system_prompt(None), "tools": tools,
                    "user_turn": brain.user_turn_text, "validate": validate,
                    "confirm": {n for n in registry.REGISTRY if brain.needs_confirm(n)}}
        except Exception as e:  # noqa: BLE001 - another agent may be mid-edit
            print(f"[bench] live import failed ({type(e).__name__}: {e}); using {SNAPSHOT.name}", file=sys.stderr)
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    params = {t["function"]["name"]: t["function"]["parameters"] for t in snap["tools"]}

    def validate(name: str, args: dict) -> str | None:  # mirrors registry.validate_args
        sch = params.get(name)
        if sch is None:
            return "unknown tool"
        props = sch["properties"]
        args = {k: v for k, v in args.items() if k not in INJECTED_PARAMS}
        if set(args) - set(props):
            return f"unexpected argument(s) {sorted(set(args) - set(props))}"
        if [r for r in sch.get("required", []) if r not in args]:
            return "missing required argument(s)"
        for k, v in args.items():
            p, t = props[k], props[k].get("type")
            try:
                if t == "integer":
                    v = int(v)
                elif t == "number":
                    v = float(v)
            except (TypeError, ValueError):
                return f"bad value for {k!r}"
            if "enum" in p and v not in p["enum"]:
                return f"bad value for {k!r}: must be one of {p['enum']}"
        return None

    def user_turn(text: str) -> str:
        return f"[local time: {time.strftime('%A %d %B %Y, %H:%M')}]\n{text}"

    confirm = {t["function"]["name"] for t in snap["tools"]
               if "DANGEROUS" in t["function"]["description"] or "asked to confirm" in t["function"]["description"]}
    return {"source": "snapshot", "system": snap["system_prompt"], "tools": snap["tools"],
            "user_turn": user_turn, "validate": validate, "confirm": confirm}


# ====================================================================== HTTP
class Client:
    def __init__(self, base_url: str, api_key: str, model: str, extra: dict[str, Any], pause: float) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.extra = extra
        self.pause = pause
        self._last = 0.0
        self.http = httpx.Client(timeout=httpx.Timeout(TIMEOUT), headers={"Authorization": f"Bearer {api_key}"},
                                 transport=httpx.HTTPTransport(retries=0))

    def scrub(self, text: str) -> str:
        return text.replace(self.key, "[KEY]") if self.key and len(self.key) > 6 else text

    def _wait(self) -> None:
        gap = self.pause - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def chat(self, messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
        """One request. Returns {ok, latency, message, usage, error, status}. Never raises."""
        self._wait()
        body: dict[str, Any] = {"model": self.model, "messages": messages, **self.extra}
        if tools:
            body["tools"] = tools
        t0 = time.monotonic()
        try:
            r = self.http.post(f"{self.base}/chat/completions", json=body)
        except httpx.TimeoutException:
            return {"ok": False, "latency": time.monotonic() - t0, "error": "timeout", "status": None}
        except httpx.HTTPError as e:
            return {"ok": False, "latency": time.monotonic() - t0, "error": f"network: {type(e).__name__}",
                    "status": None}
        finally:
            self._last = time.monotonic()
        lat = time.monotonic() - t0
        if r.status_code != 200:
            out = {"ok": False, "latency": lat, "status": r.status_code,
                   "error": f"HTTP {r.status_code}: {self.scrub(r.text[:400])}"}
            if r.status_code == 429:  # keep which quota was hit (per-minute vs per-day) for the report
                out["quota"] = sorted(set(re.findall(r'"quotaId":\s*"([^"]+)"', r.text)))
                out["retry_after"] = (re.findall(r'"retryDelay":\s*"([^"]+)"', r.text) or [None])[0]
            return out
        try:
            data = r.json()
            msg = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            return {"ok": False, "latency": lat, "status": 200, "error": f"bad response: {self.scrub(r.text[:300])}"}
        return {"ok": True, "latency": lat, "status": 200, "message": msg, "usage": data.get("usage") or {},
                "finish": data["choices"][0].get("finish_reason")}

    def list_models(self) -> list[str]:
        r = self.http.get(f"{self.base}/models")
        r.raise_for_status()
        return sorted(m.get("id", "") for m in r.json().get("data", []))


# ====================================================================== scoring helpers
_THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_args(raw: Any) -> tuple[dict | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None, "arguments are not valid JSON"
    return (v, None) if isinstance(v, dict) else (None, "arguments are not a JSON object")


def arg_matches(value: Any, want: Any) -> bool:
    if isinstance(want, dict) and "contains" in want:
        return isinstance(value, str) and want["contains"].lower() in value.lower()
    if isinstance(want, dict) and "in" in want:
        return value in want["in"] or str(value).lower() in [str(w).lower() for w in want["in"]]
    if isinstance(want, dict) and "range" in want:
        try:
            return want["range"][0] <= float(value) <= want["range"][1]
        except (TypeError, ValueError):
            return False
    try:
        return value == want or float(value) == float(want)
    except (TypeError, ValueError):
        return str(value).strip().lower() == str(want).strip().lower()


def echo_assistant(msg: dict) -> dict:
    """Assistant message to send back in round 2. Tool calls are echoed VERBATIM so provider
    extras survive (Gemini 3 needs extra_content.google.thought_signature)."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    calls = []
    for c in msg.get("tool_calls") or []:
        c = dict(c)
        c.setdefault("id", f"call_{uuid.uuid4().hex[:12]}")
        c.setdefault("type", "function")
        fn = dict(c.get("function") or {})
        if not isinstance(fn.get("arguments"), str):
            fn["arguments"] = json.dumps(fn.get("arguments") or {})
        c["function"] = fn
        calls.append(c)
    if calls:
        out["tool_calls"] = calls
    if msg.get("extra_content"):
        out["extra_content"] = msg["extra_content"]
    return out


# ====================================================================== one case
def run_case(cl: Client, ed: dict, case: dict, default_fake: str) -> dict[str, Any]:
    tool_names = {t["function"]["name"] for t in ed["tools"]}
    messages = [{"role": "system", "content": ed["system"]},
                {"role": "user", "content": ed["user_turn"](case["user"])}]
    rec: dict[str, Any] = {"id": case["id"], "category": case["category"], "user": case["user"], "calls": [],
                           "rounds": 0, "latencies": [], "usage": [], "error": None, "final": None}
    for rnd in range(1, MAX_ROUNDS + 1):
        res = cl.chat(messages, ed["tools"])
        rec["latencies"].append(round(res["latency"], 3))
        rec["rounds"] = rnd
        if not res["ok"]:
            rec["error"], rec["status"] = res["error"], res.get("status")
            if "quota" in res:
                rec["quota"], rec["retry_after"] = res["quota"], res["retry_after"]
            break
        rec["usage"].append(res["usage"])
        msg = res["message"]
        calls = msg.get("tool_calls") or []
        if msg.get("reasoning") or msg.get("reasoning_content"):
            rec["reasoning_seen"] = True
        if not calls:
            rec["final"] = msg.get("content") or ""
            rec["finish"] = res.get("finish")
            break
        echoed = echo_assistant(msg)
        messages.append(echoed)
        for c in echoed["tool_calls"]:
            name = (c.get("function") or {}).get("name", "")
            args, perr = parse_args((c.get("function") or {}).get("arguments"))
            err = perr or (ed["validate"](name, args) if name in tool_names else "hallucinated tool")
            rec["calls"].append({"round": rnd, "name": name, "args": args, "error": err,
                                 "has_extra_content": "extra_content" in c})
            if name not in tool_names:
                result = f"Unknown tool {name!r}. Use only the tools provided."
            elif err:
                result = f"{name}: {err}"
            else:
                result = case.get("fake_results", {}).get(name, default_fake)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    else:
        rec["error"] = "too many rounds"
    return score(rec, case, ed, tool_names)


def score(rec: dict, case: dict, ed: dict, tool_names: set[str]) -> dict:
    names = [c["name"] for c in rec["calls"]]
    called = set(names)
    rec["hallucinated"] = sorted(called - tool_names)
    forbidden = set(case.get("forbid_tools", []))
    rec["forbidden_called"] = sorted(called & forbidden)
    if rec["error"]:
        rec.update(tool_ok=False, args_ok=False, final_ok=False, passed=False)
        return rec
    if case.get("no_tool"):
        tool_ok = not names
        unexpected: set[str] = set()
    else:
        expected_alts = [set(e) if isinstance(e, list) else {e} for e in case.get("expect_tools", [])]
        allowed = set().union(*expected_alts) if expected_alts else set()
        unexpected = called - allowed
        tool_ok = (all(alts & called for alts in expected_alts) and not rec["hallucinated"]
                   and not rec["forbidden_called"] and not (unexpected & ed["confirm"]))
    rec["unexpected"] = sorted(unexpected)
    # args: every call schema-valid, and the expected values present in at least one call of that tool
    args_ok = all(c["error"] is None for c in rec["calls"])
    for tool, want in (case.get("expect_args") or {}).items():
        mine = [c for c in rec["calls"] if c["name"] == tool and c["args"] is not None]
        if mine and not any(all(arg_matches(c["args"].get(k), v) for k, v in want.items()) for c in mine):
            args_ok = False
    rec["args_ok_strict"] = args_ok
    final = rec["final"] or ""
    rec["think_leak"] = "<think>" in final or "</think>" in final
    visible = _THINK.sub("", final).strip()
    rec["text_tool_call"] = bool(re.search(r'"(name|function)"\s*:\s*"(%s)"' % "|".join(map(re.escape, tool_names)),
                                           visible))
    final_ok = bool(visible) and not rec["think_leak"] and not rec["text_tool_call"]
    if final_ok and case.get("final_any"):
        final_ok = any(w.lower() in visible.lower() for w in case["final_any"])
    rec.update(tool_ok=tool_ok, args_ok=args_ok, final_ok=final_ok, passed=tool_ok and args_ok and final_ok)
    return rec


# ====================================================================== whole model
def ollama_unload(base: str, model: str) -> None:
    root = base.rstrip("/").removesuffix("/v1")
    try:
        httpx.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except httpx.HTTPError:
        pass


def summarize(results: list[dict], cold: float | None) -> dict[str, Any]:
    n = len(results)
    done = [r for r in results if not r.get("skipped")]
    with_calls = [r for r in done if r["calls"]]
    firsts = [r["latencies"][0] for r in done if r["latencies"] and not r["error"]]
    totals = [sum(r["latencies"]) for r in done if r["latencies"] and not r["error"]]
    errors: dict[str, int] = {}
    for r in done:
        if r["error"]:
            key = "timeout" if r["error"] == "timeout" else ("429" if r.get("status") == 429 else
                  f"HTTP {r['status']}" if r.get("status") else r["error"][:40])
            errors[key] = errors.get(key, 0) + 1
    chit = [r for r in done if r["category"] == "chitchat"]
    inj = [r for r in done if r["category"] == "injection"]
    pt = sum((u.get("prompt_tokens") or 0) for r in done for u in r["usage"])
    ct = sum((u.get("completion_tokens") or 0) for r in done for u in r["usage"])
    pct = lambda k, rows: round(100 * sum(1 for r in rows if r[k]) / len(rows), 1) if rows else None  # noqa: E731
    return {
        "cases": n, "completed": len(done), "skipped": n - len(done),
        "pass_pct": pct("passed", results), "tool_acc_pct": pct("tool_ok", results),
        "valid_args_pct": pct("args_ok", with_calls),
        "schema_valid_calls_pct": (round(100 * sum(1 for r in done for c in r["calls"] if c["error"] is None)
                                         / max(1, sum(len(r["calls"]) for r in done)), 1)),
        "final_ok_pct": pct("final_ok", results),
        "chitchat_pass": f"{sum(r['passed'] for r in chit)}/{len(chit)}" if chit else "n/a",
        "injection_pass": (all(not r["forbidden_called"] for r in inj) and all(not r["error"] for r in inj))
                          if inj and not all(r["error"] for r in inj) else None,
        "impossible_pass": all(r["passed"] for r in done if r["category"] == "impossible") or None,
        "hallucinated_calls": sum(len(r["hallucinated"]) for r in done),
        "think_leaks": sum(1 for r in done if r.get("think_leak")),
        "text_tool_calls": sum(1 for r in done if r.get("text_tool_call")),
        "first_turn_median_s": round(statistics.median(firsts), 2) if firsts else None,
        "total_median_s": round(statistics.median(totals), 2) if totals else None,
        "total_worst_s": round(max(totals), 2) if totals else None,
        "cold_start_s": round(cold, 2) if cold is not None else None,
        "prompt_tokens": pt, "completion_tokens": ct, "errors": errors,
    }


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def resolve_key(provider: str | None, key_env: str | None) -> str:
    from bot.cli import read_env  # does not load bot.config
    names = [key_env] if key_env else (PROVIDERS[provider][1] if provider else [])
    dotenv = read_env()
    for n in names:
        v = os.environ.get(n, "").strip() or dotenv.get(n, "").strip()
        if v:
            return v
    if provider and PROVIDERS[provider][2]:
        return PROVIDERS[provider][2]
    if not names:
        return "none"
    sys.exit(f"No API key: set {' or '.join(names)} in the environment or .env")


def report() -> str:
    """Markdown table from data/bench/summary.jsonl (latest full run per provider/model/tag)."""
    rows: dict[tuple, dict] = {}
    for line in (OUT_DIR / "summary.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("cases", 0) >= 15 and not str(r.get("tag", "")).startswith(("probe", "smoke")):
            rows[(r["provider"], r["model"], r["tag"])] = r
    out = ["| Model | Provider | Settings | Pass | Tool acc. | Valid args | Chit-chat | Injection | "
           "Median | Worst | Cold start | Errors |", "|" + "---|" * 12]
    for r in sorted(rows.values(), key=lambda r: (-(r["pass_pct"] or 0), r["total_median_s"] or 1e9)):
        err = ", ".join(f"{k} ×{v}" for k, v in r["errors"].items()) or "-"
        if r["skipped"]:
            err += f" ({r['skipped']} skipped)"
        out.append(f"| `{r['model']}` | {r['provider']} | {r['tag'] or 'default'} | {r['pass_pct']}% | "
                   f"{r['tool_acc_pct']}% | {r['valid_args_pct']}% | {r['chitchat_pass']} | "
                   f"{ {True: 'pass', False: 'FAIL', None: 'n/a'}[r['injection_pass']]} | "
                   f"{r['total_median_s']} s | {r['total_worst_s']} s | "
                   f"{str(r['cold_start_s']) + ' s' if r['cold_start_s'] is not None else '-'} | {err} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    if argv is None and sys.argv[1:] == ["--report"]:
        print(report())
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--provider", choices=sorted(PROVIDERS), help="preset base URL + key lookup")
    ap.add_argument("--base-url")
    ap.add_argument("--model", action="append", help="model id (repeatable)")
    ap.add_argument("--api-key-env", help="env/.env variable that holds the key (never printed)")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--reasoning-effort", help="send reasoning_effort (e.g. none, low)")
    ap.add_argument("--extra", default="{}", help='extra JSON merged into the request body, e.g. \'{"think": false}\'')
    ap.add_argument("--no-think-tag", action="store_true", help="append /no_think to the system prompt (Qwen3)")
    ap.add_argument("--tag", default="", help="label for this settings combination (file name + report)")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between requests (free tiers: 4)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--cold", action="store_true", help="Ollama: unload the model first and time a cold request")
    ap.add_argument("--max-timeouts", type=int, default=3, help="abort a model after N timeouts")
    ap.add_argument("--snapshot", action="store_true", help="use data/bench/tools_snapshot.json, not bot/")
    a = ap.parse_args(argv)

    base = a.base_url or (PROVIDERS[a.provider][0] if a.provider else None)
    if not base:
        ap.error("--base-url or --provider is required")
    provider = a.provider or re.sub(r"^https?://", "", base).split("/")[0]
    key = resolve_key(a.provider, a.api_key_env)
    extra = json.loads(a.extra)
    if a.reasoning_effort:
        extra["reasoning_effort"] = a.reasoning_effort

    if a.list_models:
        for m in Client(base, key, "", {}, 0).list_models():
            print(m)
        return 0
    if not a.model:
        ap.error("--model is required")

    ed = load_edith(a.snapshot)
    if a.no_think_tag:
        ed["system"] += "\n/no_think"
    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = spec["cases"]
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = a.tag or ",".join(f"{k}={v}" for k, v in extra.items()) + ("+no_think" if a.no_think_tag else "")

    for model in a.model:
        cl = Client(base, key, model, extra, a.pause)
        print(f"\n=== {provider} / {model} {('[' + tag + ']') if tag else ''}  ({len(cases)} cases, tools: {ed['source']})")
        cold = None
        if a.cold and "11434" in base:
            ollama_unload(base, model)
            time.sleep(2)
            r = cl.chat([{"role": "system", "content": ed["system"]}, {"role": "user", "content": "hi"}], ed["tools"])
            cold = r["latency"]
            print(f"  cold start (load + prefill of the full tool prompt): {cold:.1f}s"
                  + ("" if r["ok"] else f"  ERROR {r['error']}"))
        results: list[dict] = []
        timeouts, stop = 0, None
        for case in cases:
            if stop:
                results.append({"id": case["id"], "category": case["category"], "skipped": stop, "calls": [],
                                "latencies": [], "usage": [], "error": stop, "passed": False, "tool_ok": False,
                                "args_ok": False, "final_ok": False, "forbidden_called": [], "hallucinated": []})
                continue
            rec = run_case(cl, ed, case, spec.get("default_fake_result", "Done."))
            results.append(rec)
            mark = "PASS" if rec["passed"] else "fail"
            why = rec["error"] or ", ".join(k for k in ("tool_ok", "args_ok", "final_ok") if not rec[k])
            calls = " ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)[:60]})" for c in rec["calls"])
            print(f"  {mark} {case['id']:<18} {sum(rec['latencies']):6.1f}s  {calls[:110]}"
                  + (f"  <- {why}" if not rec["passed"] else ""))
            if rec.get("status") == 429:
                stop = "skipped: rate limited (429)"
                print(f"  !! 429 rate limit: quota={rec.get('quota')} retry_after={rec.get('retry_after')}")
            if rec.get("status") in (400, 401, 403, 404):
                stop = f"skipped: HTTP {rec['status']} (model unavailable or request rejected)"
            if rec["error"] == "timeout":
                timeouts += 1
                if timeouts >= a.max_timeouts:
                    stop = f"skipped: {timeouts} timeouts"
        summary = summarize(results, cold)
        row = {"provider": provider, "model": model, "tag": tag, "base_url": base, "when": time.strftime("%Y-%m-%d %H:%M"),
               "tools_source": ed["source"], **summary}
        out = OUT_DIR / f"{slug(provider)}__{slug(model)}__{slug(tag) or 'default'}.json"
        out.write_text(json.dumps({"summary": row, "cases": results}, indent=1, ensure_ascii=False), encoding="utf-8")
        with (OUT_DIR / "summary.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  -> pass {summary['pass_pct']}% · tools {summary['tool_acc_pct']}% · args {summary['valid_args_pct']}% · "
              f"chit-chat {summary['chitchat_pass']} · injection "
              f"{ {True: 'ok', False: 'FAIL', None: 'n/a'}[summary['injection_pass']]} · "
              f"median {summary['total_median_s']}s · worst {summary['total_worst_s']}s · errors {summary['errors'] or '-'}")
        print(f"  saved {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
