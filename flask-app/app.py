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
from pdf2docx import parse
from functools import wraps
from playwright.sync_api import sync_playwright

# Graphs
import base64
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from io import BytesIO
from matplotlib.ticker import FuncFormatter
import numpy as np

# ------------------------------------------------------------
# ------------------------------------------------------------
# Configuration & bootstrap
# ------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
load_dotenv()  # read .env
PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME")
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

# ------------------------------------------------------------
if not PROJECT_ENDPOINT or not MODEL_DEPOYMENT_NAME:
    raise RuntimeError("Missing PROJECT_ENDPOINT or MODEL_DEPLOYMENT_NAME in environment.")

# Optional: wkhtmltopdf path (Windows typical install path can be provided via env var)
# WKHTMLTOPDF_PATH = os.getenv("WKHTMLTOPDF_PATH")  # e.g., C:\\Program Files\\wkhtmltopdf\\bin\\wkhtmltopdf.exe
# if WKHTMLTOPDF_PATH and Path(WKHTMLTOPDF_PATH).exists():
#     PDFKIT_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
# else:
#     PDFKIT_CONFIG = None  # pdfkit will try to find wkhtmltopdf in PATH


def html_to_pdf_playwright(html_str: str, pdf_path: Path):
    """
    Render the given HTML string in Chromium (headless) and save as PDF.
    """
    with sync_playwright() as p:
        # Azure App Service (Linux) commonly needs --no-sandbox
        browser = p.chromium.launch(args=["--no-sandbox"])  # needed in locked-down sandboxes
        page = browser.new_page()
        # Load your HTML; waits until network is idle so CSS/fonts/images are ready
        page.set_content(html_str, wait_until="networkidle")
        # Use 'screen' to match what you see in Edge Ctrl+P (you can switch to 'print' if you rely on @media print)
        page.goto(html_str.as_uri(), wait_until="load")
        page.emulate_media(media="screen")
        # Generate PDF with backgrounds, A4 and margins (aligns with your CSS intent)
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
            displayHeaderFooter=False,  # optional
            headerTemplate="...",       # optional
            footerTemplate="...",       # optional
        )
        browser.close()

def html_to_pdf_playwright_from_file(html_path: Path, pdf_path: Path):
    """
    Open the saved HTML file via file:// URL and print to PDF.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        # Load via file:// so file:///... can be fetched
        page.goto(html_path.as_uri(), wait_until="load")  # <-- key change
        page.emulate_media(media="screen")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            prefer_css_page_size = True,
            # margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
        )
        browser.close()

# ------------------------------------------------------------
# LAZY Azure clients/agents (to avoid crash at import)
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# Base CSS
# ------------------------------------------------------------
# BASE_CSS = """

# @page { size: A4; margin: 20mm; }
# @page full-bg { margin: 0; }

# body { font-family: Arial, sans-serif; line-height: 1.4;}
# h1, h2 { color: #000; margin: 0 0 8px; }
# h4 { color: #000; margin: 0 0 8px; background-color: #bfdfff; padding-top: 10px; padding-bottom: 10px;}
# .meta { color: #666; font-size: 12px; margin-bottom: 18px; }
# table { border-collapse: collapse; width: 100%; }
# th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
# .page-break { page-break-before: always; }
# /* wkhtmltopdf-friendly two-column layout */
# .two-column { display: table; width: 100%; table-layout: fixed; }
# .column { display: table-cell; width: 50%; vertical-align: top; padding: 0 10px 0 0; }
# .column.right { padding: 0 0 0 10px; }
# body, .column { word-break: break-word; overflow-wrap: anywhere; }

# .full-bg-page { page: full-bg; background: #bfdfff; 
# """

BASE_CSS = """
/* ---------- Paged media ---------- */
@page { size: A4; margin: 20mm; }           /* default margins for all pages */
@page full-bg { margin: 0; }                /* named page with zero margins */

/* Ensure printed colors/backgrounds render in Chromium headless */
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

/* Global text & layout */
body { margin: 0; font-family: Arial, sans-serif; line-height: 1.4; }  /* avoid default body margin */
h1, h2 { color: #000; margin: 0 0 8px; }
h4 { color: #000; margin: 0 0 8px; background-color: #bfdfff; padding-top: 10px; padding-bottom: 10px; }
.meta { color: #666; font-size: 12px; margin-bottom: 18px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
.page-break { page-break-before: always; }

/* Two-column (table/table-cell) — keep your original approach */
.two-column { display: table; width: 100%; table-layout: fixed; }
.column { display: table-cell; width: 50%; vertical-align: top; padding: 0 10px 0 0; }
.column.right { padding: 0 0 0 10px; }
body, .column { word-break: break-word; overflow-wrap: anywhere; }

/* ---------- Full-bleed page (blue background) ---------- */
.full-bg-page { page: full-bg; background: #bfdfff; }
.full-bg-page .vcenter {
  display: table;
  width: 100%;
  height: 371mm;
}
.full-bg-page .vcenter > div {
  display: table-cell;
  vertical-align: middle;
  text-align: center;
}

/* Optional: consistent borders + centering for chart images */
.chart-img {
  border: 2px solid #333;
  display: block;
  margin: 0 auto 30px auto;   /* center horizontally + spacing */
  width: 800px;               /* matches your instructions */
}
"""

