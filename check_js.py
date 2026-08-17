"""Estrae il JS dai template e ne verifica la sintassi con node."""
import re, subprocess, sys, pathlib, os

ok = True
for f in sorted(pathlib.Path("templates").glob("*.html")):
    html = f.read_text(encoding="utf-8")
    for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                      html, re.S)):
        js = m.group(1)
        # i template Jinja non sono JS validi: neutralizza {{ }} e {% %}
        js = re.sub(r"\{\{.*?\}\}", "0", js)
        js = re.sub(r"\{%.*?%\}", "", js)
        p = f"/tmp/_chk_{f.stem}_{i}.js"
        open(p, "w", encoding="utf-8").write(js)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        if r.returncode:
            ok = False
            print(f"ERRORE in {f.name}:")
            print("  " + r.stderr.strip().split("\n")[-3])
        else:
            print(f"  ok  {f.name} (blocco {i+1}, {len(js.splitlines())} righe)")
        os.remove(p)
sys.exit(0 if ok else 1)
