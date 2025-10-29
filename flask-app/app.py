import os
import io
import json
import time
import zipfile
import threading
import re
from datetime import datetime
from pathlib import Path
from typing import Tuple
from flask import Flask, request, jsonify, send_file, abort
from dotenv import load_dotenv

# Azure SDK
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.agents import AgentsClient  # accessed via project_client.agents
from azure.ai.agents.models import ListSortOrder, MessageRole

# Converters
import pdfkit
# import pypandoc
from pdf2docx import parse

# --- NEW (minimal) ---
from functools import wraps
# ---------------------

# -------------------------------------------------------
# Configuration & bootstrap
# -------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
load_dotenv()  # read .env

PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME")
API_KEY = os.getenv("API_KEY")  # optional API key gate

# --- NEW (minimal): tiny gate using API_KEY ---
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        supplied = request.headers.get("x-api-key") or request.args.get("api_key")
        # If API_KEY not set, allow requests (good for local dev)
        if API_KEY and supplied != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
# -------------------------------------------------------

if not PROJECT_ENDPOINT or not MODEL_DEPLOYMENT_NAME:
    raise RuntimeError("Missing PROJECT_ENDPOINT or MODEL_DEPLOYMENT_NAME in environment.")

# Optional: wkhtmltopdf path (Windows typical install path can be provided via env var)
WKHTMLTOPDF_PATH = os.getenv("WKHTMLTOPDF_PATH")  # e.g., C:\\Program Files\\wkhtmltopdf\\bin\\wkhtmltopdf.exe
if WKHTMLTOPDF_PATH and Path(WKHTMLTOPDF_PATH).exists():
    PDFKIT_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
else:
    PDFKIT_CONFIG = None  # pdfkit will try to find wkhtmltopdf in PATH

# -------------------------------------------------------
# LAZY Azure clients/agents (to avoid crash at import)
# -------------------------------------------------------
_cred = None
_project_client: AIProjectClient | None = None
_agents_client: AgentsClient | None = None
_eng_agent = None
_malay_agent = None
_html_maker_agent = None
_init_lock = threading.Lock()
_init_error: str | None = None

def _ensure_clients():
    """Init DefaultAzureCredential + AIProjectClient + AgentsClient once."""
    global _cred, _project_client, _agents_client, _init_error
    if _agents_client:
        return
    with _init_lock:
        if _agents_client:
            return
        try:
            _cred = DefaultAzureCredential()
            # keep your compatibility shim
            try:
                _project_client = AIProjectClient(
                    endpoint=PROJECT_ENDPOINT,
                    credential=_cred
                )
            except TypeError:
                _project_client = AIProjectClient(
                    endpoint=PROJECT_ENDPOINT,
                    credential=_cred,
                    subscription_id=os.getenv("AZURE_SUBSCRIPTION_ID"),
                    resource_group_name=os.getenv("AZURE_RESOURCE_GROUP"),
                    project_name=os.getenv("AZURE_AI_PROJECT_NAME"),
                )
            _agents_client = _project_client.agents  # type: ignore
            _init_error = None
        except Exception as e:
            # don't crash the worker; record the error for /health and first API call
            _init_error = f"Azure auth/client init failed: {e}"
            _cred = None
            _project_client = None
            _agents_client = None

# -------------------------------------------------------
# Base CSS
# -------------------------------------------------------
BASE_CSS = """
page { size: A4; margin: 20mm; }
body { font-family: Arial, sans-serif; line-height: 1.4; }
h1, h2 { color: #0a5; margin: 0 0 8px; }
h4 { color: #000; margin: 0 0 8px; background-color: #bfdfff; padding-top: 10px; padding-bottom: 10px;}
.meta { color: #666; font-size: 12px; margin-bottom: 18px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
.page-break { page-break-before: always; }
/* wkhtmltopdf-friendly two-column layout */
.two-column { display: table; width: 100%; table-layout: fixed; }
.column { display: table-cell; width: 50%; vertical-align: top; padding: 0 10px 0 0; }
.column.right { padding: 0 0 0 10px; }
body, .column { word-break: break-word; overflow-wrap: anywhere; }
h1 { font-size: 20pt; }
h2 { font-size: 14pt; }
"""



