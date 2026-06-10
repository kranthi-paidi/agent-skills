# JMeter JMX reference & correlation conventions

This file documents how `openapi_to_jmx.py` maps an OpenAPI/Swagger spec onto
JMeter elements, and the conventions to keep in mind when hand-editing the
output. Read it when you need to explain or tweak the generated plan.

## Element hierarchy produced

```
Test Plan  (named after info.title)
├─ User Defined Variables "Environment Variables"  (BASE_PROTOCOL/HOST/PORT)
└─ Thread Group "API Flow"  (threads / ramp / loops configurable)
   ├─ HTTP Header Manager "Default Header Manager"   ← shared by ALL samplers
   ├─ HTTP Sampler  (operation 1)
   │  ├─ Response Assertion        (response code ∈ declared 2xx)
   │  ├─ JSON Extractor(s)         (correlation: ids, tokens)
   │  └─ JSON Assertion            (a required response field exists)
   ├─ HTTP Sampler  (operation 2) ...
   └─ View Results Tree            (listener, disabled by default)
```

## Header manager best practice

A single Header Manager at **Thread Group scope** applies to every sampler
below it — JMeter merges it into each request. This is preferred over one
Header Manager per sampler: less duplication, one place to change
`Content-Type`/`Accept`/auth. Add a per-sampler Header Manager only when a
single operation needs a header the others must not have (e.g. `multipart/
form-data` on an upload). Per-sampler managers override matching header names
from the parent.

## Correlation & dynamic values

* **Extraction**: for each successful response with a JSON schema, the script
  walks the schema's leaf fields. Any field whose name ends in
  `id|token|key|uuid|guid|secret|code` gets a `JSONPostProcessor` that stores
  it in a JMeter variable named after the field (sanitised).
* **Reuse in paths**: a path template `/pets/{id}` becomes `/pets/${id}`. So a
  `POST /pets` that extracts `id` feeds a later `GET /pets/{id}`. This is why
  **`--order crud` matters** — create must run before read/update/delete so the
  variable is populated. With the default `path` order, document order is kept;
  most specs already list create-style operations first.
* **Reuse in bodies**: when building a request body, a property whose name
  matches a previously-extracted field is emitted as `${field}` instead of a
  fresh value.
* **Auth token promotion**: if a security scheme declares bearer/oauth2/apiKey,
  the global Header Manager gets `Authorization: ${BEARER_TOKEN}` (or the api
  key header). The first response field matching `*token*`/`accessToken`/`jwt`
  is extracted into that variable, so logging in once authenticates the rest of
  the flow.
* **Dynamic per-run values**: fields are filled with JMeter functions so each
  run is unique — `${__UUID()}` for uuids, `${__time(...)}` for timestamps/
  dates, `${__Random(min,max,)}` for integers and name suffixes, generated
  emails for `email` fields. Enum fields use the first declared value; fields
  with `example`/`default` use those verbatim.

## Output layout & repo safety

By default the generator writes three artifacts into a `perf/` folder (override
with `--output-dir`), named from the spec title:

```
perf/
├── <slug>.jmx               # the plan
├── <slug>.test_data.csv     # data-file template (namespaced to the plan)
├── <slug>.manifest.md       # correlation audit (human)
└── <slug>.manifest.json     # correlation audit (machine / CI)
```

