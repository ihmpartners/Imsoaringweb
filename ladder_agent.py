#!/usr/bin/env python3
"""
IMSOARING ladder climb checker
==============================

Opens a BGA ladder page, finds the highest-scoring cells, opens each one to
reach the flight, downloads its IGC file, and checks the climb with
igc_climb.py (which must sit in the same folder).

    python ladder_agent.py
    python ladder_agent.py https://bgaladder.net/Ladders/NationalLadder/2026/H --top 3
    python ladder_agent.py --distinct-pilots --show      # watch the browser work

Setup (once):
    pip install playwright
    python -m playwright install chromium

Output goes to ./ladder_check/: the IGC files, report.md and report.json.
If the page layout defeats the cell finder, ladder_check/debug/ holds the
rendered HTML, a screenshot and any JSON the page loaded, for diagnosis.
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import igc_climb  # noqa: E402

DEFAULT_URL = "https://bgaladder.net/Ladders/NationalLadder/2026/H"
NUM = re.compile(r"^\s*(\d{1,3}(?:[,\s]\d{3})*|\d+)(?:\.\d+)?\s*$")

# JavaScript run in the page: every table cell whose text is a single number,
# with its row's pilot text, column header and any link inside or around it.
CELLS_JS = r"""
() => {
  const out = [];
  const tables = [...document.querySelectorAll('table')];
  tables.forEach((tbl, ti) => {
    const heads = [...tbl.querySelectorAll('thead th, thead td')].map(h => h.innerText.trim());
    const rows = [...tbl.querySelectorAll('tbody tr, tr')];
    rows.forEach((tr, ri) => {
      const cells = [...tr.children];
      if (!cells.length || cells.every(c => c.tagName === 'TH')) return;
      // pilot: first cell with letters that is not just a number
      const pilotCell = cells.find(c => /[A-Za-z]{2,}/.test(c.innerText || ''));
      const pilot = pilotCell ? pilotCell.innerText.trim().split('\n')[0] : '';
      cells.forEach((td, ci) => {
        const text = (td.innerText || '').trim();
        if (!/^\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?$|^\d+(?:\.\d+)?$/.test(text)) return;
        const a = td.querySelector('a[href]') || td.closest('a[href]');
        const clickable = !!(a || td.onclick || td.querySelector('[onclick],button,[role=button]') ||
                             getComputedStyle(td).cursor === 'pointer' ||
                             (td.firstElementChild && getComputedStyle(td.firstElementChild).cursor === 'pointer'));
        out.push({table: ti, row: ri, col: ci, text, pilot,
                  header: heads[ci] || '', href: a ? a.href : null, clickable,
                  selector: `table:nth-of-type(${ti + 1})`});
      });
    });
  });
  return out;
}
"""


def log(msg):
    print(msg, flush=True)


def to_num(text):
    return float(re.sub(r"[,\s]", "", text))


def pick_cells(cells, top, distinct, exclude_cols):
    """Biggest scoring cells. Totals/rank columns are excluded by header name."""
    ok = []
    for c in cells:
        h = c["header"].lower()
        if any(x in h for x in exclude_cols):
            continue
        if not (c["href"] or c["clickable"]):
            continue          # a flight score should open something
        ok.append(dict(c, value=to_num(c["text"])))
    ok.sort(key=lambda c: -c["value"])
    chosen, seen = [], set()
    for c in ok:
        if distinct and c["pilot"] in seen:
            continue
        chosen.append(c); seen.add(c["pilot"])
        if len(chosen) == top:
            break
    return chosen


def claimed_from_text(text):
    """Pull claimed height figures from a flight page's text."""
    out = {}
    for label, pat in (("height_gain_m", r"(?:height|gain|climb)[^\n\d]{0,30}?(\d{3,5})\s*m\b"),
                       ("height_gain_ft", r"(?:height|gain|climb)[^\n\d]{0,30}?(\d{3,5})\s*(?:ft|feet)\b"),
                       ("points", r"(\d{2,5}(?:\.\d+)?)\s*(?:pts|points)\b")):
        m = re.search(pat, text, re.I)
        if m:
            out[label] = float(m.group(1))
    return out


