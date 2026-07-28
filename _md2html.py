import sys, markdown

src, out = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f:
    body = markdown.markdown(
        f.read(),
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )

CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 11pt;
       color: #1a1a1a; line-height: 1.45; }
h1 { font-size: 22pt; color: #0b3d91; border-bottom: 3px solid #0b3d91;
     padding-bottom: 6px; margin: 0 0 10px; }
h2 { font-size: 15pt; color: #0b3d91; margin-top: 22px;
     border-bottom: 1px solid #ccc; padding-bottom: 3px; }
h3 { font-size: 12.5pt; color: #333; margin-top: 16px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt; }
th, td { border: 1px solid #bbb; padding: 5px 8px; text-align: left;
         vertical-align: top; }
th { background: #0b3d91; color: #fff; }
tr:nth-child(even) td { background: #f2f5fb; }
code { background: #eef1f5; padding: 1px 4px; border-radius: 3px;
       font-family: Consolas, monospace; font-size: 9.5pt; }
pre { background: #f5f7fa; border: 1px solid #ddd; border-radius: 4px;
      padding: 10px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #5794f2; background: #f4f8ff;
             margin: 10px 0; padding: 6px 12px; color: #333; }
a { color: #0b3d91; text-decoration: none; }
ul { margin: 6px 0; }
hr { border: none; border-top: 1px solid #ddd; margin: 18px 0; }
"""

html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{CSS}</style></head><body>{body}</body></html>"""

with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("HTML written:", out)
