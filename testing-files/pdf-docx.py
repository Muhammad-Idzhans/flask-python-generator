from pdf2docx import parse

# Change this to your PDF filename (the file is in the same folder as this script)
PDF_NAME  = "Laporan Stok Harta Tanah H1 2025.pdf"
DOCX_NAME = "sample_report.docx"

# Convert entire PDF -> DOCX
pages_converted = parse(pdf_file=PDF_NAME, docx_file=DOCX_NAME)
print(f"Converted {pages_converted} pages from '{PDF_NAME}' to '{DOCX_NAME}'")