# -------------------------------------------------------
# Static Content
# -------------------------------------------------------
COPYRIGHT_BM = """
© Jabatan Penilaian dan Perkhidmatan Harta

Hak cipta terpelihara.

Tidak dibenarkan mencetak semula mana-mana bahagian artikel, ilustrasi, dan isi kandungan laporan ini dalam apa juga bentuk dan dengan cara apa jua sama ada secara elektronik, mekanik, fotokopi, rakaman atau cara lain sebelum mendapat izin bertulis daripada penerbit. Penerbit tidak bertanggungjawab terhadap kesahihan maklumat yang terkandung dalam laporan ini. Maklumat dalam laporan ini tidak boleh digunakan dalam apa-apa timbang tara, dakwaan dan tindakan undangundang atau sebagai asas untuk kesimpulan lain. Laporan ini dibuat tertakluk kepada beberapa andaian dan batasan.
"""

COPYRIGHT_ENG = """
© Valuation and Property Services Department

Copyright Reserved

No part of this report may be reproduced, stored in a retrieval system, transmitted in any form or by any means electronic, mechanical, photocopying, recording or otherwise without the prior written permission of the publisher. No responsibility is accepted for the accuracy of information contained in this report. Material published in this report cannot be used in any arbitration, litigation and legal proceedings or as a basis for other conclusions. The report was constructed subject to a set of assumptions and limitations.
"""

PENDAHULUAN_BM = """
Laporan Stok Harta Tanah menyebarkan maklumat berdasarkan kepada skop berikut:

i.      Stok sedia ada mengikut sub-sektor harta tanah iaitu kediaman, perdagangan, industri dan riadah.
ii.     Penawaran hadapan yang terdiri daripada data penawaran akan datang, mula pembinaan dan penawaran yang
dirancang.

Ingin dimaklumkan bahawa semua jadual data tersebut perlu dibaca seiring dengan catatan teknikal yang disertakan bersama
laporan ini. Maklumat berkenaan harta tanah perdagangan iaitu kompleks perniagaan dikategorikan kepada pusat membeli
belah, arked dan pasaraya besar manakala bagi pejabat binaan khas terdiri daripada pejabat kerajaan dan swasta.

Kami ingin merakamkan ucapan ribuan terima kasih kepada semua yang telah menjayakan penerbitan laporan ini terutamanya
kepada semua Pihak Berkuasa Tempatan, Pemaju, Pengurus Harta, Pemilik Bangunan, Pejabat Tanah dan lain-lain agensi
Kerajaan yang terlibat di dalam memberikan input bagi tujuan penerbitan berkala ini. Tanpa sokongan tuan, kami tidak
mungkin dapat menerbitkan laporan ini.

Seperti yang telah diketahui, pasaran harta tanah yang sihat dan stabil tidak sahaja penting bagi individu tetapi juga kepada
ekonomi negara pada keseluruhannya. Oleh itu, kami akan sentiasa memastikan laporan yang disediakan kepada pembaca
adalah berkualiti dan menepati masa. Kami sangat mengalu-alukan maklum balas, komen serta pandangan daripada
pembaca untuk memperbaiki lagi laporan ini. Kami boleh dihubungi melalui telefon, faksimili atau emel kepada:

Pengarah
Pusat Maklumat Harta Tanah Negara (NAPIC)
Jabatan Penilaian dan Perkhidmatan Harta
Kementerian Kewangan Malaysia
Aras 7, Perbendaharaan 2
No 7, Pesiaran Perdana
Presint 2
62592 Putrajaya

Tel     : 03-8886 9000
Fax     : 03-8886 9007
Email   : napic@jpph.gov.my
"""

FOREWORD_ENG = """
The Property Market Stock Report disseminates informations on the following scopes:

i.      Existing inventories of properties on a sectorial basis namely residential, commercial, industry and leisure.
ii.     Future supply comprises Incoming Supply, Construction Starts and Planned Supply.

Please be informed that all the data tabulated should be read in line with NAPIC’s Technical Notes attached in the report.
Information pertaining to commercial properties ie shopping complex is categorized into three sections ie shopping centre,
arcade and hypermarket whilst for purpose built office designated for publicly owned and private ownership.

We would like to express our gratitude to all those who had made this publication a success. Specifically, we wish to thank
all local councils, developers, property managers/building owners, land offices nation wide and other relevant government
bodies for giving their valuable inputs to make this quarterly survey a success. Without your support we will not be able to
publish this report.

It is a known fact that a healthy and stable property market is crucial to not only the individuals but also to the country’s
economy as a whole. Therefore, it is our utmost wish to provide readers with high quality information in a timely manner. We
welcome feedback, comments and suggestions from our readers to further improve this report. You may call, write, fax or
email to us.

Director
National Property Information Centre (NAPIC)
Valuation and Property Services Department
Ministry Of Finance Malaysia
Level 7, Perbendaharaan 2
No 7, Pesiaran Perdana
Precinct 2
62592 Putrajaya

Tel     : 03-8886 9000
Fax     : 03-8886 9007
Email   : https://napic2.jpph.gov.my
"""


