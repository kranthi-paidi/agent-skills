---
name: openapi-to-jmeter
description: Convert an OpenAPI 3.x spec or Swagger 2.0 file into a ready-to-run Apache JMeter test plan (.jmx). Use this skill whenever the user mentions JMeter, a .jmx file, load testing or performance testing from an API spec, or wants to turn an OpenAPI/Swagger/swagger.json/openapi.yaml file into JMeter scripts — even if they only say "make a JMeter script from my API spec". Produces a single valid JMX with one ordered HTTP sampler per operation, automatic correlation (ids/tokens passed between steps), dynamic values, response and JSON assertions matching the spec, and a shared Header Manager following JMeter best practices.
---

# OpenAPI / Swagger → JMeter JMX

Turn an API specification into one valid `.jmx` test plan that exercises every
operation in a sensible order, wiring outputs of earlier calls into later ones.

## When to use

Trigger whenever a user has an OpenAPI 3.x or Swagger 2.0 file (JSON or YAML)
and wants JMeter scripts, a `.jmx`, load/performance test scaffolding, or
"convert this API spec to JMeter". A spec almost always contains many
operations; the goal is a *single* JMX covering all of them.

## Workflow

1. **Locate the spec.** Find the uploaded file under
   `/mnt/user-data/uploads/`. If the content is already visible in context,
   you still need the file path on disk for the script. Accept `.json`,
   `.yaml`, `.yml`.

