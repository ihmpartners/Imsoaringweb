"""Assemble the HTML utility from ui.html + core.js + the sample flight."""
import json, os
here = os.path.dirname(os.path.abspath(__file__))
ui = open(os.path.join(here, "ui.html")).read()
core = open(os.path.join(here, "core.js")).read()
sample = open(os.path.join(here, "tests", "samples", "sample_rich.igc")).read()
html = ui.replace("/*CORE*/", core).replace("/*SAMPLE*/", json.dumps(sample))
open(os.path.join(here, "climb-analyser.html"), "w").write(html)
print("climb-analyser.html", len(html), "bytes")