# -------------------------------------------------------
# Agent Instructions
# -------------------------------------------------------
#   Contents that need to be written:
#   1.0     Residential Property Stock Report - Harta Tanah Kediaman/ Residential Property
#   2.0     Shop Property Stock Report - Kedai/Shop
#   3.0     Serviced Apartment Property Stock Report - Pangsapuri Khidmat/Serviced Apartment
#   4.0     Shopping Complex Property Stock Report - Kompleks Perniagaan/Shopping Complex
#   5.0     Purpose-Built Office Stock Report - Pejabat Binaan Khas/Purpose-Built Office
#   6.0     Industrial Property Stock Report - Harta Tanah Industri/Industrial Property
#   7.0     Leisure Property Stock Report - Harta Tanah Riadah / Leisure Property

ENG_REPORT_INSTRUCTIONS = """
You are an expert report writer for Malaysia’s National Property Information Centre (NAPIC).
Write a formal, factual English narrative that mirrors the style of the official “Property Stock Report”.

INPUT
- A single JSON with keys: title, period, generated_on, source, technical_notes_version, executive_summary_seed,
  and sections: residential, shop, serviced_apartment, shopping_complex, purpose_built_office, industrial, leisure.
- Each section includes per-state stock (existing, incoming, planned) or floor space (s.m.) and time-series trends.
- Use ONLY the numbers provided. Do NOT invent values. If a figure is absent, omit it.

OUTPUT (plain text, not HTML)
Produce a structured report in this exact order:

1) Title line (use the input 'title')
2) Period & Generated On (e.g., “Period: Jan–Jun 2025 | Generated on: 2025-10-28”)
3) Executive Summary (2–4 concise paragraphs)
   - Summarize national totals and the key directional movements from 'trends' (e.g., completions up, new planned down).
   - Mention top contributing states where relevant.
   - Keep neutral tone; avoid forecasts or opinions.

4) 1.0 Residential Property Stock Report
   - One overview paragraph: national totals (existing/incoming/planned), leaders by state (top 2–3).
   - Composition paragraph: landed vs stratified shares (existing and pipeline).
   - Activity paragraph: trends (completions, starts, new planned) with YoY indicated if provided.

5) 2.0 Shop Property Stock Report
   - Overview paragraph with state leaders and pipeline notes.
   - Trends paragraph using shop ‘trends’ series.

6) 3.0 Serviced Apartment Property Stock Report
   - Overview paragraph with concentration (e.g., W.P. Kuala Lumpur, Selangor, Johor).
   - Trends paragraph using serviced_apartment ‘trends’.

7) 4.0 Shopping Complex Property Stock Report
   - Overview paragraph with existing space (s.m.) leaders and pipeline (incoming/planned) in s.m.
   - If available, include composition by type (shopping centre / arcade / hypermarket).
   - Trends paragraph using shopping_complex ‘trends’ (space in s.m.).

8) 5.0 Purpose-Built Office Property Stock Report
   - Overview paragraph highlighting W.P. Kuala Lumpur and Selangor existing space.
   - Note any significant incoming/planned space by state.
   - Trends paragraph using purpose_built_office ‘trends’ (space in s.m.).

9) 6.0 Industrial Property Stock Report
   - Overview paragraph with national totals by state leaders (units) and pipeline.
   - Trends paragraph using industrial ‘trends’ (units).

10) 7.0 Leisure Property Stock Report
   - Overview paragraph using hotels/rooms (existing/incoming/planned).
   - Trends paragraph using leisure ‘trends’ (rooms).

STYLE & RULES
- Formal, neutral, evidence-based. No projections or advice.
- Use thousands separators (e.g., 1,745,057). Show units (units, s.m., rooms).
- Do not repeat raw tables; synthesize into narrative with selective figures (leaders, totals, significant changes).
- When figures are zero, avoid over-emphasis; write “minimal” or omit if it adds no value.
- Never reference “payload” or “JSON”; just write the report.

COMPUTATION GUIDANCE
- “Leaders”: pick top 2–3 states by existing stock/space; if close, mention ties briefly.
- “YoY change”: only state it if the payload includes yoy percentages for that section; otherwise stick to the trend direction qualitatively.
- Rounding: integers for counts and rooms; 0 decimals for s.m. values.

Return ONLY the report text (no markup).
"""

