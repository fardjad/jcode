# Delegation efficiency measurement plugin

This external catalog plugin consumes the content-free `1012`/`1013` observer
 envelope (`jcode.delegation-efficiency.v1`) and stores validated facts in a
private SQLite database. It never imports jcode runtime code and never stores
prompts, tool output, summaries, paths, payload hashes, or arbitrary metadata.

## Upstream source and updates

The upstream contract is the runtime measurement boundary described by
`plans/delegation-guard-efficiency-measurement.md`, Part 1 and Part 1.5, and
patches 1012 and 1013. This plugin is intentionally version-coupled to schema
`1.0` and semantics `part1-runtime-1`. When the envelope changes, update the
allowlist, fixtures, and compatibility tests together. Unknown versions,
unknown event kinds, nested values, forbidden names, and invalid scalar bounds
are rejected without persistence. The allowlist has no arbitrary `x_`
extensions. `deletion_generation`, when present, is a non-negative bounded
integer used by the deletion barrier.

## Install and configure

The project uses uv as a reproducible project and script runner. Runtime logic
uses Python's standard library and has no third-party dependencies. The catalog
root's `plugins/README.md` describes the one-time whole-directory symlink.
Copying only the entrypoint scripts is not sufficient because they import the
`delegation_efficiency` package. After creating that symlink, verify this
project in place:

```bash
PLUGIN_DIR="$HOME/.jcode/plugins/delegation-efficiency"
(
  cd "$PLUGIN_DIR"
  uv lock --check
  uv run --project . python -c 'import delegation_efficiency'
)
```

The commands verify the symlinked uv project and that its package is importable
without `PYTHONPATH`. The direct scripts also resolve the package relative to
their own location, so they work from any current directory.

Enable the detached measurement hook once. The plugin creates a private state
directory automatically, so normal use does not require setup or state flags:

```toml
[hooks]
delegation_efficiency = "~/.jcode/plugins/delegation-efficiency/record.py"
```

The record command accepts one JSON envelope from `JCODE_HOOK_PAYLOAD`, as
provided by jcode's measurement dispatcher. When that variable is absent, it
reads one JSON envelope from stdin for direct or manual use. It returns
promptly and always exits successfully with `inserted`, `duplicate`,
`rejected_deleted_generation`, or `dropped`. It fails open so observer
persistence cannot affect a guarded run.

## Storage and privacy policy

Each invocation opens SQLite in WAL mode with a two-second busy timeout and
bounded retry. Event insertion is one `BEGIN IMMEDIATE` transaction keyed by
immutable `event_id`; duplicate delivery is idempotent. Foreign keys cascade
source deletion into normalized guard, delegation, provider, and communication
observations. Source JSON is the validated scalar envelope, retained as an
immutable fact. Provider cost fields remain provider-reported source facts or
null. No price catalog, FX conversion, or inferred attribution is used.

Event identifiers and session identifiers are local-detail join keys. They are
kept only in the private detail store for 90 days by default, are absent from
aggregate reports, and are synchronously deleted by purge. Aggregates retain
only month, event kind, evidence class, counts, and known versus unknown cost
coverage. Aggregates are retained until the explicit `purge-aggregates` command.
A session deletion barrier is committed before success and rejects events from
the deleted generation or older, preventing resurrection after late delivery.
Events may pass only with a validated `deletion_generation` greater than the
barrier generation.

## Generate and interpret reports

Run the installed project, rather than a checkout. After enabling the hook,
these commands work without a state option:

```bash
PLUGIN_DIR="$HOME/.jcode/plugins/delegation-efficiency"

# Print the current aggregate snapshot, or roll up new detail first.
"$PLUGIN_DIR/report.py"
"$PLUGIN_DIR/report.py" --rollup

# Write a self-contained HTML report into plugin state and print its path.
"$PLUGIN_DIR/report.py" --visual

# Roll up old detail and apply the default 90-day retention period.
"$PLUGIN_DIR/retention.py" retain

# Inspect retention state. Purge commands remain explicit and require --yes.
"$PLUGIN_DIR/retention.py" status
"$PLUGIN_DIR/retention.py" purge-detail --yes
"$PLUGIN_DIR/retention.py" purge-session SESSION 1 --yes
"$PLUGIN_DIR/retention.py" purge-aggregates --yes
```