# BASE_CSS = """
# /* ---------- Paged media ---------- */
# @page { size: A4; margin: 20mm; }           /* default margins for all pages */
# @page full-bg { margin: 0; }                /* named page with zero margins */

# /* Ensure printed colors/backgrounds render in Chromium headless */
# * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

# /* Global text & layout */
# body { margin: 0; font-family: Arial, sans-serif; line-height: 1.4; }
# h1, h2 { color: #000; margin: 0 0 8px; }
# /* Your h4 style for regular pages; keep it if needed on non-full pages */
# h4 { color: #000; margin: 0 0 8px; background-color: #bfdfff; padding-top: 10px; padding-bottom: 10px; }

# .meta { color: #666; font-size: 12px; margin-bottom: 18px; }
# table { border-collapse: collapse; width: 100%; }
# th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
# .page-break { page-break-before: always; }

# /* Two-column (table/table-cell) */
# .two-column { display: table; width: 100%; table-layout: fixed; }
# .column { display: table-cell; width: 50%; vertical-align: top; padding: 0 10px 0 0; }
# .column.right { padding: 0 0 0 10px; }
# body, .column { word-break: break-word; overflow-wrap: anywhere; }

# /* ---------- Full-bleed page (blue background) ---------- */
# /* Make the container exactly one A4 page (portrait), no margins */
# .full-bg-page {
#   page: full-bg;                /* attach named @page with margin:0 */
#   background: #bfdfff;          /* paint whole page */
#   margin: 0; padding: 0;
#   width: 210mm;                 /* A4 width */
#   min-height: 297mm;            /* A4 height */
#   display: table;               /* table + table-cell for vertical centering */
#   break-inside: avoid-page;     /* keep this block on a single page */
# }

# /* Vertical centering cell */
# .full-bg-page .vcenter {
#   display: table-cell;
#   vertical-align: middle;
#   text-align: center;
# }

# /* OPTIONAL: use a class for chart borders/centering instead of inline styles */
# .chart-img {
#   border: 2px solid #333;
#   display: block;
#   margin: 0 auto 30px auto;   /* center horizontally + spacing */
#   width: 800px;
# }
# """

