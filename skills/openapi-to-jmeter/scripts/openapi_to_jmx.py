#!/usr/bin/env python3
"""
openapi_to_jmx.py — Convert an OpenAPI 3.x / Swagger 2.0 spec into a valid
Apache JMeter test plan (.jmx).

Design goals (see SKILL.md for the workflow):
  * One Thread Group, ordered HTTP Samplers (one per operation).
  * A single HTTP Header Manager at Thread Group scope (JMeter best practice)
    plus per-sampler header overrides only where the operation needs them.
  * Automatic correlation: response bodies are scanned (via JSON extractors
    we generate from the response schema) for ids/tokens, stored as JMeter
    variables, and re-used by later operations whose path/body reference the
    same field names. Auth tokens detected from a login-style operation are
    promoted into the global Header Manager via an extractor + variable.
  * Dynamic values (timestamps, uuids, random ints) are emitted as JMeter
    functions so each run is unique.
  * Response Assertions derived from declared response codes and, when a JSON
    response schema is present, a JSON Assertion on a required field.

Usage:
  python3 openapi_to_jmx.py SPEC [-o OUT.jmx] [--base-url URL]
                            [--threads N] [--ramp N] [--loops N]
                            [--order path|tag|crud]

SPEC may be JSON or YAML (.yaml/.yml). PyYAML is used if available; otherwise
JSON is assumed.
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


# --------------------------------------------------------------------------- #
# Spec loading / normalisation
# --------------------------------------------------------------------------- #
def load_spec(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    text = raw.lstrip()
    if text.startswith("{"):
        return json.loads(raw)
    try:
        import yaml  # type: ignore
        return yaml.safe_load(raw)
    except ImportError:
        # Last resort: maybe it's JSON without a leading brace edge case.
        return json.loads(raw)


def load_config(path):
    """Load the optional config YAML. Returns {} if path is None."""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(raw)
    except ImportError:
        # Config is documented as YAML, but JSON is a valid subset.
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit("Reading a YAML config requires PyYAML. Install it with "
                     "'pip install pyyaml' (or pass a JSON config).")
    return cfg or {}


def apply_config(args, cfg):
    """Fold config values into args. Precedence: explicit CLI flag > config >
    built-in default. We detect 'explicit CLI flag' by comparing against the
    argparse default sentinel set in main().
    """
    if not cfg:
        return args

    # Environment / base_url
    envs = cfg.get("environments") or {}
    explicit_env = getattr(args, "env", None)
    env_name = explicit_env or "default"
    if explicit_env and explicit_env not in envs:
        sys.exit(f"--env '{explicit_env}' not found in config environments "
                 f"({', '.join(envs) or 'none defined'}).")
    env = envs.get(env_name, {})
    if args.base_url is None and env.get("base_url"):
        args.base_url = env["base_url"]

    # Load profile (only fill when the flag was left at default)
    load = cfg.get("load") or {}
    if args.threads == 1 and "threads" in load:
        args.threads = int(load["threads"])
    if args.ramp == 1 and "ramp" in load:
        args.ramp = int(load["ramp"])
    if args.loops == 1 and "loops" in load:
        args.loops = int(load["loops"])

    # Ordering
    order = cfg.get("order") or {}
    args.order_sequence = order.get("sequence")
    args.order_include_unlisted = order.get("include_unlisted", True)
    if args.order == "path" and order.get("strategy"):
        args.order = order["strategy"]

    # Auth / headers / values carried through for build_jmx to consume
    args.cfg_auth = cfg.get("auth") or {}
    args.cfg_headers = cfg.get("headers") or {}
    args.cfg_values = cfg.get("values") or {}
    return args


def is_swagger2(spec):
    return "swagger" in spec and str(spec["swagger"]).startswith("2")


def resolve_ref(spec, ref):
    """Resolve a local $ref like '#/components/schemas/Pet'."""
    if not ref.startswith("#/"):
        return None
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def deref(spec, schema, _depth=0):
    """Shallowly resolve a $ref (one hop, guarded against cycles)."""
    if _depth > 8 or not isinstance(schema, dict):
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        resolved = resolve_ref(spec, schema["$ref"])
        return deref(spec, resolved or {}, _depth + 1)
    return schema


# --------------------------------------------------------------------------- #
# Base URL / server
# --------------------------------------------------------------------------- #
def parse_base_url(spec, override):
    if override:
        url = override
    elif is_swagger2(spec):
        host = spec.get("host", "localhost")
        base_path = spec.get("basePath", "") or ""
        schemes = spec.get("schemes") or ["https"]
        url = f"{schemes[0]}://{host}{base_path}"
    else:
        servers = spec.get("servers") or [{"url": "https://localhost"}]
        url = servers[0].get("url", "https://localhost")
    m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?(/.*)?$", url)
    if not m:
        return ("https", url, "", "")
    proto, domain, port, path = m.groups()
    if not port:
        port = "443" if proto == "https" else "80"
    return (proto, domain, port, (path or "").rstrip("/"))


# --------------------------------------------------------------------------- #
# Operation extraction & ordering
# --------------------------------------------------------------------------- #
HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"]
# CRUD ordering puts create before read/update before delete so correlated
# ids exist by the time later steps need them.
CRUD_RANK = {"post": 0, "get": 1, "put": 2, "patch": 3, "delete": 4}


def collect_operations(spec):
    ops = []
    paths = spec.get("paths", {}) or {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", []) or []
        for method in HTTP_METHODS:
            if method not in item:
                continue
            op = item[method]
            if not isinstance(op, dict):
                continue
            params = shared_params + (op.get("parameters", []) or [])
            ops.append({
                "method": method,
                "path": path,
                "operation_id": op.get("operationId"),
                "summary": op.get("summary") or op.get("description") or "",
                "tags": op.get("tags") or [],
                "parameters": params,
                "request_body": op.get("requestBody"),
                "responses": op.get("responses", {}) or {},
                "consumes": op.get("consumes"),   # swagger2
                "produces": op.get("produces"),   # swagger2
                "security": op.get("security"),
                "raw": op,
            })
    return ops


def order_operations(ops, mode, sequence=None, include_unlisted=True):
    # Explicit sequence of operationIds takes precedence over any strategy.
    if sequence:
        by_id = {}
        for o in ops:
            if o["operation_id"]:
                by_id.setdefault(o["operation_id"], o)
        ordered, used, missing = [], set(), []
        for op_id in sequence:
            if op_id in by_id:
                ordered.append(by_id[op_id])
                used.add(id(by_id[op_id]))
            else:
                missing.append(op_id)
        if missing:
            sys.stderr.write(
                "Warning: config order.sequence lists operationId(s) not "
                f"found in the spec: {', '.join(missing)}\n")
        if include_unlisted:
            ordered += [o for o in ops if id(o) not in used]
        return ordered
    if mode == "crud":
        return sorted(
            ops,
            key=lambda o: (o["path"], CRUD_RANK.get(o["method"], 9)),
        )
    if mode == "tag":
        return sorted(ops, key=lambda o: (o["tags"][0] if o["tags"] else "~"))
    return ops  # "path" / default: preserve spec document order


def sampler_name(op):
    if op["operation_id"]:
        return op["operation_id"]
    return f"{op['method'].upper()} {op['path']}"


def build_location_param_map(ops):
    """Map a collection path to the path-param name of its item path.

    e.g. given '/orders' and '/orders/{orderId}', returns {'/orders':
    'orderId'}. Used so a POST /orders that returns a Location header can bind
    the extracted id to the same variable name the downstream
    GET /orders/{orderId} consumes.
    """
    item_paths = {}
    for o in ops:
        params = path_param_names(o["path"])
        if len(params) >= 1 and o["path"].endswith("}"):
            # collection = path with the trailing /{param} removed
            collection = re.sub(r"/\{[^}]+\}$", "", o["path"])
            # only map when the item path adds exactly one param to collection
            if path_param_names(collection) == params[:-1]:
                item_paths.setdefault(collection, params[-1])
    return item_paths


# --------------------------------------------------------------------------- #
# Correlation engine
# --------------------------------------------------------------------------- #
# We track which JSON field names have been "produced" by an earlier response
# (via a JSON extractor) so that later operations referencing the same name in
# their path can use ${field} instead of a literal placeholder.
def var_for(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


class CorrelationManifest:
    """Records what the generator wired up so a human can audit the JMX.

    Three buckets:
      * produced   — variables extracted from a response (var -> producer op)
      * consumed   — resolved uses of a produced variable (var, by, where)
      * unresolved — path params / required fields that need a value but had
                     no producer at the point they were needed. These are the
                     manual-fix checklist.
    """

    def __init__(self):
        self.produced = []    # {variable, source, json_path, kind}
        self.consumed = []    # {variable, consumer, where, producer}
        self.unresolved = []  # {consumer, variable, where}
        # Columns the data file should carry: variable -> {reason, sample_hint}
        # dict preserves insertion order and de-dupes by variable name.
        self.datafile = {}

    def record_datafile_column(self, variable, reason, hint=""):
        # First reason wins; don't clobber a credential reason with a later
        # unresolved one for the same variable.
        if variable not in self.datafile:
            self.datafile[variable] = {"reason": reason, "hint": hint}

    def datafile_columns(self):
        return list(self.datafile.items())

    def record_produce(self, variable, source, json_path, kind="id"):
        # JSON producers get a $. prefix; header producers carry a plain label.
        loc = json_path if kind == "header" else "$." + json_path
        self.produced.append({
            "variable": variable, "source": source,
            "json_path": loc, "kind": kind})

    def record_consume(self, consumer, variable, where, producer):
        self.consumed.append({
            "variable": variable, "consumer": consumer,
            "where": where, "producer": producer})

    def record_unresolved(self, consumer, variable, where):
        self.unresolved.append({
            "consumer": consumer, "variable": variable, "where": where})
        self.record_datafile_column(
            variable, f"unresolved — needed by {consumer} ({where})")

    def to_dict(self):
        return {
            "summary": {
                "variables_produced": len(self.produced),
                "correlations_resolved": len(self.consumed),
                "correlations_unresolved": len(self.unresolved),
            },
            "produced": self.produced,
            "consumed": self.consumed,
            "unresolved": self.unresolved,
        }

    def to_markdown(self, title):
        lines = [f"# Correlation manifest — {title}", ""]
        s = self.to_dict()["summary"]
        lines += [
            f"- Variables produced (extracted from responses): "
            f"**{s['variables_produced']}**",
            f"- Correlations resolved (variable reused downstream): "
            f"**{s['correlations_resolved']}**",
            f"- Correlations **UNRESOLVED** (need a manual value): "
            f"**{s['correlations_unresolved']}**",
            "",
        ]
        if self.unresolved:
            lines += [
                "## ⚠️ Unresolved — fix these before running", "",
                "Each row is a path parameter or required field that needs a "
                "value but no earlier operation produced one. Set a User "
                "Defined Variable, add a pre-step that creates the resource, "
                "or hard-code a known value.", "",
                "| Operation | Variable | Where it's needed |",
                "|---|---|---|",
            ]
            for u in self.unresolved:
                lines.append(
                    f"| {u['consumer']} | `${{{u['variable']}}}` | "
                    f"{u['where']} |")
            lines.append("")
        else:
            lines += ["## ✅ No unresolved correlations", "",
                      "Every path parameter and required correlatable field "
                      "had a producer. Still worth a quick sanity check that "
                      "each variable feeds the operation you intended.", ""]
        if self.produced:
            lines += ["## Variables produced", "",
                      "| Variable | Extracted from | JSON path |",
                      "|---|---|---|"]
            for p in self.produced:
                lines.append(
                    f"| `${{{p['variable']}}}` | {p['source']} | "
                    f"`{p['json_path']}` |")
            lines.append("")
        if self.consumed:
            lines += ["## Correlations resolved", "",
                      "| Variable | Consumed by | Where | Produced by |",
                      "|---|---|---|---|"]
            for c in self.consumed:
                lines.append(
                    f"| `${{{c['variable']}}}` | {c['consumer']} | "
                    f"{c['where']} | {c['producer']} |")
            lines.append("")
        return "\n".join(lines)


def schema_field_names(spec, schema, prefix="", out=None, depth=0):
    """Return a flat list of (json_path, leaf_name) from an object schema."""
    if out is None:
        out = []
    if depth > 6:
        return out
    schema = deref(spec, schema or {})
    t = schema.get("type")
    if t == "array" or "items" in schema:
        items = deref(spec, schema.get("items", {}))
        return schema_field_names(spec, items, prefix, out, depth + 1)
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            jp = f"{prefix}.{name}" if prefix else name
            sub = deref(spec, sub)
            if sub.get("type") in (None, "object") and sub.get("properties"):
                schema_field_names(spec, sub, jp, out, depth + 1)
            else:
                out.append((jp, name))
    return out


def success_response_schema(spec, op):
    for code in ("200", "201", "2XX", "default"):
        resp = op["responses"].get(code)
        if not resp:
            continue
        resp = deref(spec, resp)
        content = resp.get("content")
        if content:  # OpenAPI 3
            for mt, media in content.items():
                if "json" in mt and media.get("schema"):
                    return deref(spec, media["schema"])
        elif resp.get("schema"):  # swagger2
            return deref(spec, resp["schema"])
    return None


def success_response_headers(spec, op):
    """Return declared response-header names for the success response.

    Used to detect Location/id-bearing headers for correlation. Covers
    OpenAPI 3 (responses.<code>.headers) and the common case where no headers
    are declared but a 201 implies a Location (handled by the caller).
    """
    for code in ("201", "200", "2XX", "default"):
        resp = op["responses"].get(code)
        if not resp:
            continue
        resp = deref(spec, resp)
        headers = resp.get("headers") or {}
        return list(headers.keys()), code
    return [], None


# Response headers worth correlating: Location plus *-Id / *-Token style.
HEADER_CORRELATABLE = re.compile(r"^(location|.*-(id|token|key))$", re.I)


CORRELATABLE = re.compile(r"(id|token|key|uuid|guid|secret|code)$", re.I)

# Login / credential inputs that belong in a data file, not hard-coded in the
# JMX. Matched against request-body field names.
CREDENTIAL_FIELDS = re.compile(
    r"^(email|username|user|login|password|passwd|pwd|"
    r"client_id|clientId|client_secret|clientSecret|api_key|apiKey|"
    r"grant_type|scope)$", re.I)


def path_param_names(path):
    return re.findall(r"\{([^}]+)\}", path)


# --------------------------------------------------------------------------- #
# Dynamic value substitution
# --------------------------------------------------------------------------- #
def dynamic_value_for(name, schema):
    """Return a JMeter function for fields that should vary per run."""
    n = name.lower()
    fmt = (schema or {}).get("format", "")
    if "uuid" in n or fmt == "uuid":
        return "${__UUID()}"
    if n in ("email",):
        return "user_${__time(,)}_${__Random(1,99999,)}@example.com"
    if "timestamp" in n or fmt in ("date-time",):
        return "${__time(yyyy-MM-dd'T'HH:mm:ss,)}"
    if fmt == "date":
        return "${__time(yyyy-MM-dd,)}"
    if "name" in n:
        return "name_${__Random(1,99999,)}"
    return None


def example_for(spec, schema, field_name=""):
    schema = deref(spec, schema or {})
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    dyn = dynamic_value_for(field_name, schema)
    if dyn:
        return dyn
    t = schema.get("type")
    if t == "integer":
        return "${__Random(1,1000,)}"
    if t == "number":
        return 1.5
    if t == "boolean":
        return True
    if t == "array":
        return [example_for(spec, schema.get("items", {}), field_name)]
    if t == "object" or schema.get("properties"):
        return build_example_object(spec, schema)
    return "${__Random(1,1000,)}_value"


def build_example_object(spec, schema, produced=None, depth=0,
                         manifest=None, consumer=None, path_prefix=""):
    schema = deref(spec, schema or {})
    if depth > 6:
        return {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    obj = {}
    for name, sub in props.items():
        sub = deref(spec, sub)
        if (schema.get("readOnly") or sub.get("readOnly")):
            continue
        field_path = f"{path_prefix}.{name}" if path_prefix else name
        # Re-use a correlated value if an earlier response produced this field.
        if produced and name in produced:
            obj[name] = "${%s}" % var_for(name)
            if manifest is not None:
                manifest.record_consume(consumer, var_for(name),
                                        f"body field '{field_path}'",
                                        produced[name])
            continue
        # Credential inputs (email/password/client_secret/...) come from the
        # data file rather than being hard-coded, so emit ${var} and register
        # the column.
        if CREDENTIAL_FIELDS.match(name):
            obj[name] = "${%s}" % var_for(name)
            if manifest is not None:
                manifest.record_datafile_column(
                    var_for(name), f"credential input for {consumer}")
            continue
        if name in required or len(obj) < 6:
            # A *required* field that looks correlatable (id/token/etc.) but
            # has no producer is a manual-fix point: record it as unresolved
            # AND reference it as ${var} so it binds to the CSV data-file column
            # rather than getting a throwaway random value.
            if (manifest is not None and name in required
                    and CORRELATABLE.search(name)):
                manifest.record_unresolved(
                    consumer, var_for(name),
                    f"required body field '{field_path}'")
                obj[name] = "${%s}" % var_for(name)
                continue
            obj[name] = example_for(spec, sub, name)
    return obj


# --------------------------------------------------------------------------- #
# XML helpers (JMeter .jmx hand-built tree)
# --------------------------------------------------------------------------- #
def el(tag, attrib=None, text=None):
    e = ET.Element(tag, attrib or {})
    if text is not None:
        e.text = text
    return e


def string_prop(name, value):
    return el("stringProp", {"name": name}, "" if value is None else str(value))


def bool_prop(name, value):
    return el("boolProp", {"name": name}, "true" if value else "false")


def collection_prop(name):
    return el("collectionProp", {"name": name})


def element_prop(name, eltype):
    return el("elementProp", {"name": name, "elementType": eltype})


# --------------------------------------------------------------------------- #
# JMeter component builders
# --------------------------------------------------------------------------- #
def make_header_manager(name, headers):
    hm = el("HeaderManager", {
        "guiclass": "HeaderPanel",
        "testclass": "HeaderManager",
        "testname": name,
        "enabled": "true",
    })
    coll = collection_prop("HeaderManager.headers")
    for hname, hvalue in headers.items():
        ep = element_prop("", "Header")
        ep.append(string_prop("Header.name", hname))
        ep.append(string_prop("Header.value", hvalue))
        coll.append(ep)
    hm.append(coll)
    return hm


def make_user_defined_vars(name, variables):
    args = el("Arguments", {
        "guiclass": "ArgumentsPanel",
        "testclass": "Arguments",
        "testname": name,
        "enabled": "true",
    })
    coll = collection_prop("Arguments.arguments")
    for k, v in variables.items():
        ep = element_prop(k, "Argument")
        ep.append(string_prop("Argument.name", k))
        ep.append(string_prop("Argument.value", v))
        ep.append(string_prop("Argument.metadata", "="))
        coll.append(ep)
    args.append(coll)
    return args


def make_http_sampler(domain, port, protocol, base_path, op, body_json):
    full_path = base_path + op["path"]
    # Convert {pathParam} -> ${pathParam} so JMeter variables drive them.
    full_path = re.sub(r"\{([^}]+)\}", lambda m: "${%s}" % var_for(m.group(1)),
                       full_path)
    s = el("HTTPSamplerProxy", {
        "guiclass": "HttpTestSampleGui",
        "testclass": "HTTPSamplerProxy",
        "testname": sampler_name(op),
        "enabled": "true",
    })

    # Body / query arguments
    args_elem = element_prop("HTTPsampler.Arguments", "Arguments")
    args_coll = collection_prop("Arguments.arguments")
    post_body_raw = bool(body_json)
    if body_json:
        arg = element_prop("", "HTTPArgument")
        arg.append(bool_prop("HTTPArgument.always_encode", False))
        arg.append(string_prop("Argument.value", body_json))
        arg.append(string_prop("Argument.metadata", "="))
        args_coll.append(arg)
    else:
        # query params as named args
        for p in op["parameters"]:
            if p.get("in") == "query":
                val = "${%s}" % var_for(p["name"])
                arg = element_prop(p["name"], "HTTPArgument")
                arg.append(bool_prop("HTTPArgument.always_encode", True))
                arg.append(string_prop("Argument.value", val))
                arg.append(string_prop("Argument.metadata", "="))
                arg.append(bool_prop("HTTPArgument.use_equals", True))
                arg.append(string_prop("Argument.name", p["name"]))
                args_coll.append(arg)
    args_elem.append(args_coll)
    s.append(args_elem)

    s.append(string_prop("HTTPSampler.domain", domain))
    s.append(string_prop("HTTPSampler.port", port))
    s.append(string_prop("HTTPSampler.protocol", protocol))
    s.append(string_prop("HTTPSampler.contentEncoding", "UTF-8"))
    s.append(string_prop("HTTPSampler.path", full_path))
    s.append(string_prop("HTTPSampler.method", op["method"].upper()))
    s.append(bool_prop("HTTPSampler.follow_redirects", True))
    s.append(bool_prop("HTTPSampler.auto_redirects", False))
    s.append(bool_prop("HTTPSampler.use_keepalive", True))
    s.append(bool_prop("HTTPSampler.DO_MULTIPART_POST", False))
    s.append(bool_prop("HTTPSampler.postBodyRaw", post_body_raw))
    return s


def make_response_assertion(codes):
    ra = el("ResponseAssertion", {
        "guiclass": "AssertionGui",
        "testclass": "ResponseAssertion",
        "testname": "Response Code Assertion",
        "enabled": "true",
    })
    coll = collection_prop("Asserion.test_strings")
    for c in codes:
        coll.append(string_prop(str(abs(hash(c)) % 100000000), c))
    ra.append(coll)
    ra.append(string_prop("Assertion.custom_message", ""))
    ra.append(string_prop("Assertion.test_field", "Assertion.response_code"))
    ra.append(bool_prop("Assertion.assume_success", False))
    # 1 = Matches, 16 = Equals, 2 = Contains; use Contains+OR for code lists.
    ra.append(el("intProp", {"name": "Assertion.test_type"}, "33"))  # OR + Equals
    return ra


def make_json_assertion(json_path, testname):
    ja = el("JSONPathAssertion", {
        "guiclass": "JSONPathAssertionGui",
        "testclass": "JSONPathAssertion",
        "testname": testname,
        "enabled": "true",
    })
    ja.append(string_prop("JSON_PATH", "$." + json_path))
    ja.append(string_prop("EXPECTED_VALUE", ""))
    ja.append(bool_prop("JSONVALIDATION", True))
    ja.append(bool_prop("EXPECT_NULL", False))
    ja.append(bool_prop("INVERT", False))
    ja.append(bool_prop("ISREGEX", False))
    return ja


def make_json_extractor(var_name, json_path):
    ex = el("JSONPostProcessor", {
        "guiclass": "JSONPostProcessorGui",
        "testclass": "JSONPostProcessor",
        "testname": f"Extract {var_name}",
        "enabled": "true",
    })
    ex.append(string_prop("JSONPostProcessor.referenceNames", var_name))
    ex.append(string_prop("JSONPostProcessor.jsonPathExprs", "$." + json_path))
    ex.append(string_prop("JSONPostProcessor.match_numbers", "1"))
    ex.append(string_prop("JSONPostProcessor.defaultValues", "NOT_FOUND"))
    return ex


def make_header_extractor(var_name, header_name, take_last_path_segment=True):
    """RegexExtractor scoped to response headers.

    Many REST creates return the new resource id only in a Location header
    (e.g. 'Location: /orders/12345'). We capture either the whole header value
    or, for Location-style headers, the last path segment (the id).
    """
    ex = el("RegexExtractor", {
        "guiclass": "RegexExtractorGui",
        "testclass": "RegexExtractor",
        "testname": f"Extract {var_name} from {header_name} header",
        "enabled": "true",
    })
    # useHeaders=true makes the extractor match against response headers.
    ex.append(string_prop("RegexExtractor.useHeaders", "true"))
    ex.append(string_prop("RegexExtractor.refname", var_name))
    if take_last_path_segment:
        # Match 'Header: .../<id>' and capture the trailing segment.
        regex = f"{header_name}:\\s*.*/([^/\\s]+)"
    else:
        regex = f"{header_name}:\\s*(.+?)\\s*$"
    ex.append(string_prop("RegexExtractor.regex", regex))
    ex.append(string_prop("RegexExtractor.template", "$1$"))
    ex.append(string_prop("RegexExtractor.default", "NOT_FOUND"))
    ex.append(string_prop("RegexExtractor.match_number", "1"))
    return ex


# --------------------------------------------------------------------------- #
# Hash tree assembly
# --------------------------------------------------------------------------- #
def ht():
    return el("hashTree")


def build_jmx(spec, args):
    protocol, domain, port, base_path = parse_base_url(spec, args.base_url)

    all_ops = collect_operations(spec)
    ops = order_operations(
        all_ops, args.order,
        sequence=getattr(args, "order_sequence", None),
        include_unlisted=getattr(args, "order_include_unlisted", True))
    if not ops:
        sys.exit("No operations found in the spec.")

    # Map a collection path (e.g. '/orders') to the single path-param name used
    # by its item path (e.g. '/orders/{orderId}' -> 'orderId'). Lets a create's
    # Location header bind to the downstream param name.
    location_param = build_location_param_map(all_ops)

    # --- global header / auth detection ---
    global_headers = {"Content-Type": "application/json",
                      "Accept": "application/json"}
    auth_header_name, auth_var = detect_global_auth(spec)
    # Config auth overrides / supplements detection.
    cfg_auth = getattr(args, "cfg_auth", {}) or {}
    if cfg_auth.get("header"):
        auth_header_name = cfg_auth["header"]
        auth_var = auth_var or "AUTH_TOKEN"
    if auth_header_name:
        scheme = cfg_auth.get("scheme")
        prefix = f"{scheme} " if scheme else ""
        global_headers[auth_header_name] = f"{prefix}${{{auth_var}}}"
    # Config header overrides last (highest precedence among headers).
    for hk, hv in (getattr(args, "cfg_headers", {}) or {}).items():
        global_headers[hk] = str(hv)

    # --- root ---
    root = el("jmeterTestPlan", {
        "version": "1.2", "properties": "5.0", "jmeter": "5.6.3"})
    root_ht = ht()
    root.append(root_ht)

    test_plan = el("TestPlan", {
        "guiclass": "TestPlanGui", "testclass": "TestPlan",
        "testname": spec.get("info", {}).get("title", "OpenAPI Test Plan"),
        "enabled": "true"})
    test_plan.append(bool_prop("TestPlan.functional_mode", False))
    test_plan.append(bool_prop("TestPlan.serialize_threadgroups", False))
    udv = element_prop("TestPlan.user_defined_variables", "Arguments")
    udv.append(collection_prop("Arguments.arguments"))
    test_plan.append(udv)
    test_plan.append(string_prop("TestPlan.comments", ""))
    root_ht.append(test_plan)
    plan_ht = ht()
    root_ht.append(plan_ht)

    # Plan-level User Defined Variables (env config)
    plan_ht.append(make_user_defined_vars("Environment Variables", {
        "BASE_PROTOCOL": protocol, "BASE_HOST": domain, "BASE_PORT": port,
    }))
    plan_ht.append(ht())

    # --- thread group ---
    tg = el("ThreadGroup", {
        "guiclass": "ThreadGroupGui", "testclass": "ThreadGroup",
        "testname": "API Flow", "enabled": "true"})
    tg.append(string_prop("ThreadGroup.on_sample_error", "continue"))
    loop = element_prop("ThreadGroup.main_controller", "LoopController")
    loop.append(bool_prop("LoopController.continue_forever", False))
    loop.append(string_prop("LoopController.loops", str(args.loops)))
    tg.append(loop)
    tg.append(string_prop("ThreadGroup.num_threads", str(args.threads)))
    tg.append(string_prop("ThreadGroup.ramp_time", str(args.ramp)))
    tg.append(bool_prop("ThreadGroup.scheduler", False))
    plan_ht.append(tg)
    tg_ht = ht()
    plan_ht.append(tg_ht)

    # Thread-group-scoped Header Manager (best practice: shared once)
    tg_ht.append(make_header_manager("Default Header Manager", global_headers))
    tg_ht.append(ht())

    # --- samplers ---
    # Emit into a scratch hashtree first so the manifest is fully populated
    # before we decide whether a CSV Data Set Config is needed (its column
    # list comes from the manifest).
    produced = {}          # field_name -> producer sampler name once extracted
    manifest = CorrelationManifest()
    samplers_ht = ht()
    cfg_auth = getattr(args, "cfg_auth", {}) or {}
    for op in ops:
        emit_sampler(spec, op, domain, port, protocol, base_path,
                     samplers_ht, produced, auth_header_name, auth_var,
                     manifest, location_param, cfg_auth)

    # CSV Data Set Config — placed before the samplers so it's in scope for all
    # of them. Only emitted when there are columns that need human-supplied
    # data (unresolved correlations + detected credentials).
    columns = [v for v, _ in manifest.datafile_columns()]
    if columns and not getattr(args, "no_datafile", False):
        csv_name = getattr(args, "datafile_name", None) or "test_data.csv"
        tg_ht.append(make_csv_dataset(csv_name, columns))
        tg_ht.append(ht())

    # Now attach the samplers (each element followed by its child hashTree).
    for child in list(samplers_ht):
        tg_ht.append(child)

    # --- listeners (disabled by default; load testing uses CLI summariser) ---
    tg_ht.append(make_view_results_tree())
    tg_ht.append(ht())

    return root, manifest


def detect_global_auth(spec):
    """If a bearer/apiKey security scheme exists, return (header, var)."""
    comps = spec.get("components", {}).get("securitySchemes") or \
        spec.get("securityDefinitions") or {}
    for _, scheme in (comps or {}).items():
        if not isinstance(scheme, dict):
            continue
        stype = scheme.get("type", "").lower()
        if stype == "http" and scheme.get("scheme", "").lower() == "bearer":
            return ("Authorization", "BEARER_TOKEN")
        if stype == "oauth2":
            return ("Authorization", "BEARER_TOKEN")
        if stype == "apikey" and scheme.get("in") == "header":
            return (scheme.get("name", "X-API-Key"), "API_KEY")
    return (None, None)


TOKEN_FIELDS = re.compile(r"(access_token|accessToken|token|jwt|id_token)$",
                          re.I)


def emit_sampler(spec, op, domain, port, protocol, base_path, parent_ht,
                 produced, auth_header_name, auth_var, manifest,
                 location_param=None, cfg_auth=None):
    location_param = location_param or {}
    cfg_auth = cfg_auth or {}
    name = sampler_name(op)
    # Build request body using already-produced correlated values.
    body_json = build_request_body(spec, op, produced, manifest, name)

    sampler = make_http_sampler(domain, port, protocol, base_path, op,
                                body_json)
    parent_ht.append(sampler)
    s_ht = ht()
    parent_ht.append(s_ht)

    # Path-param correlation audit: each {param} consumes a variable. If a
    # prior op produced it, it's resolved; otherwise it's a manual-fix point.
    for pname in path_param_names(op["path"]):
        v = var_for(pname)
        if pname in produced:
            manifest.record_consume(name, v, f"path param '{{{pname}}}'",
                                    produced[pname])
        else:
            manifest.record_unresolved(name, v, f"path param '{{{pname}}}'")

    # 1) Response code assertion from declared responses.
    success_codes = [c for c in op["responses"].keys()
                     if c.isdigit() and c.startswith("2")]
    if not success_codes:
        success_codes = ["200", "201", "204"]
    s_ht.append(make_response_assertion(success_codes))
    s_ht.append(ht())

    # 2) Correlation extractors from the success schema.
    schema = success_response_schema(spec, op)
    if schema:
        fields = schema_field_names(spec, schema)
        # Auth-token promotion: extract bearer/api token into the global var.
        if auth_var:
            for jp, leaf in fields:
                if TOKEN_FIELDS.search(leaf):
                    s_ht.append(make_json_extractor(auth_var, jp))
                    s_ht.append(ht())
                    manifest.record_produce(auth_var, name, jp, kind="auth")
                    break
        # Generic id/token correlation for downstream path params.
        for jp, leaf in fields:
            if CORRELATABLE.search(leaf) and leaf not in produced:
                s_ht.append(make_json_extractor(var_for(leaf), jp))
                s_ht.append(ht())
                produced[leaf] = name
                manifest.record_produce(var_for(leaf), name, jp)
        # 3) JSON assertion on a required field if present.
        required = (schema.get("required") or [])
        if required and op["method"] in ("get", "post", "put"):
            s_ht.append(make_json_assertion(required[0],
                                            f"Body contains {required[0]}"))
            s_ht.append(ht())

    # 4) Header-based correlation (Location and *-Id/*-Token headers).
    #    Many creates return the new id only in a Location header; capture it so
    #    downstream {param} consumers resolve instead of going UNRESOLVED.
    declared_headers, _code = success_response_headers(spec, op)
    # A 201 create commonly implies a Location even when not declared.
    implies_location = (op["method"] == "post"
                        and "201" in op["responses"])
    header_targets = list(declared_headers)
    if implies_location and not any(
            h.lower() == "location" for h in header_targets):
        header_targets.append("Location")

    for hname in header_targets:
        if not HEADER_CORRELATABLE.match(hname):
            continue
        is_location = hname.lower() == "location"
        if is_location:
            # Bind the captured id to the downstream item path-param name if we
            # can infer it from the collection; else use a generic name.
            target_var = location_param.get(op["path"])
            if not target_var:
                target_var = "created_id"
            if target_var in produced:
                continue
            s_ht.append(make_header_extractor(var_for(target_var), "Location",
                                              take_last_path_segment=True))
            s_ht.append(ht())
            produced[target_var] = name
            manifest.record_produce(var_for(target_var), name,
                                    "Location header (last path segment)",
                                    kind="header")
        else:
            v = var_for(hname.replace("-", "_"))
            if v in produced:
                continue
            s_ht.append(make_header_extractor(v, hname,
                                              take_last_path_segment=False))
            s_ht.append(ht())
            produced[hname] = name
            manifest.record_produce(v, name, f"{hname} header", kind="header")


def build_request_body(spec, op, produced, manifest=None, consumer=None):
    rb = op.get("request_body")
    if not rb:
        return None
    rb = deref(spec, rb)
    content = rb.get("content")
    schema = None
    if content:
        for mt, media in content.items():
            if "json" in mt and media.get("schema"):
                schema = deref(spec, media["schema"])
                break
    if schema is None:
        # swagger2 body parameter
        for p in op["parameters"]:
            if p.get("in") == "body" and p.get("schema"):
                schema = deref(spec, p["schema"])
                break
    if schema is None:
        return None
    obj = build_example_object(spec, schema, produced=produced,
                               manifest=manifest, consumer=consumer)
    return json.dumps(obj, indent=2)


def make_csv_dataset(filename, variable_names):
    """CSV Data Set Config at Thread Group scope, reading the template file."""
    cds = el("CSVDataSet", {
        "guiclass": "TestBeanGUI",
        "testclass": "CSVDataSet",
        "testname": "Test Data (CSV)",
        "enabled": "true",
    })
    cds.append(string_prop("filename", filename))
    cds.append(string_prop("fileEncoding", "UTF-8"))
    cds.append(string_prop("variableNames", ",".join(variable_names)))
    cds.append(bool_prop("ignoreFirstLine", True))   # header row present
    cds.append(string_prop("delimiter", ","))
    cds.append(bool_prop("quotedData", True))
    cds.append(bool_prop("recycle", True))            # loop over rows
    cds.append(bool_prop("stopThread", False))
    cds.append(string_prop("shareMode", "shareMode.all"))
    return cds


def make_view_results_tree():
    vrt = el("ResultCollector", {
        "guiclass": "ViewResultsFullVisualizer",
        "testclass": "ResultCollector",
        "testname": "View Results Tree", "enabled": "false"})
    vrt.append(bool_prop("ResultCollector.error_logging", False))
    save = element_prop("saveConfig", "SampleSaveConfiguration")
    for tag, val in [
        ("time", "true"), ("latency", "true"), ("timestamp", "true"),
        ("success", "true"), ("label", "true"), ("code", "true"),
        ("message", "true"), ("threadName", "true"), ("dataType", "true"),
        ("responseData", "false"), ("samplerData", "false"),
        ("assertions", "true"), ("responseHeaders", "false"),
    ]:
        save.append(el(tag, {}, val))
    vrt.append(save)
    vrt.append(string_prop("filename", ""))
    return vrt


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_jmx(root, out_path):
    indent(root)
    tree = ET.ElementTree(root)
    with open(out_path, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(fh, encoding="utf-8", xml_declaration=False)


def write_csv_template(path, columns, values=None):
    """CSV with a header row and, if known values are supplied, one pre-filled
    data row so the plan runs out of the box.

    `columns` is a list of (variable, info) tuples. `values` maps variable name
    -> known value (from config). JMeter resolves a relative CSV path relative
    to the .jmx file, so the CSV is written next to the plan and referenced by
    basename.
    """
    import csv as _csv
    values = values or {}
    header = [v for v, _ in columns]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(header)
        # Only write a data row if we actually have at least one known value;
        # otherwise leave it header-only so the dev fills it in.
        if any(h in values for h in header):
            w.writerow([values.get(h, "") for h in header])
    filled = [h for h in header if h in values]
    return header, filled


def indent(elem, level=0):
    pad = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def slugify(text, fallback="test-plan"):
    """Turn a spec title into a filesystem-friendly basename."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or fallback


