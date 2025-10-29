import os
import json
import time
from datetime import datetime

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    ListSortOrder,
    MessageRole,
)

import pdfkit
from jinja2 import Template
import pypandoc

# Load environment variables from .env file
load_dotenv()
PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME")

# To check if the environment variables are loaded correctly
if not PROJECT_ENDPOINT or not MODEL_DEPLOYMENT_NAME:
  raise RuntimeError("Missing PROJECT_ENDPOINT or MODEL_DEPLOYMENT_NAME in environment.")

# Azure Clients
credential = DefaultAzureCredential()
project_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)
agents_client = project_client.agents

# ---------------------------------------------------------------------------------------------------------
# FUNCTIONS SECTION
# ---------------------------------------------------------------------------------------------------------

# Create or get an agent by name
def create_or_get_agent(name:str, instructions: str):
  for a in agents_client.list_agents():
    if getattr(a, "name", "") == name:
      updated = agents_client.update_agent(
         agent_id = a.id,
         model = MODEL_DEPLOYMENT_NAME,
         instructions = instructions
      )
      return updated
  
  agentsList = agents_client.create_agent(
    model = MODEL_DEPLOYMENT_NAME,
    name = name,
    instructions = instructions
  )

  return agentsList



# Run agent with text input and get text output
def run_agent_text(agent, user_payload: dict, system_hint: str = "") -> str:
  thread = agents_client.threads.create()
  content = json.dumps({
      "hint": system_hint,
      "input": user_payload
  }, ensure_ascii=False)

  agents_client.messages.create(
      thread_id=thread.id,
      role="user",
      # If you need to pass files, the SDK supports file attachments as well.
      # Here we send JSON as a single text message.
      content=[{"type": "text", "text": content}]
  )

  run = agents_client.runs.create(thread_id=thread.id, agent_id=agent.id)

  # Simple poll loop; in production use exponential backoff/timeouts
  while True:
      status = agents_client.runs.get(thread_id=thread.id, run_id=run.id).status
      if status in ["completed", "failed", "cancelled"]:
          break
      time.sleep(0.75)

  if status != "completed":
      raise RuntimeError(f"Run status = {status}")

  # Fetch messages in ascending order; extract last agent message text
  msgs = agents_client.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING)
  texts = []
  for m in msgs:
      if m.role == MessageRole.AGENT:
          # Each agent message can have multiple text segments
          for t in getattr(m, "text_messages", []) or []:
              texts.append(t.text.value)
  return "\n".join(texts).strip()



# Convert HTML output to PDF and DOCX
def save_outputs(html_str: str, pdf_path="output.pdf", docx_path="output.docx"):
  # HTML to PDF
  pdfkit.from_string(html_str, pdf_path)

  # HTML to DOCX
  pypandoc.convert_text(html_str, "docx", format="html", outputfile=docx_path, extra_args=["--standalone"])

  return pdf_path, docx_path



# Run agent to create report
def orchestrate_report(payload: dict):

  # Step 1: Generate English Report Text
  english_report_text = run_agent_text(
    eng_agent,
    user_payload= payload,
    system_hint = "Produce a formal english report based on the input data."
  )

  # Step 2: Translate to Malay Report Text
  malay_report_text = run_agent_text(
    malay_agent,
    user_payload = {"english_report_text": english_report_text},
    system_hint = "Terjemah ke Bahasa Malaysia dengan gaya rasmi mengikut arahan."
  )

  
  # Step 3: HTML generation
  html_payload = {
      "title": payload.get("title", ""),
      "period": payload.get("period", ""),
      "generated_on": payload.get("generated_on", datetime.now().strftime("%Y-%m-%d")),
      "english_report_text": english_report_text,
      "malay_report_text": malay_report_text,
      "rows": payload.get("rows", []),
  }

  html_code = run_agent_text(
    html_maker_agent,
    user_payload = html_payload,
    system_hint = "Generate HTML Document fully by instructions"
  )

  return {
    "english_report_text": english_report_text,
    "malay_report_text": malay_report_text,
    "html": html_code
  }

