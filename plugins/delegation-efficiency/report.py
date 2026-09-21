#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
from __future__ import annotations
import argparse, html, json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegation_efficiency.aggregate import report, rollup
from delegation_efficiency.analysis import analysis_report, counterfactual_net_savings, export_analysis, threshold_analysis
from delegation_efficiency.schema import configure_state_dir, state_dir


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: int) -> str:
    return f"{value:,}"


def _percent(value: object) -> str:
    if value is None:
        return "Not available"
    return f"{float(value) * 100:.1f}%"


def _impact_metric(impact: dict, key: str, label: str) -> str:
    metric = impact.get("avoided", {}).get(key, {})
    total = metric.get("total")
    evidence = str(metric.get("evidence", "unknown"))
    value = "Not measured" if total is None else _number(int(total))
    return (
        f'<div><strong>{value}</strong><span>{_text(label)} avoided</span>'
        f'<small class="evidence {"estimated" if evidence == "estimated" else "measured" if evidence == "measured" else "unknown"}">'
        f'{_text(evidence.capitalize())} · coverage {_percent(metric.get("coverage"))}</small></div>'
    )


def _bar(value: int, maximum: int, label: str) -> str:
    width = 0 if maximum == 0 else round(value * 100 / maximum)
    return (
        '<div class="bar-row"><span class="bar-label">'
        f"{_text(label)}</span><span class=\"bar\"><span style=\"width:{width}%\"></span>"
        f'</span><strong>{_number(value)}</strong></div>'
    )


def visual_report(data: dict) -> str:
    """Render only the already-aggregated, content-free report as standalone HTML."""
    rows = [row for row in data.get("monthly", []) if isinstance(row, dict)]
    total = sum(int(row.get("event_count") or 0) for row in rows)
    known_cost_count = sum(int(row.get("known_cost_count") or 0) for row in rows)
    unknown_cost_count = sum(int(row.get("unknown_cost_count") or 0) for row in rows)
    known_cost_micros = sum(int(row.get("known_cost_micros") or 0) for row in rows)
    months = sorted({str(row.get("month", "")) for row in rows if row.get("month")})

    event_counts = Counter()
    evidence_counts = Counter()
    for row in rows:
        event_count = int(row.get("event_count") or 0)
        evidence_counts[str(row.get("evidence_class", "unknown"))] += event_count
    provider_count = sum(int(row.get("event_count") or 0) for row in rows if row.get("event_kind") == "provider_usage")
    analysis = data.get("analysis", {}) if isinstance(data.get("analysis", {}), dict) else {}
    savings = analysis.get("counterfactual_net_savings", {}) if isinstance(analysis.get("counterfactual_net_savings", {}), dict) else {}
    impact = analysis.get("impact", {}) if isinstance(analysis.get("impact", {}), dict) else {}
    eligible_outputs = int(impact.get("eligible_outputs") or 0)
    intercepted_outputs = int(impact.get("intercepted_eligible_outputs") or 0)
    evidence_html = "\n".join(
        _bar(count, max(evidence_counts.values(), default=0), label)
        for label, count in sorted(evidence_counts.items())
    ) or '<p class="muted">No evidence rows are available.</p>'

    sections = [
        f"""
        <section class="impact">
          <h2>Counterfactual delegation net savings</h2>
          <p>Only explicitly guard-linked delegations are included. The token
          baseline models the guarded full tool output as estimated coordinator
          input, not provider output usage. Monetary baseline and net savings
          are unavailable without an authoritative input-token price or exact
          communication-to-request attribution. Unknown and unlinked
          components are excluded, not zero.</p>
          <div class="metrics compact">
            <div><strong>{_number(int(savings.get('summary', {}).get('linked_intercepted_delegations') or 0))}</strong><span>linked intercepted delegations</span></div>
            <div><strong>{_number(int(savings.get('summary', {}).get('token_rows') or 0))}</strong><span>complete token rows</span></div>
            <div><strong>{_number(int(savings.get('summary', {}).get('monetary_rows') or 0))}</strong><span>complete monetary rows</span></div>
          </div>
          <p class="muted">Token amounts remain separate from monetary cost. Evidence:
          {_text(savings.get('evidence', 'unknown'))}. Exclusions:
          {_text(savings.get('exclusions', {}))}.</p>
        </section>
        """,
        """
        <section class="impact">
          <h2>What the guard saved</h2>
          <p>These totals are based only on eligible outputs that were recorded
          as intercepted. They show measured payload reduction, not a dollar
          estimate or a comparison with an unguarded run.</p>
          <div class="metrics impact-metrics">
            <div><strong>__INTERCEPTED__</strong><span>intercepted eligible outputs</span></div>
            <div><strong>__ELIGIBLE__</strong><span>eligible outputs observed</span></div>
            <div><strong>__COVERAGE__</strong><span>interception opportunity covered</span></div>
          </div>
          <div class="metrics impact-metrics">
            __IMPACT__
          </div>
        </section>
        """.replace("__INTERCEPTED__", _number(intercepted_outputs))
        .replace("__ELIGIBLE__", _number(eligible_outputs))
        .replace("__COVERAGE__", _percent(analysis.get("rates", {}).get("interception", {}).get("rate")))
        .replace(
            "__IMPACT__",
            "\n".join((_impact_metric(impact, key, label) for key, label in (("bytes", "Bytes"), ("lines", "Lines"), ("tokens", "Tokens"))))
            or '<p class="muted">No avoided-payload measurements are available.</p>',
        ),
        f"""
        <section>
          <h2>Evidence and coverage</h2>
          <p>These bars show recorded totals by evidence class. They describe
          what was measured or estimated at the source boundary, not confidence
          in an unobserved alternative.</p>
          <div class="bars">{evidence_html}</div>
        </section>
        """,
        f"""
        <section>
          <h2>How complete is the picture?</h2>
          <p>Coverage shows how much of the observed interception opportunity
          has a corresponding avoided-payload measurement. Missing measurements
          remain unknown rather than being treated as zero.</p>
          <div class="metrics compact">
            <div><strong>{_number(sum(int(metric.get('available') or 0) for metric in impact.get('avoided', {}).values() if isinstance(metric, dict)))}</strong><span>payload measurements available</span></div>
            <div><strong>{_number(eligible_outputs)}</strong><span>eligible outputs in scope</span></div>
            <div><strong>{_number(intercepted_outputs)}</strong><span>intercepted outputs in scope</span></div>
          </div>
        </section>
        """,
    ]
    if provider_count or known_cost_count or unknown_cost_count:
        sections.append(f"""
        <section>
          <h2>Provider cost coverage</h2>
          <div class="metrics compact">
            <div><strong>{_number(provider_count)}</strong><span>provider usage events</span></div>
            <div><strong>{_number(known_cost_count)}</strong><span>events with known cost</span></div>
            <div><strong>{_number(unknown_cost_count)}</strong><span>events with unknown cost</span></div>
          </div>
          <p>Known cost is the sum of provider-reported source facts in the
          aggregate, expressed in micros. It is not a complete spend total and
          unknown cost has not been estimated.</p>
        </section>
        """)
    period = f"{_text(months[0])} to {_text(months[-1])}" if months else "No period recorded"
    sections_html = "\n".join(sections)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delegation efficiency aggregate report</title>
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#5e6b82; --line:#dbe3ef;
  --panel:#fff; --accent:#356ae6; --accent-soft:#dce7ff; --warn:#fff4d6; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#f5f7fb; color:var(--ink);
  font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:32px 20px 56px; }}