def guard_overwrite(paths, force):
    """If any path exists and not force, abort before writing anything."""
    if force:
        return
    existing = [p for p in paths if p and __import__("os").path.exists(p)]
    if existing:
        listing = "\n  ".join(existing)
        sys.exit(
            "Refusing to overwrite existing file(s):\n  " + listing +
            "\nRe-run with --force to overwrite, choose a different "
            "--output / --output-dir, or remove the files first.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="OpenAPI 3.x / Swagger 2.0 JSON or YAML file")
    ap.add_argument("-o", "--output", default=None,
                    help="Output .jmx path. Default: <spec-title>.jmx inside "
                         "--output-dir. A bare filename is placed in "
                         "--output-dir; a path with directories is used as-is.")
    ap.add_argument("--output-dir", default="perf",
                    help="Folder for generated artifacts (jmx, csv, manifest) "
                         "when --output is a bare name or omitted. Default: "
                         "'perf'. Created if missing.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing output files. Without this, the "
                         "tool refuses to clobber an existing .jmx / .csv / "
                         "manifest (protects other devs' plans in a repo).")
    ap.add_argument("--base-url", help="Override server URL")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--ramp", type=int, default=1)
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--order", choices=["path", "tag", "crud"], default="path",
                    help="Operation ordering. 'path' = document order (default), "
                         "'crud' = create→read→update→delete per path.")
    ap.add_argument("--manifest", default=None,
                    help="Path for the correlation manifest. Defaults to the "
                         "output name with a '.manifest.md' suffix. The JSON "
                         "form is written alongside with a '.manifest.json' "
                         "suffix.")
    ap.add_argument("--no-manifest", action="store_true",
                    help="Skip writing the correlation manifest.")
    ap.add_argument("--no-datafile", action="store_true",
                    help="Don't generate a CSV data-file template or wire a "
                         "CSV Data Set Config into the plan.")
    ap.add_argument("--datafile-name", default=None,
                    help="Basename for the CSV template. Default is namespaced "
                         "to the plan (<plan>.test_data.csv) so multiple plans "
                         "in one folder don't collide. Written next to the .jmx "
                         "and referenced by basename so JMeter finds it.")
    ap.add_argument("--config", default=None,
                    help="Optional config YAML (environments, load profile, "
                         "ordering sequence, auth hints, known values, headers). "
                         "See assets/config.example.yaml. CLI flags override it.")
    ap.add_argument("--env", default=None,
                    help="Named environment from the config's 'environments' "
                         "map to use for base_url (default: 'default').")
    a = ap.parse_args()

    import os
    # Load and fold in config before anything reads thread/order/etc. CLI > config.
    cfg = load_config(a.config) if a.config else {}
    a = apply_config(a, cfg)

    spec = load_spec(a.spec)
    if "paths" not in spec:
        sys.exit("Spec has no 'paths' — is this a valid OpenAPI/Swagger file?")

    # --- resolve output locations (repo-aware) ---
    title = spec.get("info", {}).get("title", "API")
    slug = slugify(title)

    # Decide the .jmx path. If --output has directory parts, honor them as-is.
    # If it's a bare filename (or omitted), place it inside --output-dir.
    if a.output and (os.path.dirname(a.output) or os.path.isabs(a.output)):
        jmx_path = a.output
    else:
        bare = a.output or f"{slug}.jmx"
        if not bare.endswith(".jmx"):
            bare += ".jmx"
        jmx_path = os.path.join(a.output_dir, bare)
    out_dir = os.path.dirname(os.path.abspath(jmx_path))
    jmx_base = re.sub(r"\.jmx$", "", os.path.basename(jmx_path))

    # CSV is namespaced to the plan so two plans in one folder don't collide.
    # An explicit --datafile-name overrides the namespaced default.
    if a.datafile_name:
        csv_base = a.datafile_name
    else:
        csv_base = f"{jmx_base}.test_data.csv"
    a.datafile_name = csv_base  # build_jmx wires this basename into the plan
    csv_path = os.path.join(out_dir, csv_base)

    # Manifest paths (md + json), namespaced to the plan too.
    if a.no_manifest:
        manifest_md = manifest_json = None
    elif a.manifest:
        manifest_md = a.manifest
        manifest_json = (re.sub(r"\.(md|markdown)$", "", manifest_md) + ".json"
                         if manifest_md.endswith((".md", ".markdown"))
                         else manifest_md + ".json")
    else:
        manifest_md = os.path.join(out_dir, jmx_base + ".manifest.md")
        manifest_json = os.path.join(out_dir, jmx_base + ".manifest.json")

    # Build first (in memory) so we know whether a CSV will actually be written
    # before we run the collision check — avoids flagging a CSV we won't create.
    root, manifest = build_jmx(spec, a)
    columns = manifest.datafile_columns()
    cfg_values = getattr(a, "cfg_values", {}) or {}
    will_write_csv = bool(columns) and not a.no_datafile

    # --- surface existing JMX plans in the target folder (don't merge) ---
    if os.path.isdir(out_dir):
        others = sorted(
            os.path.join(out_dir, f) for f in os.listdir(out_dir)
            if f.endswith(".jmx") and os.path.join(out_dir, f) !=
            os.path.abspath(jmx_path))
        if others:
            print(f"Note: {len(others)} existing JMX plan(s) already in "
                  f"{out_dir}:")
            for o in others:
                print(f"  - {os.path.basename(o)}")
            print("  This tool generates a standalone plan and does not merge "
                  "into existing ones. Confirm this should be a separate plan.")

    # --- refuse to clobber unless --force ---
    targets = [jmx_path]
    if will_write_csv:
        targets.append(csv_path)
    if manifest_md:
        targets += [manifest_md, manifest_json]
    guard_overwrite(targets, a.force)

    # --- write everything ---
    os.makedirs(out_dir, exist_ok=True)
    write_jmx(root, jmx_path)
    op_count = len(collect_operations(spec))
    print(f"Wrote {jmx_path} ({op_count} operations).")

    if will_write_csv:
        header, filled = write_csv_template(csv_path, columns, cfg_values)
        note = f", {len(filled)} pre-filled from config" if filled else ""
        print(f"Wrote {csv_path} (header + "
              f"{'1 data row' if filled else 'no rows'}{note}; "
              f"{len(header)} column(s): {', '.join(header)}).")
        missing = [h for h in header if h not in cfg_values]
        if missing:
            print(f"  Fill in column(s) before running: {', '.join(missing)}")
    elif not a.no_datafile:
        print("No data-file columns needed (no unresolved correlations or "
              "credential inputs detected); skipped CSV template.")

    if manifest_md:
        with open(manifest_md, "w", encoding="utf-8") as fh:
            fh.write(manifest.to_markdown(title))
        with open(manifest_json, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, indent=2)
        s = manifest.to_dict()["summary"]
        print(f"Wrote {manifest_md} and {manifest_json}.")
        print(f"  produced={s['variables_produced']} "
              f"resolved={s['correlations_resolved']} "
              f"UNRESOLVED={s['correlations_unresolved']}")
        if s["correlations_unresolved"]:
            print("  ⚠️  Review the UNRESOLVED section before running the plan.")


if __name__ == "__main__":
    main()