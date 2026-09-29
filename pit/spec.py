"""Job spec: one TOML file per job. Parse, validate, render templated inputs."""
import re
import shlex
import tomllib
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# jobs.<id>.result[.key...] | jobs.<id>.verdict | findings.<id>.text|status | inputs.<key>
REF = re.compile(r"^(?:(jobs)\.(.+?)\.(result|verdict)|(findings)\.(.+?)\.(text|status)|(inputs))((?:\.[A-Za-z0-9_\-]+)*)$")
VERDICTS = ("pass", "fail", "invalid", "unknown")
FINDING_LISTS = ("produces", "produces_if_pass", "produces_if_fail", "refutes_if_pass", "refutes_if_fail")
DEFAULTS = {"value": 1, "depends_on": [], "inputs": {}, "priority": 0, "run": "",
            "fail_on": ["feedback_report", "cache_miss"], **{k: [] for k in FINDING_LISTS}}


class SpecError(ValueError):
    pass


def load(path) -> dict:
    return normalize(tomllib.loads(Path(path).read_text()))


def loads(text: str) -> dict:
    return normalize(tomllib.loads(text))


def normalize(raw: dict) -> dict:
    spec = {**DEFAULTS, **raw}
    for k in ("depends_on", "fail_on", *FINDING_LISTS):
        spec[k] = list(spec[k])
    return spec


def dep_id(dep: str) -> tuple[str, str | None]:
    """`review3@pass` -> ("review3", "pass"): the dependant runs only on that branch."""
    base, _, want = dep.partition("@")
    return base, want or None


def refs(value):
    """Every template expression inside a (nested) value."""
    if isinstance(value, str):
        yield from TEMPLATE.findall(value)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from refs(v)


def validate(spec: dict, lanes: dict) -> list[str]:
    """Reasons to refuse the spec; empty list = accepted. `lanes` is lanes.load()['lanes']."""
    errs = []
    for k in ("id", "question", "expect", "if_pass", "if_fail"):
        if not str(spec.get(k, "")).strip():
            errs.append(f"missing {k}")
    if spec.get("id") and not ID_RE.match(str(spec["id"])):
        errs.append(f"bad id {spec['id']!r} (letters, digits, _ . : -)")
    if spec.get("expect") and spec["expect"] not in ("pass", "fail"):
        errs.append(f"expect must be pass or fail, not {spec['expect']!r}")
    if spec.get("if_pass") and str(spec["if_pass"]).strip() == str(spec.get("if_fail", "")).strip():
        errs.append("if_pass == if_fail: the run cannot change a decision")
    lane = spec.get("lane")
    if lane != "any" and lane not in lanes:
        errs.append(f"unknown lane {lane!r} (have: {', '.join([*lanes, 'any'])})")
    budget_s, budget_usd, value = spec.get("budget_s"), spec.get("budget_usd"), spec.get("value", 1)
    if not isinstance(budget_s, (int, float)) or budget_s <= 0:
        errs.append("budget_s must be a positive number")
    if not isinstance(budget_usd, (int, float)) or budget_usd < 0:
        errs.append("budget_usd must be a number >= 0")
    if not isinstance(value, (int, float)) or value <= 0:
        errs.append("value must be a positive number (questions it settles)")
    elif isinstance(budget_usd, (int, float)):
        # cheap lanes carry a $/value ceiling; `any` could land on any of them, so the strictest applies
        caps = [l["max_usd_per_value"] for n, l in lanes.items()
                if "max_usd_per_value" in l and lane in (n, "any")]
        if caps and budget_usd > value * min(caps):
            errs.append(f"budget_usd {budget_usd} > value {value} x ${min(caps)}/value on a cheap lane")
    if not isinstance(spec.get("run", ""), str):
        errs.append("run must be a shell command string")
    deps = {dep_id(d)[0] for d in spec.get("depends_on", [])}
    for d in spec.get("depends_on", []):
        if dep_id(d)[1] not in (None, "pass", "fail"):
            errs.append(f"depends_on {d!r}: only @pass or @fail")
    for k in FINDING_LISTS:
        if not all(isinstance(x, str) and ID_RE.match(x) for x in spec.get(k, [])):
            errs.append(f"{k} must be a list of ids")
    for expr in refs([spec.get("inputs", {}), spec.get("run", "")]):
        m = REF.match(expr)
        if not m:
            errs.append(f"unresolved template {{{{ {expr} }}}}: not jobs.<id>.result|verdict, "
                        f"findings.<id>.text|status or inputs.<key>")
        elif (m[2] or m[5]) and (m[2] or m[5]) not in deps:
            errs.append(f"unresolved template {{{{ {expr} }}}}: {m[2] or m[5]} is not in depends_on")
        elif m[7] and m[8].lstrip(".").split(".")[0] not in spec.get("inputs", {}):
            errs.append(f"unresolved template {{{{ {expr} }}}}: no such input")
    return errs


def _lookup(expr: str, ctx: dict):
    m = REF.match(expr)
    if not m:
        raise SpecError(f"bad template {expr!r}")
    if m[1]:
        job = ctx["jobs"].get(m[2])
        if not job or job.get("verdict") is None:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: {m[2]} has no result yet")
        cur = job["result"] if m[3] == "result" else job["verdict"]
    elif m[4]:
        f = ctx["findings"].get(m[5])
        if not f:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: no finding {m[5]}")
        cur = f[m[6]]
    else:
        cur = ctx["inputs"]
    for key in [k for k in m[8].split(".") if k]:
        if not isinstance(cur, dict) or key not in cur:
            raise SpecError(f"unresolved {{{{ {expr} }}}}: no key {key!r}")
        cur = cur[key]
    return cur


def fill(value, ctx: dict, quote: bool = False):
    """A string that is exactly one template takes the upstream value as-is (lists stay lists);
    templates inside a longer string are interpolated as text (shell-quoted when `quote`)."""
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value.strip())
        if whole and not quote:
            return _lookup(whole[1], ctx)
        return TEMPLATE.sub(lambda m: _str(_lookup(m[1], ctx), quote), value)
    if isinstance(value, list):
        return [fill(v, ctx, quote) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx, quote) for k, v in value.items()}
    return value


def _str(v, quote: bool = False) -> str:
    items = v if isinstance(v, list) else [v]
    return " ".join(shlex.quote(str(x)) if quote else str(x) for x in items)


def render(spec: dict, ctx: dict) -> tuple[dict, str]:
    """(inputs, run) with upstream results filled in. ctx = {jobs: {id: {verdict, result}}, findings: {...}}."""
    inputs = fill(spec.get("inputs", {}), ctx)
    # upstream values are data, not shell: quote them into the command
    return inputs, fill(spec.get("run", ""), {**ctx, "inputs": inputs}, quote=True)
