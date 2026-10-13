"""Phase 16 — Local Human-Review Application Preview Generator.

Creates a completely offline, static, non-submitting HTML preview
for the Novami Frontend Developer job application.
Uses strictly synthetic candidate data; never accesses real profile values,
never launches a browser, and never initiates network traffic.
"""

import html
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "artifacts" / "phase16_application_preview.html"

SYNTHETIC_PREVIEW_DATA = {
    "fullName": "[SYNTHETIC CANDIDATE NAME]",
    "email": "candidate@example.invalid",
    "phone": "[SYNTHETIC PHONE]",
    "position": "Frontend Developer (Engineering)",
    "portfolioUrl": "",
    "linkedinUrl": "",
    "coverLetter": "",
}

JOB_METADATA = {
    "employer": "Novami Infotech Pvt. Ltd.",
    "role": "Frontend Developer",
    "listing_url": "https://www.novamiinfotechs.com/careers/frontend-developer/",
    "status": "Preparation preview — not submitted",
    "vacancy_note": "Note: Vacancy status has not been rechecked during Phase 16 (strictly offline operation).",
}

PREVIEW_FIELDS = [
    {
        "website_field": "fullName",
        "input_type": "text",
        "required": True,
        "proposed_profile_key": "name",
        "confidence": "Confirmed",
        "preview_value_status": "Synthetic candidate name provided ([SYNTHETIC CANDIDATE NAME])",
        "human_review_needed": "No",
    },
    {
        "website_field": "email",
        "input_type": "email",
        "required": True,
        "proposed_profile_key": "email",
        "confidence": "Confirmed",
        "preview_value_status": "Synthetic RFC 2606 email provided (candidate@example.invalid)",
        "human_review_needed": "No",
    },
    {
        "website_field": "phone",
        "input_type": "tel",
        "required": True,
        "proposed_profile_key": "phone",
        "confidence": "Confirmed",
        "preview_value_status": "Synthetic placeholder phone provided ([SYNTHETIC PHONE])",
        "human_review_needed": "No",
    },
    {
        "website_field": "position",
        "input_type": "select-one",
        "required": True,
        "proposed_profile_key": "position (Role selection)",
        "confidence": "Confirmed",
        "preview_value_status": "Target role option: 'Frontend Developer (Engineering)' (Reported as verified during Phase 15)",
        "human_review_needed": "No",
    },
    {
        "website_field": "portfolioUrl",
        "input_type": "url",
        "required": False,
        "proposed_profile_key": "portfolio",
        "confidence": "Candidate",
        "preview_value_status": "Blank (Optional; no URL planned)",
        "human_review_needed": "Yes (Decide if a portfolio URL should be provided)",
    },
    {
        "website_field": "linkedinUrl",
        "input_type": "url",
        "required": False,
        "proposed_profile_key": "linkedin",
        "confidence": "Candidate",
        "preview_value_status": "Blank (Optional; no URL planned)",
        "human_review_needed": "Yes (Decide if a LinkedIn URL should be provided)",
    },
    {
        "website_field": "coverLetter",
        "input_type": "textarea",
        "required": False,
        "proposed_profile_key": "cover_letter",
        "confidence": "Candidate",
        "preview_value_status": "Blank (Optional; no approved cover letter planned)",
        "human_review_needed": "Yes (Decide if a cover letter should be provided)",
    },
]

REVIEW_CHECKLIST = [
    "Confirm the employer (Novami Infotech Pvt. Ltd.) and target role (Frontend Developer).",
    "Confirm the proposed profile mappings (fullName -> name, email -> email, phone -> phone, position -> target role).",
    "Decide whether optional fields (portfolioUrl, linkedinUrl, coverLetter) should remain blank.",
    "Review the application manually on the employer's website separately, if desired.",
]

SAFETY_NOTICES = [
    "This is a local, offline preview only.",
    "No real personal information or profile values are included.",
    "No live website was navigated, requested, or opened in Phase 16.",
    "No form was filled or submitted.",
    "This preview does not authorize application execution, submission, or autofill.",
]


