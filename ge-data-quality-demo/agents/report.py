"""
report.py
---------
Formats triage failures into Markdown, HTML, and (optionally) a Slack summary,
then writes each to disk.

Intended to be imported by triage_failures.py; can also be run standalone
if you pass a pre-built list of TriagedFailure objects.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from triage_failures import TriagedFailure

# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def format_report(failures: list[TriagedFailure], total_expectations: int) -> str:
    lines: list[str] = []
    lines.append("# Data Quality Triage Report")
    lines.append(f"\n{len(failures)} of {total_expectations} expectations failed.\n")

    for i, f in enumerate(failures, start=1):
        lines.append(f"## {i}. [{f.priority}] `{f.column}` -- {f.expectation_type}")
        if f.unexpected_count is not None:
            pct = f" ({f.unexpected_percent:.1f}%)" if f.unexpected_percent is not None else ""
            lines.append(f"- Affected rows: {f.unexpected_count}{pct}")
        if f.partial_unexpected_list:
            sample = f.partial_unexpected_list[:5]
            lines.append(f"- Sample values: `{sample}`")
        lines.append(f"- Suggested next step: {f.next_step}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

PRIORITY_COLORS = {
    "CRITICAL": ("#ff4d4d", "#2a0000"),
    "HIGH":     ("#ffaa00", "#2a1a00"),
    "MEDIUM":   ("#4db8ff", "#001a2a"),
}

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Data Quality Triage Report</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 2rem;
      background: #0d1117;
      color: #c9d1d9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      font-size: 15px;
      line-height: 1.6;
    }}
    h1 {{
      font-size: 1.6rem;
      color: #f0f6fc;
      border-bottom: 1px solid #30363d;
      padding-bottom: 0.5rem;
      margin-bottom: 0.25rem;
    }}
    .meta {{
      color: #8b949e;
      font-size: 0.875rem;
      margin-bottom: 2rem;
    }}
    .failure {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.25rem;
      border-left: 4px solid #30363d;
    }}
    .failure-header {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.75rem;
      flex-wrap: wrap;
    }}
    .index {{ color: #8b949e; font-size: 0.875rem; min-width: 1.5rem; }}
    .badge {{
      display: inline-block;
      padding: 0.2rem 0.65rem;
      border-radius: 20px;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.05em;
    }}
    .column-name {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.9rem;
      color: #79c0ff;
      background: #1c2128;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
    }}
    .expectation-type {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.8rem;
      color: #8b949e;
    }}
    .stats {{ display: flex; gap: 1.5rem; margin-bottom: 0.75rem; flex-wrap: wrap; }}
    .stat {{ display: flex; flex-direction: column; }}
    .stat-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: #8b949e; }}
    .stat-value {{ font-size: 1rem; font-weight: 600; color: #f0f6fc; }}
    .sample {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.8rem;
      background: #1c2128;
      border: 1px solid #30363d;
      border-radius: 4px;
      padding: 0.4rem 0.75rem;
      margin-bottom: 0.75rem;
      color: #a5d6ff;
      word-break: break-all;
    }}
    .next-step-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: #8b949e; margin-bottom: 0.2rem; }}
    .next-step {{ color: #c9d1d9; font-size: 0.9rem; }}
    .summary-bar {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
    .summary-card {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 0.75rem 1.25rem;
      min-width: 130px;
    }}
    .summary-card .label {{ font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; }}
    .summary-card .value {{ font-size: 1.5rem; font-weight: 700; color: #f0f6fc; }}
    .footer {{
      margin-top: 2rem;
      font-size: 0.8rem;
      color: #484f58;
      border-top: 1px solid #21262d;
      padding-top: 1rem;
    }}
  </style>
</head>
<body>
  <h1>Data Quality Triage Report</h1>
  <p class="meta">Generated: {generated_at} &nbsp;·&nbsp; {failed} of {total} expectations failed</p>

  <div class="summary-bar">
    <div class="summary-card">
      <div class="label">Total checks</div>
      <div class="value">{total}</div>
    </div>
    <div class="summary-card">
      <div class="label">Failed</div>
      <div class="value" style="color:#ff4d4d">{failed}</div>
    </div>
    <div class="summary-card">
      <div class="label">Critical</div>
      <div class="value" style="color:#ff4d4d">{n_critical}</div>
    </div>
    <div class="summary-card">
      <div class="label">High</div>
      <div class="value" style="color:#ffaa00">{n_high}</div>
    </div>
    <div class="summary-card">
      <div class="label">Medium</div>
      <div class="value" style="color:#4db8ff">{n_medium}</div>
    </div>
  </div>

  {failure_cards}

  <div class="footer">Generated by triage_failures.py &nbsp;·&nbsp; ge-data-quality-demo</div>
</body>
</html>
"""