`-o path/name.jmx` with directory parts overrides both the folder and name; a
bare `-o name.jmx` is placed inside `--output-dir`. The CSV and manifest always
sit next to the `.jmx` (the CSV must, for JMeter's relative-path resolution).

Two safeguards make this usable in a repo that already contains other devs'
plans:

* **No silent overwrite.** If the target `.jmx`, CSV, or manifest already
  exists, the tool aborts and lists them, requiring `--force` (or a different
  name/dir). This prevents clobbering a committed plan.
* **Existing-plan awareness, not merging.** The tool scans the output folder
  and reports any other `.jmx` files, then proceeds to write its own
  standalone plan. It deliberately does not merge into an existing plan —
  combining two test plans is a human decision, so the agent should confirm
  intent rather than auto-merge.

The plan-namespaced CSV name means several generated plans can share one folder
without their data files colliding.

## Config file (optional)

`--config FILE.yaml` persists run settings so a spec regenerates identically and
known cross-service values get pre-filled. See `assets/config.example.yaml` for
the full annotated schema. Sections:

* **environments** — named `base_url`s; select with `--env NAME`. Lets one
  config target localhost / staging / prod without editing samplers.
* **load** — `threads`/`ramp`/`loops` (same as the flags).
* **order** — either `strategy` (path/tag/crud) or an explicit `sequence` of
  operationIds. The sequence is the lightweight scenario control: operations run
  in the listed order; `include_unlisted: false` drops anything not listed.
* **auth** — force the login operation, token field, header, and scheme prefix
  when auto-detection isn't enough.
* **values** — known values keyed by JMeter variable name (the CSV columns).
  These pre-fill the CSV template's first data row, so the unresolved
  cross-service ids and credentials you supply here make the plan runnable
  immediately. Columns without a value stay blank for the dev to fill.
* **headers** — extra entries merged into the shared Header Manager.

Precedence is CLI flag > config > spec default. An explicit `--env` that isn't
defined is a hard error listing the valid names.

## Header / Location correlation

Beyond JSON-body fields, the generator correlates ids returned in response
**headers**. The main case: a `201 Created` whose new-resource id appears only
in a `Location` header (e.g. `Location: /orders/12345`). The generator emits a
`RegexExtractor` (scoped to response headers, `useHeaders=true`) that captures
the last path segment and binds it to the downstream item path-param name —
inferred by matching the create's collection path (`/orders`) to the item path
(`/orders/{orderId}`). Headers named `*-Id`/`*-Token`/`*-Key` are also captured
(whole value). This removes a common class of false UNRESOLVED entries, since
many REST creates don't echo the id in the body at all.

## Test-data CSV & CSV Data Set Config

The generator writes a header-only CSV (default `test_data.csv`) next to the
`.jmx` and inserts a `CSV Data Set Config` at Thread Group scope (placed before
the samplers so it's in scope for all of them). The config is set to
`recycle=true`, `ignoreFirstLine=true` (header present), quoted data, and
`shareMode.all`.

Only columns that genuinely need human-supplied data are included:

* **Unresolved correlations** — path params / required correlatable fields with
  no producer (the same set the manifest flags).
* **Credential inputs** — request-body fields matching email/username/login/
  password/client_id/client_secret/api_key/grant_type/scope.

Correlated ids and dynamic values are intentionally excluded: they're extracted
or generated at runtime, and pinning them in a data file would break
uniqueness and correlation. The sampler that consumes a CSV column already
references it as `${column}` in its body or path, so the wiring is complete —
the only manual step is adding at least one data row. If no columns qualify, no
CSV and no CSV Data Set Config are emitted. Flags: `--no-datafile`,
`--datafile-name NAME.csv`.

Because JMeter resolves a relative CSV path relative to the plan file, keep the
CSV in the same directory as the `.jmx` (or edit the config to an absolute
path / `${__P(...)}` property for CI).

## Correlation manifest

Every run also emits `<name>.manifest.md` (human-readable) and
`<name>.manifest.json` (machine-readable) next to the `.jmx`. This is the audit
trail for the name-based heuristics:

* **produced** — variables extracted from responses, with the source operation
  and JSON path.
* **consumed** — resolved correlations: which variable feeds which downstream
  path param or body field, and which operation produced it.
* **unresolved** — path params and required correlatable fields that needed a
  value but had no producer at the point they were used. This is the manual-fix
  checklist. For microservices it is normal to see ids here that are minted by
  another service and therefore absent from this spec.

The JSON form is suitable for CI gating — e.g. fail a pipeline, or just warn, if
`summary.correlations_unresolved` exceeds a threshold. Pass `--no-manifest` to
suppress it or `--manifest PATH` to choose the filename.

## Assertions

* **Response Assertion** checks the HTTP response code equals one of the
  declared 2xx codes (test_type 33 = Equals + OR across the list). If the spec
  declares no 2xx code, it defaults to 200/201/204.
* **JSON Assertion** validates that the first `required` response field is
  present (JSON path `$.<field>`, validation on, no expected value) for
  read/create/update operations.

## Parameterising the environment

The plan hard-codes the resolved host/port/protocol into each sampler for
portability, but also exposes `BASE_PROTOCOL`/`BASE_HOST`/`BASE_PORT` as User
Defined Variables. To point the plan at another environment without editing
every sampler, either override at generation time with `--base-url`, or in the
JMeter GUI replace each sampler's domain with `${BASE_HOST}` (and similarly for
port/protocol). For CI runs, pass `-JBASE_HOST=...` and reference
`${__P(BASE_HOST)}`.

## Running the result

```
# GUI (for inspection / debugging)
jmeter -t test_plan.jmx

# Non-GUI load test with an HTML dashboard (recommended for real runs)
jmeter -n -t test_plan.jmx -l results.jtl -e -o report/
```

Bump `--threads`/`--ramp`/`--loops` for load; keep them at 1 for a functional
smoke run. Enable the View Results Tree listener only while debugging — it is
memory-heavy and should stay disabled for load tests.

## Known limitations to flag to the user

* Only local `$ref`s (`#/...`) are resolved; remote/file refs are skipped.
* `allOf`/`oneOf`/`anyOf` composition is not merged — the script reads
  `properties` directly, so composed schemas yield partial bodies.
* Correlation is name-based heuristics; verify that an extracted `id` truly
  feeds the intended downstream operation, especially when multiple resources
  share the field name `id`.
* XML/form request bodies are not generated (JSON only); for those, set the
  sampler body manually.