MALAY_REPORT_INSTRUCTIONS = """
Anda ialah penterjemah teknikal Bahasa Malaysia (BM) yang mematuhi gaya penulisan rasmi.
- Terjemah kandungan laporan ke Bahasa Malaysia tanpa mengubah makna.
- Kekalkan struktur seksyen, angka, unit dan istilah teknikal.
- Elakkan menambah fakta baharu; tiada ringkasan.
- Hasilkan teks (bukan HTML).
"""

HTML_MAKER_INSTRUCTIONS = f"""
You are an HTML builder for a printable A4 report.
- Input: English report text, Malay report text, and inventory rows.
- Output: A complete HTML document with:
 Page 1: Page 1: Title and meta info (Period, Generated On). Both title and meta info at the center of the page vertically and horizontally.
 Page 2: No title. {COPYRIGHT_BM!r} on the left, {COPYRIGHT_ENG!r} on the right. English version on the right is in italic.

 Page 3: Pendahuluan at the top as title (Use h3) at the center top instead start from left, and the content have to be just like from {PENDAHULUAN_BM!r}. Double <br/> for each blank line.
 Page 4: Foreword at the top as title (Use h3) at the center top instead start from left, and the content have to be just like from {FOREWORD_ENG!r}. Content and title in italic.

 Page 5: Main title is "RESIDENTIAL PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "1.0 HARTA TANAH KEDIAMAN" and "1.0 RESIDENTIAL PROPERTY" as title is bold (use h3).
    - Residential Property Stock Report Content.

 Page 6: Main title is "SHOP PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "2.0 KEDAI" and "2.0 SHOP" as title is bold (use h3).
    - Shop Property Stock Report Content.

 Page 7: Main title is "SERVICED APARTMENT PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "3.0 PANGSAPURI KHIDMAT" and "3.0 SERVICED APARTMENT" as title is bold (use h3).
    - Service Apartment Property Stock Report Content.

 Page 8: Main title is "SHOPPING COMPLEX PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "4.0 KOMPLEKS PERNIAGAAN" and "4.0 SHOPPING COMPLEX" as title is bold (use h3).
    -  Shopping Complex Report Content.

 Page 9: Main title is "PURPOSE-BUILT OFFICE PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "5.0 PEJABAT BINAAN KHAS" and "5.0 PURPOSE-BUILT OFFICE" as title is bold (use h3).
    - Purpose-Built Offi Stock Report Content.

 Page 10: Main title is "INDUSTRIAL PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "6.0 HARTA TANAH INDUSTRI" and "6.0 INDUSTRIAL PROPERTY" as title is bold (use h3).
    - Industrial Property Stock Report Content.

 Page 11: Main title is "LEISURE PROPERTY PROPERTY STOCK REPORT" at the top center (use h4).
    - Malay content on the left, english content on the right.
    - "7.0 HARTA TANAH RIADAH" and "7.0 LEISURE PROPERTY " as title is bold (use h3).
    - Leisure Property Stock Report Content.

- For every bilingual section, use EXACTLY this structure:
 <div class="two-column">
 <div class="column left">[Malay content]</div>
 <div class="column right">[English content]</div>
 </div>
- For the english content, make it italic.
- Do NOT use CSS columns or modern flexbox. The CSS provided uses table/table-cell layout.
- Use the provided CSS styles from {BASE_CSS!r} for overall formatting.
- Insert <div class="page-break"></div> between pages.
- Output only valid HTML starting with <!doctype html>.
- Do NOT include any explanations, comments, or code fences like ``` in the output.
- All paragraphs should be justified.
- NEVER use Markdown code fences or language tags (e.g., ```html). Return ONLY raw HTML starting with <!doctype html>.
- Follow all instructions give.
"""