_FAILURE_CARD_TEMPLATE = """\
  <div class="failure" style="border-left-color: {border_color}">
    <div class="failure-header">
      <span class="index">#{index}</span>
      <span class="badge" style="background:{badge_bg}; color:{badge_color}">{priority}</span>
      <span class="column-name">{column}</span>
      <span class="expectation-type">{expectation_type}</span>
    </div>
    {stats_html}
    {sample_html}
    <div class="next-step-label">Suggested next step</div>
    <div class="next-step">{next_step}</div>
  </div>"""


def _failure_card(index: int, f: TriagedFailure) -> str:
    border_color, badge_bg = PRIORITY_COLORS.get(f.priority, ("#666", "#222"))
    badge_color = "#fff" if f.priority == "CRITICAL" else "#000"

    stats_html = ""
    if f.unexpected_count is not None:
        pct = f"{f.unexpected_percent:.1f}%" if f.unexpected_percent is not None else "—"
        stats_html = f"""\
    <div class="stats">
      <div class="stat">
        <span class="stat-label">Affected rows</span>
        <span class="stat-value">{f.unexpected_count}</span>
      </div>
      <div class="stat">
        <span class="stat-label">% of total</span>
        <span class="stat-value">{pct}</span>
      </div>
    </div>"""

    sample_html = ""
    if f.partial_unexpected_list:
        sample = f.partial_unexpected_list[:5]
        sample_html = f'<div class="sample">Sample: {sample}</div>'

    return _FAILURE_CARD_TEMPLATE.format(
        index=index,
        border_color=border_color,
        badge_bg=badge_bg,
        badge_color=badge_color,
        priority=f.priority,
        column=f.column,
        expectation_type=f.expectation_type,
        stats_html=stats_html,
        sample_html=sample_html,
        next_step=f.next_step,
    )


def format_html_report(failures: list[TriagedFailure], total_expectations: int) -> str:
    failure_cards = "\n".join(
        _failure_card(i, f) for i, f in enumerate(failures, start=1)
    )
    return _HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        failed=len(failures),
        total=total_expectations,
        n_critical=sum(1 for f in failures if f.priority == "CRITICAL"),
        n_high=sum(1 for f in failures if f.priority == "HIGH"),
        n_medium=sum(1 for f in failures if f.priority == "MEDIUM"),
        failure_cards=failure_cards,
    )


# ---------------------------------------------------------------------------
# LLM Slack summary (optional)
# ---------------------------------------------------------------------------


def llm_slack_summary(failures: list[TriagedFailure], report: str) -> str:
    """Return a Slack-ready summary from Claude, or '' if the key isn't set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        print(
            "[report] `anthropic` package not installed. "
            "Skipping LLM summary. Run: pip install anthropic",
            file=sys.stderr,
        )
        return ""

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are a data engineer writing a Slack incident summary for a data quality failure.

Here is the triage report:

{report}

Write a concise Slack message (use Slack markdown: *bold*, _italic_, bullet points with •).
Include:
  • A one-line headline with the severity (e.g. ":red_circle: CRITICAL data quality failures detected")
  • A brief summary of what failed and why it matters
  • The top 2 action items

Keep it under 300 words. No technical jargon beyond what's necessary."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Write all reports to disk
# ---------------------------------------------------------------------------


def write_reports(
    failures: list[TriagedFailure],
    total_expectations: int,
    output_dir: str,
) -> None:
    """
    Write triage_report.md, triage_report.html, and (if ANTHROPIC_API_KEY is
    set) slack_summary.md into *output_dir*.
    """
    md_report = format_report(failures, total_expectations)

    # Markdown
    md_path = os.path.join(output_dir, "triage_report.md")
    with open(md_path, "w") as fh:
        fh.write(md_report + "\n")
    print(f"[report] Markdown report written to {md_path}")

    # HTML
    html_path = os.path.join(output_dir, "triage_report.html")
    with open(html_path, "w") as fh:
        fh.write(format_html_report(failures, total_expectations))
    print(f"[report] HTML report written to {html_path}")

    # Optional Slack summary
    slack = llm_slack_summary(failures, md_report)
    if slack:
        slack_path = os.path.join(output_dir, "slack_summary.md")
        with open(slack_path, "w") as fh:
            fh.write(slack)
        print(f"[report] Slack summary written to {slack_path}")
        print("\n--- Slack Summary (LLM) ---")
        print(slack)
    else:
        print("[report] No LLM Slack summary (set ANTHROPIC_API_KEY to enable).")