`report.py --visual` writes `aggregate-report.html` in plugin state and prints
a JSON status line containing its path. Use `--stdout` when HTML on stdout is
intended. Purge commands never assume consent: interactive use asks for `yes`,
and non-interactive use must pass `--yes`.

### Advanced state and output overrides

State resolution precedence is the CLI `--state-dir`, then
`DELEGATION_EFFICIENCY_STATE_DIR`, then `JCODE_HOME/state/delegation-efficiency`,
then `$XDG_STATE_HOME/jcode/delegation-efficiency`, with a platform-appropriate
private HOME fallback. The CLI option is available on `record.py`, `report.py`,
and `retention.py`. The environment override is useful for isolated runs:

```bash
DELEGATION_EFFICIENCY_STATE_DIR="$HOME/.local/state/jcode/delegation-efficiency" \
  "$PLUGIN_DIR/report.py" --visual
"$PLUGIN_DIR/report.py" --visual --state-dir /private/path --output /tmp/report.html
"$PLUGIN_DIR/report.py" --visual --state-dir /private/path --stdout
```

The `--rollup` command and the final report print compact JSON with this actual
shape. This pretty-printed example is nonempty because the optional sample
event above was recorded (values vary with the recorded events):

```json
{
  "costs": {
    "estimated": "separate_from_source",
    "label": "provider_reported_or_unknown"
  },
  "monthly": [
    {
      "event_count": 1,
      "event_kind": "tool_result",
      "evidence_class": "measured",
      "known_cost_count": 0,
      "known_cost_micros": 0,
      "month": "2026-09",
      "unknown_cost_count": 1
    }
  ],
  "privacy": "content_free",
  "scope": "aggregate"
}
```

Retention prints a separate scalar result. For example, after the preceding
rollup has already processed the current sample event, its output is:

```json
{"aggregated_months":1,"deleted_events":0}
```

The aggregate report contains only month, event kind, evidence class, counts,
and known versus unknown cost coverage. `known_cost_micros` is the sum of
recorded `cost_micros` values for that row. For an event-time provider-reported
cost, it is a provider-reported source fact when the event says so, not a
price-catalog calculation or a complete spend total. An unknown cost has no
numeric value: a null cost contributes to `unknown_cost_count`, it is not
estimated. The report may show `known_cost_micros` as `0` alongside a positive
`unknown_cost_count`; that zero is not a claim that unknown costs were zero.
`evidence_class` likewise describes the recorded
evidence level, not the confidence of an unrecorded counterfactual.

### Presenting and reading the visual report

Open `aggregate-report.html` in a browser or share the file as a static,
self-contained artifact. It uses only inline CSS and HTML, with no JavaScript,
CDNs, or network requests. Start with the executive summary, then use the
evidence and activity bars to explain what was actually recorded. Read the
guard, delegation, and provider-cost sections only when they appear: the
visualizer omits sections for dimensions that are absent from the aggregate.
Unknown cost and missing evidence are coverage caveats, not zeros. The report
deliberately does not prove counterfactual savings, because it does not observe
what the same work would have cost without the guard or delegation. It contains
no prompts, output, paths, hashes, raw provider text, or joinable identifiers.
The default `--visual` form writes HTML to the plugin state's report path and
prints that path. `--output PATH` writes only to the explicit path, while
`--stdout` is the explicit opt-in for HTML on stdout. Keep output paths in a
directory with the intended access controls.

Use the result as a bounded measurement ledger, not as an efficiency score:

* The visual report leads with a concise, explicitly linked delegation
  comparison: estimated coordinator-input tokens without the guard, all tokens
  with guard and delegation, and their difference as estimated token savings.
  The guarded total is broken down into guard notice, delegation request, and
  worker result tokens. Each complete row requires all three components;
  incomplete and unlinked cases are excluded, not treated as zero.
* It then shows intercepted eligible outputs, interception opportunity
  coverage, and avoided bytes, lines, and tokens. Bytes and lines are measured
  when source facts support them. Tokens are marked estimated when an
  approximate tokenizer was used. Do not derive avoided payload from
  `event_count`.