2. **Read the reference** `references/jmx_reference.md` so you can explain the
   structure and adjust correlation if the user asks. (Read it now if you
   intend to hand-edit the output; skip if you're just running the script.)

3. **Run the converter.** Copy the spec to a writable path if needed, then:

   ```bash
   python3 scripts/openapi_to_jmx.py <spec> [--output-dir perf] [options]
   ```

   By default the artifacts (`.jmx`, CSV, manifest) go into a `perf/` folder,
   with the `.jmx` named from the spec title (e.g. `pet-store.jmx`). Override
   the folder with `--output-dir`, or give a full `-o path/to/name.jmx` to
   control placement exactly.

   **Repo safety (important in shared repos):** the tool refuses to overwrite
   an existing `.jmx`, CSV, or manifest unless `--force` is passed — this
   protects plans other devs already committed. It also scans the target
   folder and reports any existing `.jmx` files, but never merges into them:
   combining two plans is a human decision. When you see that note, confirm
   with the user whether this should be a new standalone plan or replace an
   existing one (use `--force` or a different `--output` only after they
   confirm). The CSV is namespaced to the plan (`<plan>.test_data.csv`) so
   multiple plans can coexist in one folder.

   PyYAML is needed for YAML specs — `pip install pyyaml --break-system-packages`
   if the import fails.

   Alongside the `.jmx`, the script writes a **correlation manifest**
   (`<name>.manifest.md` and `.manifest.json`) recording every variable it
   produced, every correlation it resolved, and — critically — every path
   param or required field it could **not** find a producer for, flagged as
   UNRESOLVED. Use `--no-manifest` to skip it or `--manifest PATH` to rename.

   It also writes a **header-only CSV data-file template** (`test_data.csv` by
   default) and wires a `CSV Data Set Config` into the plan to read it. The CSV
   contains only the columns that need human-supplied values — the unresolved
   correlations plus detected credential inputs (email/username/password/
   client_secret/etc.). Correlated ids and dynamic values are deliberately
   *excluded*: those are extracted or generated at runtime, so putting them in
   a data file would defeat the purpose. The CSV is written next to the `.jmx`
   and referenced by basename so JMeter resolves it relative to the plan. The
   matching sampler bodies/paths reference `${column}` so filling a row drives
   the request. Use `--no-datafile` to skip, `--datafile-name NAME.csv` to
   rename. If no columns need human input, no CSV is written.

   Useful options:
   - `--config FILE.yaml` — an optional config file (see
     `assets/config.example.yaml`) that persists everything so a run is
     reproducible: named `environments` (pick one with `--env NAME`), a `load`
     profile, an explicit operation `order.sequence` (a lightweight scenario —
     list operationIds in the order to run them), `auth` hints, known `values`
     for unresolved/credential columns (pre-filled into the CSV's first row so
     the plan runs out of the box), and extra `headers`. CLI flags override the
     config; the config overrides spec defaults.
   - `--env NAME` — choose an environment from the config (default `default`).
   - `--order crud` — order operations create→read→update→delete *per path* so
     a created resource's `id` exists before later steps reference it. Use this
     when the spec lists operations in an order that would break correlation.
     Default is document order (`path`), which is usually already sensible.
   - `--base-url https://host/base` — override the server when the spec's
     server URL is wrong or templated.
   - `--threads N --ramp S --loops L` — set concurrency for a load test.
     Leave at the defaults (1/1/1) for a functional smoke run.

4. **Validate.** Always confirm the output is well-formed XML before handing
   it over:

   ```bash
   python3 -c "import xml.etree.ElementTree as ET; ET.parse('<out>.jmx'); print('valid')"
   ```

5. **Present** the `.jmx` with `present_files` and give a one-paragraph summary:
   how many operations, the ordering used, what was auto-correlated (ids,
   auth token), and how to run it (`jmeter -n -t plan.jmx -l results.jtl -e -o report/`).

## What the generated plan guarantees

The script handles the hard parts automatically; the bullets below are what to
tell the user the JMX already does, not steps to perform yourself:

- **One sampler per operation, ordered** in a single Thread Group.
- **Correlation between steps**: response `id`/`token`/`key`/`uuid` fields are
  extracted into JMeter variables and reused in later path params and request
  bodies — no manual stitching. Also handles ids returned only in a **Location
  response header** (common on `201 Created`): a regex extractor captures the
  last path segment and binds it to the downstream path-param name.
- **Auth handling**: a bearer/apiKey security scheme adds an `Authorization`
  (or api-key) header to the shared Header Manager, populated from the first
  login response containing a token.
- **Dynamic values**: uuids, timestamps, random ints and generated emails use
  JMeter functions (`${__UUID()}`, `${__time()}`, `${__Random()}`) so each run
  is unique; `readOnly` fields are omitted from request bodies.
- **Assertions matching the spec**: a Response Assertion per sampler checks the
  declared 2xx status code, plus a JSON Assertion on a required response field.
- **Header Manager best practice**: one shared Header Manager at Thread Group
  scope (`Content-Type`/`Accept`/auth), not duplicated per sampler.
- **Test-data template**: a header-only CSV of just the values a human must
  supply (unresolved ids + credentials), wired in via a `CSV Data Set Config`,
  with sampler bodies/paths already referencing those columns.

## Verifying correlation (use the manifest)

Correlation is name-based heuristics, so always read the generated
`<name>.manifest.md` before handing the plan over — it is the audit trail.
It has three sections:

- **Unresolved** — path params and required fields with no producer. In
  microservice specs this is common and expected: a service often needs an id
  (e.g. `orderId`) that a *different* service mints, so the spec literally
  can't contain its producer. Each unresolved row is a manual-fix point — the
  dev sets a User Defined Variable, adds a pre-step, or hard-codes a known
  value. **Surface this list to the user explicitly** and tell them the plan
  won't run end-to-end until these are filled in.
- **Variables produced** — what got extracted and from where.
- **Correlations resolved** — which variable feeds which downstream operation.
  Skim this for wrong matches, especially when several resources share a field
  literally named `id`.

A manifest with zero unresolved correlations means the spec chains cleanly; a
manifest with many means the user has a short checklist rather than a debugging
session. Either way the JMX is still valid and runnable once the listed values
are provided.

## Limitations to surface

Only local `$ref`s are resolved; `allOf`/`oneOf`/`anyOf` are not merged; request
bodies are JSON only (XML/form bodies need manual editing). These are listed in
`references/jmx_reference.md` — mention the relevant ones if they apply to the
user's spec.