def render_preview_html() -> str:
    """Generate self-contained, deterministic, static HTML preview."""
    rows_html = []
    for f in PREVIEW_FIELDS:
        req_badge = (
            '<span class="badge required">Required</span>'
            if f["required"]
            else '<span class="badge optional">Optional</span>'
        )
        review_badge = (
            '<span class="badge review-yes">Yes</span>'
            if f["human_review_needed"].startswith("Yes")
            else '<span class="badge review-no">No</span>'
        )
        rows_html.append(f"""          <tr>
            <td><code>{html.escape(f['website_field'])}</code></td>
            <td>{req_badge}</td>
            <td><code>{html.escape(f['proposed_profile_key'])}</code></td>
            <td><span class="confidence">{html.escape(f['confidence'])}</span></td>
            <td>{html.escape(f['preview_value_status'])}</td>
            <td>{review_badge}</td>
          </tr>""")

    checklist_html = []
    for item in REVIEW_CHECKLIST:
        checklist_html.append(f"""        <li class="checklist-item">
          <input type="checkbox" disabled class="chk-box">
          <span>{html.escape(item)}</span>
        </li>""")

    notices_html = []
    for note in SAFETY_NOTICES:
        notices_html.append(f"        <li>{html.escape(note)}</li>")

    rows_str = "\n".join(rows_html)
    checklist_str = "\n".join(checklist_html)
    notices_str = "\n".join(notices_html)

    employer = html.escape(JOB_METADATA["employer"])
    role = html.escape(JOB_METADATA["role"])
    listing_url = html.escape(JOB_METADATA["listing_url"])
    status = html.escape(JOB_METADATA["status"])
    vacancy_note = html.escape(JOB_METADATA["vacancy_note"])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Phase 16 — Local Human-Review Application Preview</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --border: #334155;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --warn-bg: #451a03;
      --warn-border: #b45309;
      --warn-text: #fde047;
      --badge-req-bg: #7f1d1d;
      --badge-req-text: #fca5a5;
      --badge-opt-bg: #14532d;
      --badge-opt-text: #86efac;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      line-height: 1.5;
      padding: 32px 16px;
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
    }}
    header {{
      margin-bottom: 24px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 16px;
    }}
    h1 {{
      font-size: 24px;
      font-weight: 700;
      color: var(--primary);
      margin-bottom: 6px;
    }}
    .subtitle {{
      color: var(--text-muted);
      font-size: 14px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .card h2 {{
      font-size: 18px;
      margin-bottom: 14px;
      color: var(--text);
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }}
    .meta-item {{
      font-size: 14px;
    }}
    .meta-label {{
      color: var(--text-muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      display: block;
      margin-bottom: 2px;
    }}
    .meta-value {{
      font-weight: 500;
    }}
    .url-text {{
      font-family: monospace;
      font-size: 13px;
      word-break: break-all;
      background: rgba(0,0,0,0.3);
      padding: 4px 8px;
      border-radius: 4px;
      display: inline-block;
    }}
    .note {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 10px;
      font-style: italic;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      margin-top: 8px;
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}
    th {{
      background: rgba(0,0,0,0.25);
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: 0.5px;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    code {{
      font-family: monospace;
      background: rgba(0,0,0,0.35);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
      color: #93c5fd;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
    }}
    .badge.required {{
      background: var(--badge-req-bg);
      color: var(--badge-req-text);
    }}
    .badge.optional {{
      background: var(--badge-opt-bg);
      color: var(--badge-opt-text);
    }}
    .badge.review-yes {{
      background: #78350f;
      color: #fde68a;
    }}
    .badge.review-no {{
      background: #1e293b;
      color: #94a3b8;
      border: 1px solid var(--border);
    }}
    .confidence {{
      font-weight: 600;
      color: #38bdf8;
    }}
    .checklist {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .checklist-item {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      font-size: 14px;
    }}
    .chk-box {{
      margin-top: 3px;
      cursor: not-allowed;
    }}
    .safety-card {{
      background: var(--warn-bg);
      border: 1px solid var(--warn-border);
      border-radius: 8px;
      padding: 18px 20px;
      margin-bottom: 24px;
    }}
    .safety-card h2 {{
      color: var(--warn-text);
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .safety-list {{
      margin-left: 20px;
      color: #fef08a;
      font-size: 13px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    footer {{
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
      border-top: 1px solid var(--border);
      padding-top: 16px;
      margin-top: 32px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Phase 16 — Local Human-Review Application Preview</h1>
      <p class="subtitle">Controlled Offline Preparation Artifact — No Live Application Actions</p>
    </header>

    <div class="safety-card">
      <h2>Safety Notice (Strictly Enforced)</h2>
      <ul class="safety-list">
{notices_str}
      </ul>
    </div>

    <section class="card">
      <h2>Job Summary</h2>
      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">Employer</span>
          <span class="meta-value">{employer}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Target Role</span>
          <span class="meta-value">{role}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Status</span>
          <span class="meta-value">{status}</span>
        </div>
        <div class="meta-item" style="grid-column: 1 / -1;">
          <span class="meta-label">Listing Reference (Display only; non-clickable)</span>
          <span class="url-text">{listing_url}</span>
        </div>
      </div>
      <p class="note">{vacancy_note}</p>
    </section>

    <section class="card">
      <h2>Field Review Table</h2>
      <table>
        <thead>
          <tr>
            <th>Website Field</th>
            <th>Requirement</th>
            <th>Proposed Profile Key</th>
            <th>Confidence</th>
            <th>Preview Value Status</th>
            <th>Human Review Needed</th>
          </tr>
        </thead>
        <tbody>
{rows_str}
        </tbody>
      </table>
    </section>

    <section class="card">
      <h2>Human Review Checklist</h2>
      <ul class="checklist">
{checklist_str}
      </ul>
    </section>

    <footer>
      <p>Vivek Job Automation — Phase 16 Local Preparation Artifact</p>
      <p>Offline execution verified. Zero network requests, zero browser sessions, zero state changes.</p>
    </footer>
  </div>
</body>
</html>"""


def generate_preview_file(output_path: Optional[Path] = None) -> Path:
    """Generate and write the local preview HTML file."""
    target_path = Path(output_path) if output_path else DEFAULT_OUTPUT_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_preview_html()
    target_path.write_text(content, encoding="utf-8")
    return target_path


if __name__ == "__main__":
    out = generate_preview_file()
    print(f"Generated preview artifact at: {out}")