# ---------------------------------------------------------------------------------------------------------






# ---------------------------------------------------------------------------------------------------------
# AGENTS CREATION SECTION
# ---------------------------------------------------------------------------------------------------------


BASE_CSS = """
page { size: A4; margin: 20mm; }
body { font-family: Arial, sans-serif; line-height: 1.4; }
h1, h2 { color: #0a5; margin: 0 0 8px; }
.meta { color: #666; font-size: 12px; margin-bottom: 18px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
.page-break { page-break-before: always; }

/* --- wkhtmltopdf-friendly two-column layout --- */
.two-column {
  display: -webkit-box;              /* legacy WebKit flex that wkhtmltopdf supports */
  -webkit-box-orient: horizontal;
  -webkit-box-pack: justify;         /* similar to justify-content: space-between */
}

.column {
  -webkit-box-flex: 1;               /* similar to flex: 1 */
  width: 49%;
  vertical-align: top;
  display: inline-block;             /* helps in older engines */
}

.column.left { margin-right: 2%; }   /* emulate 'gap' as wkhtmltopdf ignores it */

/* Optional float fallback if a build ignores -webkit-box */
.two-column.fallback .column {
  float: left;
  width: 49%;
}
.two-column.fallback::after {
  content: ""; display: table; clear: both;
}

/* --- wkhtmltopdf-friendly two-column layout --- */
.two-column {
  display: table;
  width: 100%;
  table-layout: fixed;
}
.column {
  display: table-cell;
  width: 50%;
  vertical-align: top;
  padding: 0 10px 0 0;
}
.column.right {
  padding: 0 0 0 10px;
}

/* Make long words wrap instead of forcing column break */
body, .column {
  word-break: break-word;
  overflow-wrap: anywhere;
}

/* Optional: slightly smaller headings to reduce push-down */
h1 { font-size: 20pt; }
h2 { font-size: 14pt; }

/* Debug helper (uncomment when testing) */
/* .column { outline: 1px dashed #ccc; } */
"""



BASE_HTML = """

"""

ENG_REPORT_INSTRUCTIONS = """
You are an expert report writer.
- Input: JSON with title, period, generated_on, summary_seed, rows (region & count), and observations.
- Output: a complete, formal English report text (not HTML) based on the input data provided with sections:
  1) Title
  2) Period & Generated On
  3) Executive Summary(3-4 short paragraphs)
  4) Inventory Overview (mention total and key states)
  5) Observations (bullet points)

- Keep it factual, formal, and avoid unnecessary elaboration
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
  Page 1: Title and meta info (Period, Generated On) at the top,
          followed by a two-column bilingual summary (Malay on the left, English on the right).
  Page 2: Inventory Table titled "Inventory by State" (full width).
  Page 3: Observations in two columns (Malay left, English right).
- For every bilingual section, use EXACTLY this structure:
    <div class="two-column">
      <div class="column left">[Malay content]</div>
      <div class="column right">[English content]</div>
    </div>
- Do NOT use CSS columns or modern flexbox. The CSS provided uses table/table-cell layout.
- Use the provided CSS styles from {BASE_CSS!r} for overall formatting.
- Insert <div class="page-break"></div> between pages.
- Output only valid HTML starting with <!doctype html>.
- Do NOT include any explanations, comments, or code fences like ``` in the output.
"""



eng_agent = create_or_get_agent("eng-report-agent", ENG_REPORT_INSTRUCTIONS)
malay_agent = create_or_get_agent("malay-report-agent", MALAY_REPORT_INSTRUCTIONS)  
html_maker_agent = create_or_get_agent("html-maker-agent", HTML_MAKER_INSTRUCTIONS)

# ---------------------------------------------------------------------------------------------------------






