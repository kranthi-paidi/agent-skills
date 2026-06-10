# openapi-to-jmeter — Usage Guide

Turn an OpenAPI 3.x or Swagger 2.0 spec into a ready-to-run Apache JMeter test
plan, with correlation, assertions, a data-file template, and an audit of what
could and couldn't be wired automatically.

---

## What it does

Given a spec (JSON or YAML), it generates a single valid `.jmx` containing one
ordered HTTP sampler per operation inside one Thread Group, and wires up the
parts that normally take hours by hand:

- **Ordered samplers** — every operation in the spec, in document order, or
  `crud` (create→read→update→delete per path), or an explicit sequence you
  define in config.
- **Correlation between steps** — ids/tokens in a response (`id`, `token`,
  `accessToken`, `uuid`, etc.) are extracted into JMeter variables and reused in
  later path params and request bodies. Also handles ids returned only in a
  `Location` header on `201 Created`.
- **Auth** — a detected bearer/apiKey scheme adds the right header to a shared
  Header Manager, populated from the login response.
- **Dynamic values** — uuids, timestamps, random ints, and emails use JMeter
  functions so each run is unique; read-only fields are left out of bodies.
- **Assertions** — a response-code assertion per sampler (matching the spec's
  declared 2xx codes) plus a JSON assertion on a required response field.
- **Header Manager best practice** — one shared manager at Thread Group scope,
  not duplicated per sampler.
- **Test-data CSV + CSV Data Set Config** — a CSV template wired into the plan,
  containing only the values a human must supply.
- **Correlation manifest** — a report of every variable produced, every
  correlation resolved, and everything left unresolved.
- **Repo safety** — names artifacts from the spec title, won't overwrite
  existing files without `--force`, and flags other `.jmx` files already in the
  folder.

## What it does NOT do

Being explicit here so there are no surprises:

- **It does not invent business scenarios.** It represents the API faithfully —
  one call per operation. It is not a scenario authoring tool (no branching,
  conditionals, weighted paths, or think-times). The config `order.sequence` is
  the only flow control: an ordered list of operations.
- **It does not guarantee a plan that runs end-to-end.** Microservice specs
  often need ids another service mints; those show up as UNRESOLVED and require
  a value from you. This is expected, not a failure.
- **It does not merge into existing test plans.** If the folder already has a
  `.jmx`, it tells you, then writes its own standalone plan. Combining plans is
  left to you.
- **It does not run the test or measure performance.** It generates the plan;
  you run it in JMeter.
- **It does not handle every spec construct.** Only local `$ref`s resolve;
  `allOf`/`oneOf`/`anyOf` are not merged; request bodies are JSON only (XML/form
  bodies need manual editing); remote/file `$ref`s are skipped.
- **It does not store secrets.** Credentials go into the CSV template as columns
  for you to fill — nothing is written into the plan or committed for you.
- **Correlation is heuristic, not semantic.** It matches on field names, so it
  can occasionally wire the wrong `id` when several resources share the name.
  The manifest exists so you can verify.

## What it produces

By default, into a `perf/` folder, named from the spec title (e.g. `pet-store`):

| File | Purpose |
|---|---|
| `<slug>.jmx` | The JMeter test plan. Open in JMeter or run headless. |
| `<slug>.test_data.csv` | Header-only (or pre-filled) data file for credentials and unresolved ids. Wired into the plan via a CSV Data Set Config. |
| `<slug>.manifest.md` | Human-readable correlation audit, including the UNRESOLVED checklist. |
| `<slug>.manifest.json` | Same data for CI (e.g. gate on `summary.correlations_unresolved`). |

If no values need human input, no CSV is written.

---

## How to run it

Minimal:

```bash
python3 scripts/openapi_to_jmx.py openapi.yaml
```

Common options:

```bash
python3 scripts/openapi_to_jmx.py openapi.yaml \
  --output-dir perf \          # where artifacts go (default: perf/)
  --order crud \               # create→read→update→delete per path
  --base-url https://staging.example.com \
  --threads 25 --ramp 30 --loops 5 \
  --config perf/config.yaml --env staging \
  --force                      # only to overwrite existing artifacts
```

The config file (see `assets/config.example.yaml`) persists environments, load
profile, an explicit operation sequence, auth hints, known values (pre-filled
into the CSV), and extra headers, so a run is reproducible. CLI flags override
config; config overrides spec defaults.

---

## Next steps after running

1. **Read the manifest first.** Open `<slug>.manifest.md`. The UNRESOLVED
   section is your to-do list — each row is a path param or required field with
   no producer (commonly a cross-service id). Skim the "resolved" section too,
   to confirm each variable feeds the operation you intended.

2. **Fill the data file.** Open `<slug>.test_data.csv` and add at least one row.
   Supply credentials and any UNRESOLVED ids the manifest listed. (You can
   pre-fill these via the config `values` block so future runs need no editing.)

3. **Point at the right environment.** Confirm the base URL is correct (set it
   via `--base-url` or the config `environments` map). Adjust auth if the
   manifest shows the token wasn't extracted as expected.

4. **Smoke-test in the GUI.** Open the plan in JMeter, enable the View Results
   Tree listener (it's included but disabled), and run with 1 thread / 1 loop.
   Fix anything that returns 4xx — often a body field the spec under-specified
   or a wrong correlation.

5. **Run headless for load.** Once the smoke run is clean:

   ```bash
   jmeter -n -t perf/<slug>.jmx -l results.jtl -e -o report/
   ```

   Bump threads/ramp/loops (flags or config) for real load. Keep the View
   Results Tree disabled during load runs — it's memory-heavy.

6. **Commit deliberately.** If the repo already had plans, decide whether yours
   is a new standalone plan or a replacement before committing. Keep the `.jmx`
   and its `.test_data.csv` together so JMeter resolves the relative path.

---

## When to reach for something else

- If you need branching, conditional, or weighted user journeys, this tool's
  output is a starting scaffold — author the scenario on top of it in JMeter, or
  raise it as a candidate for a dedicated scenario-builder.
- If the manifest shows mostly UNRESOLVED rows, the spec likely depends heavily
  on other services; consider generating plans per service and chaining them, or
  seeding the shared ids via the config `values` block.