# -------------------------------------------------------
# Azure agent helpers
# -------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """
    Remove Markdown code fences like ```html ... ``` or ``` ... ```,
    and triple-quote blocks, then trim leading/trailing whitespace.
    """
    if not text:
        return text or ""

    # Strip triple backtick blocks (with or without a language tag)
    m = re.search(r"```(?:[a-zA-Z]+)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)

    # Strip triple single-quote blocks if the model uses '''html
    m2 = re.search(r"'''(?:[a-zA-Z]+)?\s*(.*?)'''", text, flags=re.S)
    if m2:
        text = m2.group(1)

    # Remove a leading 'html' token that sometimes appears after a language tag
    text = re.sub(r'^\s*html\s*', '', text, flags=re.I)

    return text.strip()

def create_or_get_agent(name: str, instructions: str):
    for a in _agents_client.list_agents():
        if getattr(a, "name", "") == name:
            updated = _agents_client.update_agent(
                agent_id=a.id,
                model=MODEL_DEPLOYMENT_NAME,
                instructions=instructions
            )
            return updated
    created = _agents_client.create_agent(
        model=MODEL_DEPLOYMENT_NAME,
        name=name,
        instructions=instructions
    )
    return created

def _ensure_agents():
    """Create/update agents once; safe to call repeatedly."""
    global _eng_agent, _malay_agent, _html_maker_agent, _init_error
    _ensure_clients()
    if _init_error or not _agents_client:
        return
    if _eng_agent and _malay_agent and _html_maker_agent:
        return
    with _init_lock:
        if _eng_agent and _malay_agent and _html_maker_agent:
            return
        try:
            _eng_agent = create_or_get_agent("eng-report-agent", ENG_REPORT_INSTRUCTIONS)
            _malay_agent = create_or_get_agent("malay-report-agent", MALAY_REPORT_INSTRUCTIONS)
            _html_maker_agent = create_or_get_agent("html-maker-agent", HTML_MAKER_INSTRUCTIONS)
            _init_error = None
        except Exception as e:
            _init_error = f"Agent provisioning failed: {e}"

def run_agent_text(agent, user_payload: dict, system_hint: str = "") -> str:
    thread = _agents_client.threads.create()
    content = json.dumps({"hint": system_hint, "input": user_payload}, ensure_ascii=False)
    _agents_client.messages.create(
        thread_id=thread.id,
        role="user",
        content=[{"type": "text", "text": content}]
    )
    run = _agents_client.runs.create(thread_id=thread.id, agent_id=agent.id)
    # Poll until completion (basic loop; add timeouts/backoff in production)
    while True:
        status = _agents_client.runs.get(thread_id=thread.id, run_id=run.id).status
        if status in ["completed", "failed", "cancelled"]:
            break
        time.sleep(0.75)
    if status != "completed":
        raise RuntimeError(f"Run status = {status}")
    msgs = _agents_client.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING)
    texts = []
    for m in msgs:
        if m.role == MessageRole.AGENT:
            for t in getattr(m, "text_messages", []) or []:
                texts.append(t.text.value)
    return "\n".join(texts).strip()

def orchestrate_report(payload: dict) -> dict:
    _ensure_agents()
    if _init_error:
        raise RuntimeError(_init_error)
    english_report_text = run_agent_text(
        _eng_agent,
        user_payload=payload,
        system_hint="Produce a formal english report based on the input data."
    )
    malay_report_text = run_agent_text(
        _malay_agent,
        user_payload={"english_report_text": english_report_text},
        system_hint="Terjemah ke Bahasa Malaysia dengan gaya rasmi mengikut arahan."
    )
    html_payload = {
        "title": payload.get("title", ""),
        "period": payload.get("period", ""),
        "generated_on": payload.get("generated_on", datetime.now().strftime("%Y-%m-%d")),
        "english_report_text": english_report_text,
        "malay_report_text": malay_report_text,
        "rows": payload.get("rows", []),
    }
    html_code = run_agent_text(
        _html_maker_agent,
        user_payload=html_payload,
        system_hint="Generate HTML Document fully by instructions"
    )
    html_code = _strip_code_fences(html_code)
    return {
        "english_report_text": english_report_text,
        "malay_report_text": malay_report_text,
        "html": html_code
    }

def save_outputs(html_str: str, base_filename: str, out_dir: Path) -> Tuple[Path, Path, Path]:
    """Returns (html_path, pdf_path, docx_path)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{base_filename}.html"
    pdf_path = out_dir / f"{base_filename}.pdf"
    docx_path = out_dir / f"{base_filename}.docx"
    # Save HTML
    html_path.write_text(html_str, encoding="utf-8")
    # HTML -> PDF
    if PDFKIT_CONFIG:
        pdfkit.from_string(html_str, str(pdf_path), configuration=PDFKIT_CONFIG)
    else:
        pdfkit.from_string(html_str, str(pdf_path))
    # HTML -> DOCX (requires Pandoc installed)
    # pypandoc.convert_text(
    #     html_str,
    #     "docx",
    #     format="html",
    #     outputfile=str(docx_path),
    #     extra_args=["--standalone"]
    # )
    parse(str(pdf_path), str(docx_path))
    return html_path, pdf_path, docx_path