def find_igc_link(page):
    """IGC download link on the flight page: .igc href, or a link/button mentioning IGC or download."""
    for a in page.query_selector_all("a[href]"):
        href = a.get_attribute("href") or ""
        if re.search(r"\.igc(\?|$)", href, re.I):
            return a
    for el in page.query_selector_all("a, button"):
        t = (el.inner_text() or "").strip().lower()
        href = (el.get_attribute("href") or "").lower()
        if "igc" in t or "igc" in href or ("download" in t and "flight" in t) or "trace" in t or "log file" in t:
            return el
    return None


def save_debug(page, outdir, tag, responses):
    d = os.path.join(outdir, "debug"); os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=os.path.join(d, f"{tag}.png"), full_page=True)
    except Exception as e:  # noqa: BLE001
        log(f"  (debug save failed: {e})")
    for i, (url, body) in enumerate(responses[-20:]):
        with open(os.path.join(d, f"{tag}_json_{i}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"url": url, "body": body}, indent=1)[:2_000_000])


def open_flight(page, ctx, cell, ladder_url):
    """Open the flight behind a ladder cell. Returns the page now showing it."""
    if cell["href"]:
        p = ctx.new_page(); p.goto(cell["href"], wait_until="networkidle"); return p
    # no href: click the cell (may navigate, open a popup or a dialog)
    page.goto(ladder_url, wait_until="networkidle")
    loc = page.locator("table").nth(cell["table"]).locator("tr").nth(cell["row"]).locator(":scope > *").nth(cell["col"])
    try:
        with ctx.expect_page(timeout=4000) as pop:
            loc.click()
        p = pop.value; p.wait_for_load_state("networkidle"); return p
    except Exception:  # noqa: BLE001  - same-tab navigation or in-page dialog
        page.wait_for_load_state("networkidle"); time.sleep(1.0)
        return page


def download_igc(fpage, el, outdir, name):
    path = os.path.join(outdir, name)
    href = el.get_attribute("href")
    if href and not href.lower().startswith("javascript"):
        url = urljoin(fpage.url, href)
        r = fpage.context.request.get(url)
        if r.ok and b"\nB" in r.body():
            with open(path, "wb") as f:
                f.write(r.body())
            return path, url
    with fpage.expect_download(timeout=20000) as dl:
        el.click()
    dl.value.save_as(path)
    return path, dl.value.url


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find the top climbs on a BGA ladder page and verify them from their IGC files.")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--distinct-pilots", action="store_true", help="at most one flight per pilot")
    ap.add_argument("--exclude-cols", default="total,rank,pos,best,sum",
                    help="comma list: column headers containing these words are not flight scores")
    ap.add_argument("--tolerance", type=float, default=0.03, help="allowed difference between claim and IGC (fraction)")
    ap.add_argument("--out", default="ladder_check")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    a = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is needed:  pip install playwright  &&  python -m playwright install chromium")

    os.makedirs(a.out, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not a.show, executable_path=os.environ.get("LADDER_CHROME") or None)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        responses = []

        def grab(resp):
            if "json" in (resp.headers.get("content-type") or ""):
                try:
                    responses.append((resp.url, resp.json()))
                except Exception:  # noqa: BLE001
                    pass
        page.on("response", grab)

        log(f"Opening {a.url}")
        page.goto(a.url, wait_until="networkidle")
        try:
            page.wait_for_selector("table td", timeout=20000)
        except Exception:  # noqa: BLE001
            save_debug(page, a.out, "ladder", responses)
            sys.exit(f"No table appeared on the ladder page. Saved what loaded to {a.out}/debug/.")
        time.sleep(1.0)

        cells = page.evaluate(CELLS_JS)
        chosen = pick_cells(cells, a.top, a.distinct_pilots, [x.strip() for x in a.exclude_cols.split(",") if x.strip()])
        if not chosen:
            save_debug(page, a.out, "ladder", responses)
            sys.exit(f"Found {len(cells)} numeric cells but none that open a flight. Saved page to {a.out}/debug/.")
        log(f"Top {len(chosen)} scoring cells:")
        for i, c in enumerate(chosen, 1):
            log(f"  {i}. {c['value']:g}  {c['pilot']}  [{c['header'] or 'col ' + str(c['col'])}]")

        for i, c in enumerate(chosen, 1):
            rec = {"rank": i, "pilot": c["pilot"], "column": c["header"], "ladder_score": c["value"],
                   "flight_page": None, "igc_url": None, "igc_file": None, "claimed": {}, "analysis": None,
                   "verdict": "", "notes": []}
            log(f"\n[{i}] {c['pilot']} · {c['value']:g}")
            try:
                fpage = open_flight(page, ctx, c, a.url)
                rec["flight_page"] = fpage.url
                text = fpage.inner_text("body")
                rec["claimed"] = claimed_from_text(text)
                el = find_igc_link(fpage)
                if not el:
                    save_debug(fpage, a.out, f"flight{i}", responses)
                    rec["verdict"] = "NO IGC LINK FOUND"
                    rec["notes"].append("Could not find an IGC download on the flight page (it may need a sign-in).")
                    results.append(rec); continue
                safe = re.sub(r"[^A-Za-z0-9]+", "_", c["pilot"]).strip("_") or "pilot"
                path, url = download_igc(fpage, el, a.out, f"{i}_{safe}.igc")
                rec["igc_file"], rec["igc_url"] = path, url
                log(f"    IGC saved: {path}")
                with open(path, encoding="latin-1") as f:
                    r = igc_climb.analyse(igc_climb.parse_igc(f.read()))
                rec["analysis"] = igc_climb.clean(r)
                g = r["max_gain"]["gain_m"] if r["max_gain"] else 0
                claim = rec["claimed"].get("height_gain_m") or (
                    rec["claimed"]["height_gain_ft"] / igc_climb.M_TO_FT if "height_gain_ft" in rec["claimed"] else None)
                if claim:
                    diff = (g - claim) / claim
                    rec["difference_pct"] = round(diff * 100, 1)
                    rec["verdict"] = ("CONFIRMED" if abs(diff) <= a.tolerance else
                                      "IGC SHOWS MORE" if diff > 0 else "IGC SHOWS LESS")
                else:
                    rec["verdict"] = "MEASURED (no claimed height found on the flight page to compare)"
                if r["engine_runs"]:
                    rec["notes"].append(f"{len(r['engine_runs'])} engine run(s) excluded.")
                if r["suspected_engine_runs"]:
                    rec["notes"].append("Possible undeclared engine run (no ENL/MOP sensor).")
                rec["notes"] += r["warnings"]
                log(f"    IGC max climb {g} m · claim {claim and round(claim)} · {rec['verdict']}")
                if fpage is not page:
                    fpage.close()
            except Exception as e:  # noqa: BLE001
                rec["verdict"] = "ERROR"; rec["notes"].append(str(e))
                log(f"    error: {e}")
            results.append(rec)
        browser.close()

    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump({"ladder": a.url, "checked": time.strftime("%Y-%m-%d %H:%M"), "flights": results}, f, indent=2)
    md = [f"# Ladder climb check", "", f"Ladder: {a.url}  ", f"Checked: {time.strftime('%Y-%m-%d %H:%M')}", "",
          "| # | Pilot | Ladder score | Claimed gain | IGC max climb | Verdict |", "|---|---|---|---|---|---|"]
    for r in results:
        an = r["analysis"] or {}
        g = an.get("max_gain") or {}
        cl = (f"{r['claimed']['height_gain_m']:g} m" if r["claimed"].get("height_gain_m")
              else f"{r['claimed']['height_gain_ft']:g} ft" if r["claimed"].get("height_gain_ft") else None)
        md.append(f"| {r['rank']} | {r['pilot']} | {r['ladder_score']:g} | {cl or '–'} | "
                  f"{(str(g.get('gain_m')) + ' m') if g else '–'} | {r['verdict']} |")
    md.append("")
    for r in results:
        md.append(f"## {r['rank']}. {r['pilot']}")
        md.append(f"- Flight page: {r['flight_page'] or '–'}")
        md.append(f"- IGC: {r['igc_url'] or '–'}")
        an = r["analysis"]
        if an and an.get("max_gain"):
            g = an["max_gain"]
            md.append(f"- Max climb {g['gain_m']} m ({g['gain_ft']} ft): {g['low_m']} m at {g['low_time']} → "
                      f"{g['high_m']} m at {g['high_time']} UTC")
            md.append(f"- Launch: {an['launch']['type']}, release {an['launch']['release_time']} "
                      f"({an['launch']['found_by']}); altitude {an['altitude_source']}; engine sensor {an['engine_sensor'] or 'none'}")
        for n in r["notes"]:
            md.append(f"- Note: {n}")
        md.append("")
    with open(os.path.join(a.out, "report.md"), "w") as f:
        f.write("\n".join(md))
    log(f"\nReport: {os.path.join(a.out, 'report.md')}")


if __name__ == "__main__":
    main()
