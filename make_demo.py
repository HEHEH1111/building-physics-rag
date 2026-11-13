"""Create the original one-page example used by the quick start."""
from pathlib import Path
from reportlab.pdfgen import canvas

folder = Path(__file__).resolve().parent / "sample"
folder.mkdir(exist_ok=True)
pdf = canvas.Canvas(str(folder / "moisture-demo.pdf"), invariant=True)
pdf.setTitle("Moisture measurement example")
pdf.setAuthor("building-physics-rag contributors")
pdf.drawString(55, 760, "Moisture content: a worked example")
pdf.drawString(55, 730, "A dry sample weighs 100 g. After wetting it weighs 105 g.")
pdf.drawString(55, 710, "The added water weighs 5 g. The dry-basis moisture content is 5 percent.")
pdf.drawString(55, 680, "These numbers are synthetic and illustrate the calculation only.")
pdf.save()