* `delegation_spawn` and `delegation_follow_up` show recorded event kinds, not
  whether a delegation was proactive or guard-triggered. The aggregate report
  has no joinable guard or delegation identifiers and does not expose that
  relationship.
* Latency, follow-up counts, retries, and outcomes are accepted as event facts
  but are not columns in this report. Their counts and distributions cannot be
  inferred from the monthly rows.
* Provider totals are not cache-sensitive in this interface. Cache read or
  creation tokens, provider/model dimensions, and attempt-level totals are not
  emitted by the report, so do not compare its rows as cache-adjusted provider
  spend.
* Missing or unknown evidence remains visible through the evidence class and
  unknown-cost count. Treat missing dimensions and unknown costs as unknown,
  rather than filling them with zero or attributing them to another event.

The report is content-free: no prompts, tool output, summaries, paths, payloads,
or raw provider text are shown or stored by this plugin. Aggregate and
rollup/retention outputs contain no event, session, request, or other joinable
IDs. `--local-detail` is a separate private diagnostic view and intentionally
includes event and session IDs; it is not an aggregate report and must be
protected accordingly. Even with complete-looking rows, these measurements do
not prove counterfactual total savings because they do not observe what the
same work would have cost without the guard or delegation.

Other operator commands are:

```bash
"$PLUGIN_DIR/report.py" --local-detail
"$PLUGIN_DIR/retention.py" status
"$PLUGIN_DIR/retention.py" purge-session SESSION 1 --yes
"$PLUGIN_DIR/retention.py" purge-detail --yes
"$PLUGIN_DIR/retention.py" purge-aggregates --yes
```

Reports label scope and cost coverage. They never present known-cost sums as
complete totals, and they do not claim lifecycle completion or monetary savings.

## Part 3 analysis, exports, and threshold comparisons

The Part 3 analysis is built after each accepted source event in the separate
`event_analysis` table. The source envelope, normalized observations, and
provider-reported cost columns are immutable source facts. Derived rows carry
`derivation_version = part3-analysis-2`, explicit per-delta evidence labels, and linkage
classes. A relationship is recorded only when the event supplies an explicit
guard, delegation, or covered provider identity. Missing identities remain
`unknown_linkage`; timing, event order, model names, and child-session IDs are
never used as joins.

The analysis report exposes exact numerator and denominator values, exclusions,
unknowns, and evidence for interception, explicit delegation, known-cost
coverage, and known-success rates. It also reports original and final-visible
payload distributions, approximate-token avoided-payload diagnostics,
communication and execution observations, latency and follow-up percentiles,
failure/fallback counts, provider/model dimensions, cache token classes, and
separate proactive versus guard-triggered populations. Approximate token
differences are estimates, not provider usage or monetary savings. Provider
costs remain event-time source facts and are never presented as billing truth.

The counterfactual delegation view models the guarded full tool output as an
estimated coordinator-input token baseline. It never equates that later input
with provider-reported output tokens. This catalog has no authoritative
input-token price or rate and no exact communication-to-request attribution,
so the visual comparison remains a token estimate and monetary savings are
explicitly unavailable.

```bash
"$PLUGIN_DIR/report.py" --analysis
"$PLUGIN_DIR/report.py" --export "$PLUGIN_DIR/state/analysis.csv"
"$PLUGIN_DIR/report.py" --threshold 4096 --threshold 8192 --threshold 16384
```

Threshold analysis reports workload-observable eligibility, interception,
unknowns, exclusions, sample sizes, derivation version, and cost coverage. It
does not tune thresholds, create a synthetic unguarded baseline, infer routing
errors without an automatic oracle, or claim monetary savings. The visual
report presents a separate plain-language guard-impact view while retaining
its content-free, non-joinable output policy.

## Tests

Run with disposable state and configuration. The suite covers default state
resolution, contract fixtures,
privacy rejection, idempotency, source/derived separation, cost provenance,
monthly aggregation, event-level derived analysis, explicit-linkage checks,
content-free export, threshold analysis, retention, deletion barriers, repeated
purge, restart, concurrent writers, and executable command behavior:

```bash
python3 -m unittest discover -s plugins/delegation-efficiency/tests -v
```

For repository-wide workflows, use `scripts/isolate_config.py`; this plugin
never reads the caller's jcode or XDG configuration.