# ------------------------------------------------------------
# Static Content
# ------------------------------------------------------------
COPYRIGHT_BM = """
© Jabatan Penilaian dan Perkhidmatan Harta

Hak cipta terpelihara

Tidak dibenarkan mencetak semula mana-mana bahagian artikel, ilustrasi, dan isi kandungan laporan ini dalam apa juga bentuk dan dengan cara apa jua sama ada secara elektronik, mekanik, fotokopi, rakaman atau cara lain sebelum mendapat izin bertulis daripada penerbit. Penerbit tidak bertanggungjawab terhadap kesahihan maklumat yang terkandung dalam laporan ini. Maklumat dalam laporan ini tidak boleh digunakan dalam apa-apa timbang tara, dakwaan dan tindakan undangundang atau sebagai asas untuk kesimpulan lain. Laporan ini dibuat tertakluk kepada beberapa andaian dan batasan.
"""
COPYRIGHT_ENG = """
© Valuation and Property Services Department

Copyright Reserved

No part of this report may be reproduced, stored in a retrieval system, transmitted in any form or by any means electronic, mechanical, photocopying, recording or otherwise without the prior written permission of the publisher. No responsibility is accepted for the accuracy of information contained in this report. Material published in this report cannot be used in any arbitration, litigation and legal proceedings or as a basis for other conclusions. The report was constructed subject to a set of assumptions and limitations.
"""
PENDAHULUAN_BM = """
Laporan Stok Harta Tanah menyebarkan maklumat berdasarkan kepada skop berikut:

i. Stok sedia ada mengikut sub-sektor harta tanah iaitu kediaman, perdagangan, industri dan riadah.
ii. Penawaran hadapan yang terdiri daripada data penawaran akan datang, mula pembinaan dan penawaran yang dirancang.

Ingin dimaklumkan bahawa semua jadual data tersebut perlu dibaca seiring dengan catatan teknikal yang disertakan bersama laporan ini. Maklumat berkenaan harta tanah perdagangan iaitu kompleks perniagaan dikategorikan kepada pusat membeli belah, arked dan pasaraya besar manakala bagi pejabat binaan khas terdiri daripada pejabat kerajaan dan swasta.

Kami ingin merakamkan ucapan ribuan terima kasih kepada semua yang telah menjayakan penerbitan laporan ini terutamanya kepada semua Pihak Berkuasa Tempatan, Pemaju, Pengurus Harta, Pemilik Bangunan, Pejabat Tanah dan lain-lain agensi Kerajaan yang terlibat di dalam memberikan input bagi tujuan penerbitan berkala ini. Tanpa sokongan tuan, kami tidak mungkin dapat menerbitkan laporan ini.

Seperti yang telah diketahui, pasaran harta tanah yang sihat dan stabil tidak sahaja penting bagi individu tetapi juga kepada ekonomi negara pada keseluruhannya. Oleh itu, kami akan sentiasa memastikan laporan yang disediakan kepada pembaca adalah berkualiti dan menepati masa. Kami sangat mengalu-alukan maklum balas, komen serta pandangan daripada pembaca untuk memperbaiki lagi laporan ini. Kami boleh dihubungi melalui telefon, faksimili atau emel kepada:

Pengarah
Pusat Maklumat Harta Tanah Negara (NAPIC)
Jabatan Penilaian dan Perkhidmatan Harta
Kementerian Kewangan Malaysia
Aras 7, Perbendaharaan 2
No 7, Pesiaran Perdana
Presint 2
62592 Putrajaya

Tel : 03-8886 9000
Fax : 03-8886 9007
Email : napic@jpph.gov.my
"""
FOREWORD_ENG = """
The Property Market Stock Report disseminates informations on the following scopes:

i. Existing inventories of properties on a sectorial basis namely residential, commercial, industry and leisure.
ii. Future supply comprises Incoming Supply, Construction Starts and Planned Supply.

Please be informed that all the data tabulated should be read in line with NAPIC’s Technical Notes attached in the report. Information pertaining to commercial properties ie shopping complex is categorized into three sections ie shopping centre, arcade and hypermarket whilst for purpose built office designated for publicly owned and private ownership.

We would like to express our gratitude to all those who had made this publication a success. Specifically, we wish to thank all local councils, developers, property managers/building owners, land offices nation wide and other relevant government bodies for giving their valuable inputs to make this quarterly survey a success. Without your support we will not be able to publish this report.

It is a known fact that a healthy and stable property market is crucial to not only the individuals but also to the country’s economy as a whole. Therefore, it is our utmost wish to provide readers with high quality information in a timely manner. We welcome feedback, comments and suggestions from our readers to further improve this report. You may call, write, fax or email to us.

Director
National Property Information Centre (NAPIC)
Valuation and Property Services Department
Ministry Of Finance Malaysia
Level 7, Perbendaharaan 2
No 7, Pesiaran Perdana
Precinct 2
62592 Putrajaya

Tel : 03-8886 9000
Fax : 03-8886 9007
Email : https://napic2.jpph.gov.my
"""

