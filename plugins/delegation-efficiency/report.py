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
    """Render a minimal, content-free delegation cost comparison as HTML."""
    analysis = data.get("analysis", {}) if isinstance(data.get("analysis", {}), dict) else {}
    savings = analysis.get("counterfactual_net_savings", {}) if isinstance(
        analysis.get("counterfactual_net_savings", {}), dict
    ) else {}
    summary = savings.get("summary", {}) if isinstance(savings.get("summary", {}), dict) else {}
    comparison = summary.get("comparison", {}) if isinstance(summary.get("comparison", {}), dict) else {}
    complete_delegations = int(comparison.get("complete_delegations") or 0)
    if complete_delegations == 0:
        comparison_html = f"""
<p>No cost comparison is available yet.</p>
<p class="muted">A comparison appears after a guard interception is explicitly
linked to a delegation and all three measured costs are recorded: guard notice,
delegation request, and worker result. Missing data is unavailable, not zero.
It is not a zero-cost result.</p>"""
    else:
        comparison_html = f"""
<div class="cost"><span>Estimated cost without delegation guard</span><strong>{_number(int(comparison.get('estimated_without_guard_tokens') or 0))} tokens</strong></div>
<div class="cost"><span>Cost with delegation guard</span><strong>{_number(int(comparison.get('guarded_total_tokens') or 0))} tokens</strong></div>
<div class="cost saving"><span>Estimated savings</span><strong>{_number(int(comparison.get('estimated_savings_tokens') or 0))} tokens</strong></div>
<div class="breakdown"><strong>Cost with delegation guard</strong><ul>
<li>Guard notice: {_number(int(comparison.get('guard_notice_tokens') or 0))} tokens</li>
<li>Delegation request: {_number(int(comparison.get('delegation_request_tokens') or 0))} tokens</li>
<li>Worker result: {_number(int(comparison.get('worker_result_tokens') or 0))} tokens</li>
</ul></div>
<p class="muted">Based on {_number(complete_delegations)} complete delegations. Missing or unlinked components are excluded, not counted as zero. This is a token-cost estimate, not a monetary estimate.</p>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delegation cost comparison</title>
<style>
:root {{ color-scheme:light; --ink:#172033; --muted:#5f6b7d; --surface:#fff; --line:#d8dfeb; --accent:#2056b5; --saving:#16734a; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#f3f6fb; color:var(--ink); font:16px/1.5 system-ui,sans-serif; }}
main {{ max-width:720px; margin:40px auto; padding:28px; background:var(--surface); border:1px solid var(--line); border-radius:14px; }}
h1 {{ margin:0 0 6px; font-size:1.65rem; }} p {{ margin:8px 0; }} .muted {{ color:var(--muted); }}
.cost {{ display:grid; grid-template-columns:1fr auto; gap:12px; align-items:baseline; padding:14px 0; border-bottom:1px solid var(--line); }}
.cost strong {{ font-size:1.45rem; }} .saving strong {{ color:var(--saving); }}
.breakdown {{ margin:18px 0; padding:16px; border-radius:10px; background:#f3f6fb; }}
.breakdown ul {{ margin:8px 0 0; padding-left:22px; }} footer {{ margin-top:22px; color:var(--muted); font-size:.9rem; }}
</style></head><body><main>
<h1>Delegation cost comparison</h1>
<p class="muted">Token cost estimate for complete, explicitly linked guard delegations.</p>
{comparison_html}
<footer>Content-free aggregate report.</footer>
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