def slugify(value: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in value.strip())
    return "_".join(filter(None, safe.split("_"))).lower() or "report"

# -------------------------------------------------------
# Flask app & routes
# -------------------------------------------------------
app = Flask(__name__)

@app.get("/health")
@require_api_key
def health():
    # Non-fatal health that also shows Azure readiness
    _ensure_clients()
    ready = bool(_agents_client and _eng_agent and _malay_agent and _html_maker_agent and not _init_error)
    return {"status": "ok", "azure_ready": ready, "init_error": _init_error, "time": datetime.now().isoformat()}, 200

@app.post("/api/report")
@require_api_key  # protect generation endpoint
def api_report():
    """
    POST /api/report?format=(links|zip|pdf|docx|html)
    Body: JSON payload similar to the example below.
    """
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    payload.setdefault("generated_on", datetime.now().strftime("%Y-%m-%d"))

    # 1) Call agents to build content
    try:
        outputs = orchestrate_report(payload)
    except Exception as e:
        # 503: app is up, but backend (identity/agents) isn't ready
        return jsonify({"error": f"Agent orchestration failed: {e}"}), 503

    # 2) Persist artifacts
    title = payload.get("title") or "Generated Report"
    base_filename = f"{slugify(title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job_dir = OUTPUT_DIR / base_filename
    try:
        html_path, pdf_path, docx_path = save_outputs(outputs["html"], base_filename, job_dir)
    except Exception as e:
        return jsonify({"error": f"Failed to convert or save outputs: {e}"}), 500

    fmt = (request.args.get("format") or "links").lower()

    # 3) Response modes
    if fmt == "links":
        return jsonify({
            "message": "Report generated.",
            "files": {
                "html": f"/download/{base_filename}/{base_filename}.html",
                "pdf": f"/download/{base_filename}/{base_filename}.pdf",
                "docx": f"/download/{base_filename}/{base_filename}.docx"
            },
            "id": base_filename
        }), 200
    elif fmt in {"pdf", "docx", "html"}:
        file_map = {"pdf": pdf_path, "docx": docx_path, "html": html_path}
        file_path = file_map[fmt]
        if not file_path.exists():
            return jsonify({"error": f"{fmt.upper()} not found."}), 404
        return send_file(file_path, as_attachment=True, download_name=file_path.name)
    elif fmt == "zip":
        # Stream a ZIP containing all files
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(html_path, arcname=html_path.name)
            zf.write(pdf_path, arcname=pdf_path.name)
            zf.write(docx_path, arcname=docx_path.name)
        mem.seek(0)
        return send_file(
            mem,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{base_filename}.zip"
        )
    else:
        return jsonify({"error": "Invalid 'format' query. Use links|zip|pdf|docx|html"}), 400

@app.get("/download/<job_id>/<filename>")
@require_api_key
def download(job_id: str, filename: str):
    # Only allow reads within OUTPUT_DIR/job_id
    job_dir = OUTPUT_DIR / job_id
    file_path = job_dir / filename
    if not job_dir.exists() or not file_path.exists():
        abort(404)
    # Basic safety: ensure file_path is inside OUTPUT_DIR
    try:
        file_path.relative_to(OUTPUT_DIR)
    except ValueError:
        abort(403)
    return send_file(file_path, as_attachment=True, download_name=file_path.name)

if __name__ == "__main__":
    # Run Flask locally
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=True)