.eyebrow {{ color:var(--accent); font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
h1 {{ margin:.2rem 0 .5rem; font-size:clamp(2rem,5vw,3.2rem); line-height:1.1; }}
h2 {{ margin-top:0; font-size:1.25rem; }} p {{ color:var(--muted); }}
section,.hero {{ background:var(--panel); border:1px solid var(--line); border-radius:16px;
  box-shadow:0 5px 18px #1720330b; padding:22px; margin-top:18px; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:12px; }}
.metrics div {{ background:#f7f9fd; border-radius:12px; padding:14px; }}
.metrics strong {{ display:block; font-size:1.65rem; }} .metrics span {{ color:var(--muted); font-size:.9rem; }}
  .compact {{ grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); }}
  .impact {{ border-color:var(--accent); }} .impact-metrics {{ margin-top:14px; }}
  .evidence {{ display:block; margin-top:5px; font-size:.78rem; font-weight:700; }}
  .measured {{ color:#16734a; }} .estimated {{ color:#9a6500; }} .unknown {{ color:var(--muted); }}
.bars {{ display:grid; gap:10px; }} .bar-row {{ display:grid; grid-template-columns:minmax(130px,1fr) 3fr auto;
  align-items:center; gap:10px; }} .bar-label {{ overflow-wrap:anywhere; }}
.bar {{ display:block; height:12px; background:var(--accent-soft); border-radius:99px; overflow:hidden; }}
.bar span {{ display:block; height:100%; background:var(--accent); border-radius:99px; }}
.muted {{ color:var(--muted); }} .callout {{ background:var(--warn); border-left:5px solid #e4a91b;
  padding:14px 16px; border-radius:8px; }} footer {{ color:var(--muted); font-size:.9rem; margin-top:22px; }}
@media (max-width:600px) {{ .bar-row {{ grid-template-columns:1fr auto; }} .bar {{ grid-column:1 / -1; grid-row:2; }} }}
</style>
</head>
<body><main>
<header class="hero">
  <div class="eyebrow">Executive summary: guard impact</div>
  <h1>How much work did the guard avoid?</h1>
  <p>Period: <strong>{period}</strong>. This summary uses privacy-safe
  aggregate measurements and clearly labels estimates.</p>
  <div class="metrics">
    <div><strong>{_number(intercepted_outputs)}</strong><span>intercepted eligible outputs</span></div>
    <div><strong>{_percent(analysis.get("rates", {}).get("interception", {}).get("rate"))}</strong><span>interception opportunity covered</span></div>
    <div><strong>{_number(total)}</strong><span>recorded aggregate events</span></div>
    <div><strong>{_number(len(months))}</strong><span>months represented</span></div>
    <div><strong>{_number(known_cost_count)}</strong><span>known cost rows</span></div>
    <div><strong>{_number(unknown_cost_count)}</strong><span>unknown cost rows</span></div>
    <div><strong>{_number(known_cost_micros)}</strong><span>known cost micros</span></div>
  </div>
</header>
{sections_html}
<section class="callout">
  <h2>How to read this</h2>
  <p>Measured values come from recorded guard observations. Token totals may be
  estimated when the source tokenizer was approximate. Do not fill missing
  values with zero or treat payload reduction as monetary savings. This report
  does <strong>not prove counterfactual savings</strong>: it does not observe
  what the same work would have cost without the guard.</p>
</section>
<footer>Content-free aggregate report. No source content, paths, hashes, raw
provider text, or joinable identifiers are included.</footer>
</main></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-detail", action="store_true")
    parser.add_argument("--rollup", action="store_true")
    parser.add_argument("--visual", action="store_true", help="render a self-contained aggregate HTML report")
    parser.add_argument("--output", help="write visual HTML to this explicit path")
    parser.add_argument("--stdout", action="store_true", help="write visual HTML to stdout explicitly")
    parser.add_argument("--analysis", action="store_true", help="emit evidence-preserving event-level analysis")
    parser.add_argument("--export", metavar="PATH", help="export content-free event-level analysis as CSV")
    parser.add_argument("--threshold", action="append", type=int, metavar="BYTES", help="compare a predeclared threshold; repeatable")
    parser.add_argument("--state-dir", help="override the plugin state directory")
    args = parser.parse_args()
    if args.output and not args.visual:
        parser.error("--output requires --visual")
    if args.stdout and not args.visual:
        parser.error("--stdout requires --visual")
    if args.output and args.stdout:
        parser.error("--output and --stdout are mutually exclusive")
    configure_state_dir(args.state_dir)
    if args.export and (args.visual or args.stdout or args.analysis or args.threshold):
        parser.error("--export cannot be combined with another report mode")
    if args.threshold and (args.visual or args.stdout or args.analysis):
        parser.error("--threshold cannot be combined with another report mode")
    if args.analysis and (args.visual or args.stdout):
        parser.error("--analysis cannot be combined with visual output")
    if args.rollup: rollup()
    if args.export:
        print(json.dumps(export_analysis(args.export), sort_keys=True, separators=(",", ":")))
        return 0
    if args.threshold:
        print(json.dumps(threshold_analysis(args.threshold), sort_keys=True, separators=(",", ":")))
        return 0
    if args.analysis:
        analysis = analysis_report()
        analysis["counterfactual_net_savings"] = counterfactual_net_savings()
        print(json.dumps(analysis, sort_keys=True, separators=(",", ":")))
        return 0
    result = report(args.local_detail)
    if args.visual:
        if args.local_detail:
            parser.error("--visual requires the aggregate report; omit --local-detail")
        result["analysis"] = analysis_report()
        result["analysis"]["counterfactual_net_savings"] = counterfactual_net_savings()
        rendered = visual_report(result)
        if args.stdout:
            sys.stdout.write(rendered)
        else:
            output = Path(args.output) if args.output else state_dir() / "aggregate-report.html"
            output.write_text(rendered, encoding="utf-8")
            print(json.dumps({"format": "html", "path": str(output), "status": "written"}, separators=(",", ":")))
    else:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
if __name__ == "__main__": raise SystemExit(main())