# ------------------------------------------------------------
# Agent Instructions
# ------------------------------------------------------------
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
2) Period & Generated On (e.g., “Period: Jan–Jun 2025 \n Generated on: 2025-10-28”)
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
- Input: English report text, Malay report text, and chart file URIs (file:///...).
- Output: A complete HTML document with:
 Page 1: Title and meta info (Period, Generated On).
    - Both title and meta info at the center of the page vertically and horizontally.
    - Title font-size: 3em

 Page 2: No title. {COPYRIGHT_BM!r} content on the left (wrap with <p></p>), {COPYRIGHT_ENG!r} content on the right (wrap with <p></p>). English version on the right is in italic.
    - The content should be at the center of the page vertically and horizontally.
    - The content should have margin-top 200mm.
    - Make the <div> that wrapping this content height: 0mm.
 
 Page 3: Pendahuluan at the top as title (Use h3) at the center top instead start from left, and the content have to be just like from {PENDAHULUAN_BM!r}. Double <br/> for each blank line.

 Page 4: Foreword at the top as title (Use h3) at the center top instead start from left, and the content have to be just like from {FOREWORD_ENG!r}. Content and title in italic.

 Page 5: Main title is "RESIDENTIAL PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "1.0 HARTA TANAH KEDIAMAN" and "1.0 RESIDENTIAL PROPERTY" as title is bold (use h3). Align to left.
    - Residential Property Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.existing_by_state }}
        </div>
        <div>
            {{ charts.residential.supply_by_state }}
        </div>
        <div>
            {{ charts.residential.trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 6: Main title is "SHOP PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "2.0 KEDAI" and "2.0 SHOP" as title is bold (use h3). Align to left.
    - Shop Property Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;">
            {{ charts.residential.shop_existing_by_state }}
        </div>
        <div>
            {{ charts.residential.shop_supply_by_state }}
        </div>
        <div>
            {{ charts.residential.shop_trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 7: Main title is "SERVICED APARTMENT PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 20px for this h4 title.
    - Malay content on the left, english content on the right.
    - "3.0 PANGSAPURI KHIDMAT" and "3.0 SERVICED APARTMENT" as title is bold (use h3). Align to left.
    - Service Apartment Property Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.servap_existing_by_state }}
        </div>
        <div>
            {{ charts.residential.servap_supply_by_state }}
        </div>
        <div>
            {{ charts.residential.servap_trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 8: Main title is "SHOPPING COMPLEX PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "4.0 KOMPLEKS PERNIAGAAN" and "4.0 SHOPPING COMPLEX" as title is bold (use h3). Align to left.
    - Shopping Complex Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.shopcx_existing_space_by_state }}
        </div>
        <div>
            {{ charts.residential.shopcx_supply_space_by_state }}
        </div>
        <div>
            {{ charts.residential.shopcx_trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 9: Main title is "PURPOSE-BUILT OFFICE PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "5.0 PEJABAT BINAAN KHAS" and "5.0 PURPOSE-BUILT OFFICE" as title is bold (use h3). Align to left.
    - Purpose-Built Offi Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.pbo_existing_space_by_state }}
        </div>
        <div>
            {{ charts.residential.pbo_supply_space_by_state }}
        </div>
        <div>
            {{ charts.residential.pbo_trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 10: Main title is "INDUSTRIAL PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "6.0 HARTA TANAH INDUSTRI" and "6.0 INDUSTRIAL PROPERTY" as title is bold (use h3). Align to left.
    - Industrial Property Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.industrial_existing_by_state }}
        </div>
        <div>
            {{ charts.residential.industrial_supply_by_state }}
        </div>
        <div>
            {{ charts.residential.industrial_trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.

 Page 11: Main title is "LEISURE PROPERTY PROPERTY STOCK REPORT" at the top center (use h4). Put margin-below 10px for this h4 title.
    - Malay content on the left, english content on the right.
    - "7.0 HARTA TANAH RIADAH" and "7.0 LEISURE PROPERTY " as title is bold (use h3). Align to left.
    - Leisure Property Stock Report Content without the title from the content.
    - Content should be wrap in <p></p>
    - Then render these four charts below the narrative using ... tags (do NOT inline base64):
        <div style="margin-top: 10px;>
            {{ charts.residential.existing_by_state }}
        </div>
        <div>
            {{ charts.residential.supply_by_state }}
        </div>
        <div>
            {{ charts.residential.trends }}
        </div>
    - These graphs picture should be at the center horizontally.
    - These graphs picture, put style="width: 800px; margin-bottoms: 30px;"
    - The graph pictures should have thin black borders at least 2px.
    - No break-page after this page.

 
Page 12: Render a full-bleed page with #bfdfff background.
    - Use this exact structure:
    <div class="full-bg-page">
        <div class="vcenter">
        <div>
            <h1 style="font-size:3em;">Catatan Teknikal</h1>
            <h1 style="font-size:2em">Technical Notes</h1>
        </div>
        </div>
    </div>
    - No break-page at the top of this page.

Page 13: Put the Technical Notes Version

- For every bilingual section, use EXACTLY this structure:
 <div class="two-column">
    <div class="column left">[Malay content]</div>
    <div class="column right"><i>[English content]</i></div>
 </div>
- For the english content, make it italic.
- Do NOT use CSS columns or modern flexbox. The CSS provided uses table/table-cell layout.
- Use the provided CSS styles from {BASE_CSS!r} for overall formatting.
- Insert <div class="page-break"></div> between pages.
- Output only valid HTML starting with <!doctype html>.
- All paragraphs should be justified.
- NEVER use Markdown code fences or language tags (e.g., ```html). Return ONLY raw HTML starting with <!doctype html>.
- Don't make the font too small.
- Follow all instructions give.
"""

# ------------------------------------------------------------
# Azure agent helpers
# ------------------------------------------------------------
def _strip_code_fences(text: str) -> str:
    """
    Remove Markdown code fences like ```html ... ``` or ``` ... ```,
    and triple-quote blocks, then trim leading/trailing whitespace.
    """
    if not text:
        return text or ""
    # Strip triple backtick blocks (with or without a language tag)
    m = re.search(r"\`\`\`(?:[a-zA-Z]+)?\s*(.*?)\`\`\`", text, flags=re.S)
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
                model=MODEL_DEPOYMENT_NAME,
                instructions=instructions
            )
            return updated
    created = _agents_client.create_agent(
        model=MODEL_DEPOYMENT_NAME,
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

    # --- NEW: Create job dir early to store chart files ---
    title = payload.get("title") or "Generated Report"
    base_filename = f"{slugify(title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    job_dir = OUTPUT_DIR / base_filename

    # Build chart PNGs and return file:/// URIs
    charts = generate_all_charts(payload, job_dir)

    html_payload = {
        "title": payload.get("title", ""),
        "period": payload.get("period", ""),
        "generated_on": payload.get("generated_on", datetime.now().strftime("%Y-%m-%d")),
        "english_report_text": english_report_text,
        "malay_report_text": malay_report_text,
        "rows": payload.get("rows", []),
        "charts": charts,
    }

    html_code = run_agent_text(
        _html_maker_agent,
        user_payload=html_payload,
        system_hint="Generate HTML Document fully by instructions"
    )
    html_code = _strip_code_fences(html_code)

    return {
        "id": base_filename,                # NEW: return id for downstream saving
        "english_report_text": english_report_text,
        "malay_report_text": malay_report_text,
        "html": html_code,
        "charts": charts,
    }

def save_outputs(html_str: str, base_filename: str, out_dir: Path) -> Tuple[Path, Path, Path]:
    """Returns (html_path, pdf_path, docx_path)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{base_filename}.html"
    pdf_path = out_dir / f"{base_filename}.pdf"
    docx_path = out_dir / f"{base_filename}.docx"

    # Save HTML
    html_path.write_text(html_str, encoding="utf-8")

    # --- NEW: allow local file access so wkhtmltopdf can load file:// images ---
    # wk_opts = {"enable-local-file-access": None}

    # # HTML -> PDF
    # if PDFKIT_CONFIG:
    #     pdfkit.from_string(html_str, str(pdf_path), configuration=PDFKIT_CONFIG, options=wk_opts)
    # else:
    #     pdfkit.from_string(html_str, str(pdf_path), options=wk_opts)

    
    # HTML -> PDF (Playwright)
    # html_to_pdf_playwright(html_str, pdf_path)
    html_to_pdf_playwright_from_file(html_path, pdf_path)


    # HTML -> DOCX (requires Pandoc installed)
    parse(str(pdf_path), str(docx_path))
    return html_path, pdf_path, docx_path

def slugify(value: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in value.strip())
    return "_".join(filter(None, safe.split("_"))).lower() or "report"

# ------------------------------------------------------------
# Chart generation (minimal changes only)
# ------------------------------------------------------------
sns.set_theme(style="whitegrid")
COLORS = {
    "primary": "#0d6efd",
    "success": "#20c997",
    "warning": "#ffc107",
    "muted": "#6c757d",
    "accent1": "#198754",
    "accent2": "#6610f2",
}

def _thousands(x, pos=None):
    # thousands separators for units/rooms/s.m.
    return f"{int(x):,}"

def _fig_to_data_uri(fig, dpi=160):
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    buf.seek(0)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    plt.close(fig)
    return f"data:image/png;base64,{b64}"

# --- NEW: save figure to disk and return file:/// URI ---
def _fig_to_file_uri(fig, out_dir: Path, filename: str, dpi=160) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{filename}.png"
    fig.savefig(path, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path.as_uri()

class ChartFactory:
    def __init__(self, figsize=(10, 4.8), out_dir: Path | None = None, prefix: str = "chart"):
        self.figsize = figsize
        self.out_dir = out_dir or (OUTPUT_DIR / "charts")
        self.prefix = prefix

    def bar(self, labels, values, title, ylabel, color=COLORS["primary"], xrot=45, name="bar"):
        fig, ax = plt.subplots(figsize=self.figsize)
        sns.barplot(x=labels, y=values, ax=ax, color=color)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("")
        ax.yaxis.set_major_formatter(FuncFormatter(_thousands))
        ax.tick_params(axis='x', rotation=xrot, labelsize=8)
        return _fig_to_file_uri(fig, self.out_dir, name)

    def grouped_bar(self, labels, series, title, ylabel, colors=None, xrot=45, name="grouped_bar"):
        colors = colors or [COLORS["success"], COLORS["warning"]]
        width = 0.4
        x = range(len(labels))
        fig, ax = plt.subplots(figsize=self.figsize)
        keys = list(series.keys())
        left = [i - width/2 for i in x]
        right = [i + width/2 for i in x]
        ax.bar(left, series[keys[0]], width, label=keys[0], color=colors[0])
        ax.bar(right, series[keys[1]], width, label=keys[1], color=colors[1])
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.yaxis.set_major_formatter(FuncFormatter(_thousands))
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=xrot, ha='right', fontsize=8)
        ax.legend()
        return _fig_to_file_uri(fig, self.out_dir, name)

    def line(self, x_labels, series, title, ylabel, colors=None, name="line"):
        colors = colors or [COLORS["primary"], COLORS["success"], COLORS["warning"]]
        fig, ax = plt.subplots(figsize=self.figsize)
        for idx, (name_k, values) in enumerate(series.items()):
            ax.plot(x_labels, values, marker="o", label=name_k, color=colors[idx % len(colors)])
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("")
        ax.yaxis.set_major_formatter(FuncFormatter(_thousands))
        plt.xticks(rotation=45)
        ax.legend()
        return _fig_to_file_uri(fig, self.out_dir, name)

    def donut(self, labels, shares_pct, title, colors=None, name="donut"):
        colors = colors or [COLORS["primary"], COLORS["muted"]]
        fig, ax = plt.subplots(figsize=(6.5, 4.0))
        wedges, texts, autotexts = ax.pie(
            shares_pct,
            labels=labels,
            colors=colors,
            startangle=90,
            autopct=lambda p: f"{p:.1f}%",
            pctdistance=0.85
        )
        centre = plt.Circle((0, 0), 0.60, fc='white')
        fig.gca().add_artist(centre)
        ax.set_title(title)
        return _fig_to_file_uri(fig, self.out_dir, name)

    def horiz_bar(self, labels, values, title, xlabel, color=COLORS["primary"], name="horiz_bar"):
        fig, ax = plt.subplots(figsize=self.figsize)
        sns.barplot(y=labels, x=values, ax=ax, color=color, orient="h")
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("")
        ax.xaxis.set_major_formatter(FuncFormatter(_thousands))
        ax.tick_params(axis='y', labelsize=8)
        return _fig_to_file_uri(fig, self.out_dir, name)

    def grouped_bar_n(self, labels, series: dict, title, ylabel, palette=None, xrot=45, name="grouped_bar_n"):
        keys = list(series.keys())
        n = len(keys)
        palette = palette or [COLORS["primary"], COLORS["success"], COLORS["warning"], COLORS["muted"], COLORS["accent1"], COLORS["accent2"]]
        width = 0.8 / max(n, 1)
        x = np.arange(len(labels))
        fig, ax = plt.subplots(figsize=self.figsize)
        for i, k in enumerate(keys):
            ax.bar(x + (i - (n-1)/2) * width, series[k], width, label=k, color=palette[i % len(palette)])
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=xrot, ha='right', fontsize=8)
        ax.yaxis.set_major_formatter(FuncFormatter(_thousands))
        ax.legend()
        return _fig_to_file_uri(fig, self.out_dir, name)

    def stacked_bar(self, labels, series: dict, title, ylabel, percent=False, palette=None, xrot=45, name="stacked_bar"):
        keys = list(series.keys())
        palette = palette or [COLORS["primary"], COLORS["muted"], COLORS["warning"], COLORS["success"], COLORS["accent2"]]
        data = np.vstack([series[k] for k in keys])  # shape: (K, M)
        if percent:
            colsum = data.sum(axis=0)
            data = np.divide(data, np.where(colsum==0, 1, colsum)) * 100
        fig, ax = plt.subplots(figsize=self.figsize)
        bottom = np.zeros(data.shape[1])
        x = np.arange(len(labels))
        for i, k in enumerate(keys):
            ax.bar(x, data[i], bottom=bottom, label=k, color=palette[i % len(palette)])
            bottom += data[i]
        ax.set_title(title)
        ax.set_ylabel("Share (%)" if percent else ylabel)
        ax.set_xlabel("")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=xrot, ha='right', fontsize=8)
        if not percent:
            ax.yaxis.set_major_formatter(FuncFormatter(_thousands))
        ax.legend()
        return _fig_to_file_uri(fig, self.out_dir, name)

# ---------------------------------------------------------------------
# CHARTS GENERATION
# ---------------------------------------------------------------------
def charts_residential(res, charts_dir: Path):
    """Build all Residential charts (file URIs) from payload.sections.residential."""
    cf = ChartFactory(out_dir=charts_dir, prefix="residential")
    states = [s["state"] for s in res["by_state"]]
    existing = [s["existing"] for s in res["by_state"]]
    incoming = [s["incoming"] for s in res["by_state"]]
    planned = [s["planned"] for s in res["by_state"]]
    charts = {
        # 1) Horizontal bar – Existing stock by state
        "existing_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Residential Existing Stock by State (H1 2025)", xlabel="Units",
            name="residential_existing_by_state"
        ),
        # 2) Grouped bar – Incoming vs Planned by state (double bars)
        "supply_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Residential Incoming vs Planned Supply by State (H1 2025)",
            ylabel="Units",
            name="residential_supply_by_state"
        ),
        # 3) Donut – Landed vs Stratified shares (Existing)
        "composition_existing": cf.donut(
            labels=["Landed", "Stratified"],
            shares_pct=[
                res["composition"]["landed"]["share_pct"]["existing"],
                res["composition"]["stratified"]["share_pct"]["existing"],
            ],
            title="Distribution of Landed vs Stratified (Existing)",
            name="residential_composition_existing"
        ),
        # 4) Line – Trends H1 2022 → H1 2025
        "trends": cf.line(
            x_labels=res["trends"]["half_year"],
            series={
                "Completions": res["trends"]["completions"],
                "Starts": res["trends"]["starts"],
                "New Planned": res["trends"]["new_planned"],
            },
            title="Residential Trends (H1 2022 → H1 2025)",
            ylabel="Units",
            name="residential_trends"
        ),
    }
    return charts

def charts_shop(shop: dict, charts_dir: Path) -> dict:
    """Build Shop charts (file URIs)."""
    cf = ChartFactory(out_dir=charts_dir, prefix="shop")
    states   = [s["state"]    for s in shop["by_state"]]
    existing = [s["existing"] for s in shop["by_state"]]
    incoming = [s["incoming"] for s in shop["by_state"]]
    planned  = [s["planned"]  for s in shop["by_state"]]

    charts = {
        "existing_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Shop Existing Stock by State (H1 2025)", xlabel="Units",
            name="shop_existing_by_state"
        ),
        "supply_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Shop Incoming vs Planned Supply by State (H1 2025)",
            ylabel="Units",
            name="shop_supply_by_state"
        ),
        "trends": cf.line(
            x_labels=shop["trends"]["half_year"],
            series={
                "Completions": shop["trends"]["completions"],
                "Starts":      shop["trends"]["starts"],
                "New Planned": shop["trends"]["new_planned"],
            },
            title="Shop Trends (H1 2022 → H1 2025)",
            ylabel="Units",
            name="shop_trends"
        ),
    }
    return charts

def charts_serviced_apartment(serv: dict, charts_dir: Path) -> dict:
    """Build Serviced Apartment charts (file URIs)."""
    cf = ChartFactory(out_dir=charts_dir, prefix="serviced_apartment")
    states   = [s["state"]    for s in serv["by_state"]]
    existing = [s["existing"] for s in serv["by_state"]]
    incoming = [s["incoming"] for s in serv["by_state"]]
    planned  = [s["planned"]  for s in serv["by_state"]]

    charts = {
        "existing_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Serviced Apartment Existing Stock by State (H1 2025)", xlabel="Units",
            name="servap_existing_by_state"
        ),
        "supply_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Serviced Apartment Incoming vs Planned Supply by State (H1 2025)",
            ylabel="Units",
            name="servap_supply_by_state"
        ),
        "trends": cf.line(
            x_labels=serv["trends"]["half_year"],
            series={
                "Completions": serv["trends"]["completions"],
                "Starts":      serv["trends"]["starts"],
                "New Planned": serv["trends"]["new_planned"],
            },
            title="Serviced Apartment Trends (H1 2022 → H1 2025)",
            ylabel="Units",
            name="servap_trends"
        ),
    }
    return charts

def charts_shopping_complex(sc: dict, charts_dir: Path) -> dict:
    """Build Shopping Complex charts (file URIs). Uses space (s.m.)."""
    cf = ChartFactory(out_dir=charts_dir, prefix="shopping_complex")
    states    = [s["state"] for s in sc["by_state"]]
    existing  = [s["existing_space_sm"]  for s in sc["by_state"]]
    incoming  = [s["incoming_space_sm"]  for s in sc["by_state"]]
    planned   = [s["planned_space_sm"]   for s in sc["by_state"]]

    charts = {
        "existing_space_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Shopping Complex Existing Space by State (H1 2025)", xlabel="Total Space (s.m.)",
            name="shopcx_existing_space_by_state"
        ),
        "supply_space_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Shopping Complex Incoming vs Planned Space by State (H1 2025)",
            ylabel="Total Space (s.m.)",
            name="shopcx_supply_space_by_state"
        ),
        "trends": cf.line(
            x_labels=sc["trends"]["half_year"],
            series={
                "Completions":   sc["trends"]["completions_space_sm"],
                "Starts":        sc["trends"]["starts_space_sm"],
                "New Planned":   sc["trends"]["new_planned_space_sm"],
            },
            title="Shopping Complex Trends (H1 2022 → H1 2025)",
            ylabel="Total Space (s.m.)",
            name="shopcx_trends"
        ),
    }
    return charts

def charts_purpose_built_office(pbo: dict, charts_dir: Path) -> dict:
    """Build Purpose-Built Office charts (file URIs). Uses space (s.m.)."""
    cf = ChartFactory(out_dir=charts_dir, prefix="purpose_built_office")
    states    = [s["state"] for s in pbo["by_state"]]
    existing  = [s["existing_space_sm"]  for s in pbo["by_state"]]
    incoming  = [s["incoming_space_sm"]  for s in pbo["by_state"]]
    planned   = [s["planned_space_sm"]   for s in pbo["by_state"]]

    charts = {
        "existing_space_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Purpose-Built Office Existing Space by State (H1 2025)", xlabel="Total Space (s.m.)",
            name="pbo_existing_space_by_state"
        ),
        "supply_space_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Purpose-Built Office Incoming vs Planned Space by State (H1 2025)",
            ylabel="Total Space (s.m.)",
            name="pbo_supply_space_by_state"
        ),
        "trends": cf.line(
            x_labels=pbo["trends"]["half_year"],
            series={
                "Completions":   pbo["trends"]["completions_space_sm"],
                "Starts":        pbo["trends"]["starts_space_sm"],
                "New Planned":   pbo["trends"]["new_planned_space_sm"],
            },
            title="Purpose-Built Office Trends (H1 2022 → H1 2025)",
            ylabel="Total Space (s.m.)",
            name="pbo_trends"
        ),
    }
    return charts

def charts_industrial(ind: dict, charts_dir: Path) -> dict:
    """Build Industrial Property charts (file URIs)."""
    cf = ChartFactory(out_dir=charts_dir, prefix="industrial")
    states   = [s["state"]    for s in ind["by_state"]]
    existing = [s["existing"] for s in ind["by_state"]]
    incoming = [s["incoming"] for s in ind["by_state"]]
    planned  = [s["planned"]  for s in ind["by_state"]]

    charts = {
        "existing_by_state": cf.horiz_bar(
            labels=states, values=existing,
            title="Industrial Existing Stock by State (H1 2025)", xlabel="Units",
            name="industrial_existing_by_state"
        ),
        "supply_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": incoming, "Planned": planned},
            title="Industrial Incoming vs Planned Supply by State (H1 2025)",
            ylabel="Units",
            name="industrial_supply_by_state"
        ),
        "trends": cf.line(
            x_labels=ind["trends"]["half_year"],
            series={
                "Completions": ind["trends"]["completions"],
                "Starts":      ind["trends"]["starts"],
                "New Planned": ind["trends"]["new_planned"],
            },
            title="Industrial Trends (H1 2022 → H1 2025)",
            ylabel="Units",
            name="industrial_trends"
        ),
    }
    return charts


def charts_leisure(leisure: dict, charts_dir: Path) -> dict:
    """Build Leisure (Hotel) charts (file URIs). Uses rooms for charts."""
    cf = ChartFactory(out_dir=charts_dir, prefix="leisure")
    states   = [s["state"]        for s in leisure["by_state"]]
    rooms_ex = [s["existing_rooms"] for s in leisure["by_state"]]
    rooms_in = [s["incoming_rooms"] for s in leisure["by_state"]]
    rooms_pl = [s["planned_rooms"]  for s in leisure["by_state"]]

    charts = {
        "existing_rooms_by_state": cf.horiz_bar(
            labels=states, values=rooms_ex,
            title="Hotel Existing Rooms by State (H1 2025)", xlabel="No. of Room",
            name="leisure_existing_rooms_by_state"
        ),
        "supply_rooms_by_state": cf.grouped_bar_n(
            labels=states,
            series={"Incoming": rooms_in, "Planned": rooms_pl},
            title="Hotel Incoming vs Planned Rooms by State (H1 2025)",
            ylabel="No. of Room",
            name="leisure_supply_rooms_by_state"
        ),
        "trends": cf.line(
            x_labels=leisure["trends"]["half_year"],
            series={
                "Completions":   leisure["trends"]["completions_rooms"],
                "Starts":        leisure["trends"]["starts_rooms"],
                "New Planned":   leisure["trends"]["new_planned_rooms"],
            },
            title="Hotel Trends (H1 2022 → H1 2025)",
            ylabel="No. of Room",
            name="leisure_trends"
        ),
    }
    return charts

# Funtion to generate all others charts
def generate_all_charts(payload: dict, job_dir: Path) -> dict:
    sec = payload.get("sections", {})
    charts_dir = job_dir / "charts"
    charts = {}
    if "residential" in sec:
        charts["residential"] = charts_residential(sec["residential"], charts_dir)
    
    if "shop" in sec:
        charts["shop"] = charts_shop(sec["shop"], charts_dir)

    if "serviced_apartment" in sec:
        charts["serviced_apartment"] = charts_serviced_apartment(sec["serviced_apartment"], charts_dir)
    
    if "shopping_complex" in sec:
        charts["shopping_complex"] = charts_shopping_complex(sec["shopping_complex"], charts_dir)
    
    if "purpose_built_office" in sec:
        charts["purpose_built_office"] = charts_purpose_built_office(sec["purpose_built_office"], charts_dir)
    
    if "industrial" in sec:
        charts["industrial"] = charts_industrial(sec["industrial"], charts_dir)
    
    if "leisure" in sec:
        charts["leisure"] = charts_leisure(sec["leisure"], charts_dir)

    return charts

# ------------------------------------------------------------
# Flask app & routes
# ------------------------------------------------------------
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

    # 2) Persist artifacts (use orchestrator's id & dir)
    base_filename = outputs["id"]                           # NEW: reuse id
    job_dir = OUTPUT_DIR / base_filename                    # NEW: same dir where charts were saved

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