# ---------------------------------------------------------------------------------------------------------
# EXAMPLE TRIGGER (replace with your input mechanism)
# ---------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
  # Example input payload - It should coming from power automate that retrive data from power BI
  input_payload = {
    "title": "Laporan Stok Harta Tanah",
    "period": "Jan–Mar 2025",
    "generated_on": datetime.now().strftime("%Y-%m-%d"),
    "summary_seed": "This seed gives the context for the executive summary.",
    "rows": [
        {"region": "Selangor", "count": 1200},
        {"region": "Johor",    "count": 980},
        {"region": "Perak",    "count": 450}
    ],
    "observations": [
        "Stock levels increased in Selangor due to Q1 campaigns.",
        "Johor distribution centers reported 3% downtime."
    ]
  }

  outputs = orchestrate_report(input_payload)

  with open("preview.html", "w", encoding="utf-8") as f:
    f.write(outputs["html"])

  # Optional: auto-open the preview in your default browser
  import webbrowser
  webbrowser.open("preview.html")

  # Generate the HTMl to be PDF and DOCX
  title = input_payload["title"].replace(" ", "_")
  pdf_path, docx_path = save_outputs(
    outputs["html"],
    pdf_path = f"{title}.pdf",
    docx_path = f"{title}.docx",
  )



































# # 1️⃣ Sample HTML Template (you can replace this with your file)
# html_template = """
# <!doctype html>
# <html>
# <head>
#   <meta charset="utf-8">
#   <title>{{ title }}</title>
#   <style>
#     page { size: A4; margin: 20mm; }
#     body { font-family: Arial, sans-serif; line-height: 1.4; }
#     h1, h2 { color: #0a5; margin: 0 0 8px; }
#     .meta { color: #666; font-size: 12px; margin-bottom: 18px; }
#     table { border-collapse: collapse; width: 100%; }
#     th, td { border: 1px solid #aaa; padding: 6px 8px; font-size: 12px; }
#     .page-break { page-break-before: always; }        /* wkhtmltopdf supports this */
#     /* Modern CSS also supports: .pb { break-before: page; } */
#   </style>
# </head>
# <body>

#   <!-- Page 1 -->
#   <h1>{{ title }}</h1>
#   <div class="meta">Tempoh: {{ period }} &nbsp;•&nbsp; Dihasilkan: {{ generated_on }}</div>
#   <p>{{ summary }}</p>

#   <div class="page-break"></div>

#   <!-- Page 2 -->
#   <h2>Jadual Inventori Mengikut Negeri</h2>
#   <table>
#     <thead>
#       <tr><th>Negeri</th><th>Bilangan</th></tr>
#     </thead>
#     <tbody>
#       {% for r in rows %}
#       <tr><td>{{ r.region }}</td><td style="text-align:right">{{ "{:,}".format(r.count) }}</td></tr>
#       {% endfor %}
#     </tbody>
#   </table>

#   <div class="page-break"></div>

#   <!-- Page 3 -->
#   <h2>Pemerhatian</h2>
#   <p>
#     Tambahkan analisis di sini (AI boleh hasilkan perenggan ini).
#     Gunakan page-break untuk memastikan setiap bahagian berada pada halaman tersendiri.
#   </p>

# </body>
# </html>
# """

# # 2️⃣ Render HTML using Jinja2
# data = {
#     "title": "Sample Report",
#     "heading": "HTML to PDF & DOCX Example",
#     "content": "This file was generated from HTML using Python!"
# }

# template = Template(html_template)
# rendered_html = template.render(data)

# # 4️⃣ Convert HTML → PDF directly from the HTML string
# pdfkit.from_string(rendered_html, "output.pdf")
# print("✅ PDF file generated: output.pdf")

# # 5️⃣ Convert HTML → DOCX directly from the HTML string
# pypandoc.convert_text(rendered_html, "docx", format="html", outputfile="output.docx", extra_args=["--standalone"])
# print("✅ DOCX file generated: output.docx")
