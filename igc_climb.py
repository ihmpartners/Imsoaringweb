#!/usr/bin/env python3
"""
IMSOARING climb analyser
========================

Finds the maximum height gain in an IGC flight log: the greatest climb from a
low point after the launch to a later high point.

How it works
------------
1. Take-off and landing are found from sustained ground speed.
2. The launch is located and excluded:
   - self-launch: the engine run that starts at take-off; the launch ends when
     the engine stops;
   - winch or aerotow: the launch climb ends at release, taken as the top of the
     launch climb once the glider loses height or starts circling.
3. Engine runs are found from the ENL (engine noise) and/or MOP (means of
   propulsion) sensors declared in the IGC I record. A height gain is never
   allowed to span an engine run: the low point and high point must both lie in
   the same engine-free stretch of flight, either between release and the next
   engine start, or after an engine has been stopped.
4. In each engine-free stretch, the climb runs from the stretch's highest
   point back to the lowest point before it. Climbs are only split where an
   engine run lies between them. The flight's height gain is the biggest of
   these climbs.
5. Flights without an engine sensor are checked for long, straight, steady
   climbs that may be undeclared engine runs. These are reported as warnings,
   and can be treated as engine runs with --treat-suspected.

Pressure altitude is used when the logger records it, otherwise GPS altitude.

Usage
-----
    python igc_climb.py FLIGHT.igc
    python igc_climb.py *.igc --json
    python igc_climb.py FLIGHT.igc --release 12:04:30 --alt gps

Needs only the Python 3 standard library.
"""
import argparse
import json
import math
import re
import sys

DEFAULTS = {
    "alt": "auto",            # auto | pressure | gps
    "enl_threshold": 500,     # ENL value (0-999) at or above which the engine counts as running
    "mop_threshold": 500,     # MOP value (0-999) at or above which the engine counts as running
    "min_engine_s": 10,       # shortest engine run that counts
    "merge_gap_s": 30,        # engine runs closer together than this are merged
    "release_drop_m": 30,     # height loss below the launch top that marks release
    "max_launch_s": 1200,     # longest launch before giving up on finding release
    "selflaunch_window_s": 120,  # engine starting this soon after take-off = self-launch
    "suspect_min_s": 90,      # suspected engine run: straight climb lasting at least this long
    "suspect_rate": 1.5,      # ... at this average climb rate or better (m/s)
    "suspect_turn_deg": 120,  # ... with no more than this much total turning
    "treat_suspected": False,
    "release": None,          # manual release time "HH:MM:SS" (UTC), overrides detection
    "min_climb_m": 100,       # smallest climb counted
}

M_TO_FT = 3.28084


# ---------------------------------------------------------------- parsing
def parse_igc(text):
    exts = []
    date = None
    hdr = {"pilot": "", "glider": "", "reg": "", "comp": ""}
    T, LAT, LON, PA, GA, ENL, MOP = [], [], [], [], [], [], []
    last = -1
    day = 0
    for raw in text.splitlines():
        L = raw.rstrip()
        if not L:
            continue
        c = L[0]
        if c == "I" and re.match(r"^I\d{2}", L):
            exts = []
            n = int(L[1:3])
            for k in range(n):
                o = 3 + k * 7
                if len(L) < o + 7:
                    break
                try:
                    s, e = int(L[o:o + 2]), int(L[o + 2:o + 4])
                except ValueError:
                    continue
                exts.append((L[o + 4:o + 7].upper(), s, e))
        elif c == "H":
            u = L.upper()
            val = L.split(":", 1)[1].strip() if ":" in L else ""
            if u.startswith("HFDTE"):
                m = re.search(r"(\d{2})(\d{2})(\d{2})", L[5:])
                if m:
                    dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    date = "%04d-%02d-%02d" % (2000 + yy if yy < 80 else 1900 + yy, mm, dd)
            elif re.match(r"^H[FOP]PLT", u):
                hdr["pilot"] = val
            elif re.match(r"^H[FOP]GTY", u):
                hdr["glider"] = val
            elif re.match(r"^H[FOP]GID", u):
                hdr["reg"] = val
            elif re.match(r"^H[FOP]CID", u):
                hdr["comp"] = val
        elif c == "B" and len(L) >= 35:
            try:
                t = int(L[1:3]) * 3600 + int(L[3:5]) * 60 + int(L[5:7])
                lat = (int(L[7:9]) + int(L[9:14]) / 60000.0) * (-1 if L[14] == "S" else 1)
                lon = (int(L[15:18]) + int(L[18:23]) / 60000.0) * (-1 if L[23] == "W" else 1)
                pa, ga = int(L[25:30]), int(L[30:35])
            except ValueError:
                continue
            if last >= 0 and t + day < last - 43200:
                day += 86400          # flight crossed midnight UTC
            t += day
            if t <= last:
                continue
            if lat == 0 and lon == 0:
                continue
            enl = mop = None
            for code, s, e in exts:
                if code in ("ENL", "MOP") and len(L) >= e:
                    v = L[s - 1:e].strip()
                    if v.isdigit():
                        if code == "ENL":
                            enl = int(v)
                        else:
                            mop = int(v)
            T.append(t); LAT.append(lat); LON.append(lon); PA.append(pa); GA.append(ga)
            ENL.append(enl); MOP.append(mop)
            last = t
    if len(T) < 20:
        raise ValueError("not enough position fixes (B records) in the file")
    codes = {c for c, _, _ in exts}
    return {"date": date, **hdr, "T": T, "LAT": LAT, "LON": LON, "PA": PA, "GA": GA,
            "ENL": ENL, "MOP": MOP,
            "has_enl": "ENL" in codes and any(v is not None for v in ENL),
            "has_mop": "MOP" in codes and any(v is not None for v in MOP)}


# ---------------------------------------------------------------- helpers
def dist(la1, lo1, la2, lo2):
    R = 6371008.8
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def bearing(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def hms(t):
    t = int(round(t)) % 86400
    return "%02d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)


def dur(s):
    s = int(round(s))
    return "%dh %02dm" % (s // 3600, s // 60 % 60) if s >= 3600 else "%dm %02ds" % (s // 60, s % 60)


def runs_where(T, flags, min_s, gap_s):
    """Index ranges [a, b] where flags is true, merged across short gaps, at least min_s long."""
    runs, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append([start, i - 1]); start = None
    if start is not None:
        runs.append([start, len(flags) - 1])
    merged = []
    for r in runs:
        if merged and T[r[0]] - T[merged[-1][1]] <= gap_s:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if T[r[1]] - T[r[0]] >= min_s]


def sustained_index(T, flags, need_s, from_end=False):
    """First (or last) index of a run of flags lasting at least need_s seconds."""
    n = len(flags)
    rng = range(n - 1, -1, -1) if from_end else range(n)
    start = None
    for i in rng:
        if flags[i]:
            if start is None:
                start = i
            if abs(T[i] - T[start]) >= need_s:
                return start
        else:
            start = None
    return None


# ---------------------------------------------------------------- analysis
def analyse(igc, **opts):
    o = dict(DEFAULTS); o.update({k: v for k, v in opts.items() if v is not None})
    T, n = igc["T"], len(igc["T"])
    warnings = []

    # altitude source
    has_p = any(p != 0 for p in igc["PA"]) and max(igc["PA"]) - min(igc["PA"]) > 0
    has_g = any(g != 0 for g in igc["GA"])
    use_p = has_p if o["alt"] == "auto" else (o["alt"] == "pressure" and has_p) or (o["alt"] == "gps" and not has_g)
    if o["alt"] == "pressure" and not has_p:
        warnings.append("No pressure altitude in this file, so GPS altitude was used.")
    A = [float(v) for v in (igc["PA"] if use_p else igc["GA"])]

    # remove single-fix altitude spikes (logger glitches)
    spikes = 0
    for i in range(1, n - 1):
        d1 = (A[i] - A[i - 1]) / max(1, T[i] - T[i - 1])
        d2 = (A[i + 1] - A[i]) / max(1, T[i + 1] - T[i])
        if abs(d1) > 30 and abs(d2) > 30 and d1 * d2 < 0:
            A[i] = (A[i - 1] + A[i + 1]) / 2; spikes += 1
    if spikes:
        warnings.append("%d altitude spike%s removed." % (spikes, "" if spikes == 1 else "s"))

    # ground speed and turn per fix
    LA, LO = igc["LAT"], igc["LON"]
    GS = [0.0] * n
    for i in range(n):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        dt = T[b] - T[a]
        GS[i] = dist(LA[a], LO[a], LA[b], LO[b]) / dt if dt > 0 else 0.0
    TURN = [0.0] * n
    prev_h = None
    for i in range(1, n):
        if dist(LA[i - 1], LO[i - 1], LA[i], LO[i]) < 3:
            continue
        h = bearing(LA[i - 1], LO[i - 1], LA[i], LO[i])
        if prev_h is not None:
            d = (h - prev_h + 540) % 360 - 180
            TURN[i] = max(-90.0, min(90.0, d))
        prev_h = h

    # take-off and landing
    moving = [g > 8 for g in GS]
    tk = sustained_index(T, moving, 10)
    ld = sustained_index(T, moving, 10, from_end=True)
    if tk is None:
        tk = 0
        warnings.append("Take-off not found from ground speed; analysing from the start of the log.")
    if ld is None or ld <= tk:
        ld = n - 1
        warnings.append("Landing not found from ground speed; analysing to the end of the log.")

    # engine runs from sensors
    has_sensor = igc["has_enl"] or igc["has_mop"]
    on = [((e is not None and e >= o["enl_threshold"]) or (m is not None and m >= o["mop_threshold"]))
          for e, m in zip(igc["ENL"], igc["MOP"])]
    engine = []
    for a, b in runs_where(T, on, o["min_engine_s"], o["merge_gap_s"]):
        peak_e = max([v for v in igc["ENL"][a:b + 1] if v is not None] or [0])
        peak_m = max([v for v in igc["MOP"][a:b + 1] if v is not None] or [0])
        engine.append({"a": a, "b": b, "kind": "sensor", "peak_enl": peak_e, "peak_mop": peak_m})

    # launch and release
    launch_end, method = None, ""
    self_launch = next((r for r in engine if r["a"] <= tk or T[r["a"]] - T[tk] <= o["selflaunch_window_s"]), None)
    if o["release"]:
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", o["release"].strip())
        if not m:
            raise ValueError("--release must be HH:MM or HH:MM:SS")
        want = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
        while want < T[0] - 43200:
            want += 86400
        launch_end = next((i for i in range(n) if T[i] >= want), n - 1)
        method = "set manually"
    elif self_launch:
        launch_end, method = self_launch["b"], "engine stopped"
    else:
        runmax, ri = A[tk], tk
        ws, wsum = tk, 0.0          # 30 s window of signed turning
        for i in range(tk, ld + 1):
            if A[i] > runmax:
                runmax, ri = A[i], i
            wsum += TURN[i]
            while T[i] - T[ws] > 30:
                wsum -= TURN[ws]; ws += 1
            launched = runmax - A[tk] >= 40
            if not launched and A[i] < A[tk] - o["release_drop_m"]:
                launch_end, method = tk, "no launch climb (hill or foot launch)"; break
            if launched and A[i] < runmax - o["release_drop_m"]:
                launch_end, method = ri, "height loss after launch climb"; break
            if launched and abs(wsum) >= 300:
                launch_end, method = min(ri, ws), "circling started"; break
            if T[i] - T[tk] > o["max_launch_s"]:
                launch_end, method = ri, "launch top (no clear release found)"
                warnings.append("No clear release found in the first %d minutes. Check the release point, "
                                "or set it with --release." % (o["max_launch_s"] // 60))
                break
        if launch_end is None:
            launch_end, method = ri, "launch top (no clear release found)"
    rel = launch_end

    # launch type (estimate)
    if method == "engine stopped":
        ltype = "self-launch"
    elif method.startswith("no launch climb"):
        ltype = "hill/foot launch"
    else:
        ldur = max(1, T[rel] - T[tk]); lgain = A[rel] - A[tk]
        ltype = "winch" if ldur <= 150 and lgain / ldur >= 4 else "aerotow"

    # suspected engine runs (no sensor): long straight steady climbs after release
    suspected = []
    if rel < ld:
        cum = [0.0] * (n + 1)
        for i in range(n):
            cum[i + 1] = cum[i] + abs(TURN[i])
        flag = [False] * n
        j = rel
        for i in range(rel, ld + 1):
            j = max(j, i)
            while j <= ld and T[j] - T[i] < o["suspect_min_s"]:
                j += 1
            if j > ld:
                break
            rate = (A[j] - A[i]) / (T[j] - T[i])
            turn = cum[j + 1] - cum[i + 1]
            if rate >= o["suspect_rate"] and turn <= o["suspect_turn_deg"]:
                for k in range(i, j + 1):
                    flag[k] = True
        for a, b in runs_where(T, flag, o["suspect_min_s"], o["merge_gap_s"]):
            suspected.append({"a": a, "b": b, "kind": "suspected",
                              "gain_m": round(A[b] - A[a]), "rate": round((A[b] - A[a]) / max(1, T[b] - T[a]), 1)})
    if has_sensor:
        suspected = []          # trust the sensor
    elif suspected:
        warnings.append("No engine sensor (ENL/MOP) in this file, and %d long straight climb%s look%s like a "
                        "possible engine run. %s" % (
                            len(suspected), "" if len(suspected) == 1 else "s", "s" if len(suspected) == 1 else "",
                            "They were treated as engine runs." if o["treat_suspected"]
                            else "They were NOT excluded; use --treat-suspected to exclude them."))

    breaks = list(engine)       # a self-launch run ends at release, so it never splits a section
    if o["treat_suspected"]:
        breaks = breaks + suspected
    breaks.sort(key=lambda r: r["a"])

    # engine-free segments after release
    segs, start = [], rel
    for r in breaks:
        if r["b"] < start:
            continue
        if r["a"] > start:
            segs.append([start, min(r["a"] - 1, ld)])
        start = max(start, r["b"] + 1)
        if start > ld:
            break
    if start <= ld:
        segs.append([start, ld])
    segs = [s for s in segs if s[1] > s[0]]

    # one climb per engine-free section: from the section's highest point back to
    # the lowest point before it. Climbs are only split where an engine run lies
    # between them. If the section starts at its top (e.g. engine stopped high),
    # the search moves on to the part of the section after that top.
    def section_climb(a, b):
        while a < b:
            h = a
            for k in range(a, b + 1):
                if A[k] > A[h]:
                    h = k
            l = a
            for k in range(a, h + 1):
                if A[k] < A[l]:
                    l = k
            if A[h] - A[l] >= o["min_climb_m"]:
                return l, h
            a = h + 1
        return None

    seg_out, climbs = [], []
    for si, (a, b) in enumerate(segs):
        lh = section_climb(a, b)
        bl, bh = lh if lh else (a, a)
        g = A[bh] - A[bl]
        seg_out.append({"segment": si + 1, "start": hms(T[a]), "end": hms(T[b]),
                        "low_time": hms(T[bl]), "low_m": round(A[bl]), "high_time": hms(T[bh]), "high_m": round(A[bh]),
                        "gain_m": round(g), "gain_ft": round(g * M_TO_FT), "_i": (a, b, bl, bh), "_g": g})
        if lh:
            climbs.append({"climb": len(climbs) + 1, "segment": si + 1,
                           "low_time": hms(T[bl]), "low_m": round(A[bl]),
                           "high_time": hms(T[bh]), "high_m": round(A[bh]),
                           "gain_m": round(g), "gain_ft": round(g * M_TO_FT),
                           "duration_s": T[bh] - T[bl],
                           "avg_rate": round(g / max(1, T[bh] - T[bl]), 1),
                           "highest": False, "_i": (bl, bh), "_g": g})
    best_climb, best = None, None
    for c, sg in ((c, seg_out[c["segment"] - 1]) for c in climbs):
        if best_climb is None or c["_g"] > best_climb["_g"]:
            best_climb, best = c, sg
    if best_climb:
        best_climb["highest"] = True

    def rng(r):
        return {"start": hms(T[r["a"]]), "end": hms(T[r["b"]]), "duration_s": T[r["b"]] - T[r["a"]],
                "start_alt_m": round(A[r["a"]]), "end_alt_m": round(A[r["b"]]),
                **{k: v for k, v in r.items() if k not in ("a", "b")}, "_i": (r["a"], r["b"])}

    badge = None
    if best:
        g = best["gain_m"]
        badge = ("Diamond height (5,000 m)" if g >= 5000 else "Gold height (3,000 m)" if g >= 3000
                 else "Silver height (1,000 m)" if g >= 1000 else None)

    return {
        "date": igc["date"], "pilot": igc["pilot"], "glider": igc["glider"], "reg": igc["reg"], "comp": igc["comp"],
        "altitude_source": "pressure" if use_p else "GPS",
        "engine_sensor": "+".join(k for k, f in (("ENL", igc["has_enl"]), ("MOP", igc["has_mop"])) if f) or None,
        "takeoff": {"time": hms(T[tk]), "alt_m": round(A[tk])},
        "landing": {"time": hms(T[ld]), "alt_m": round(A[ld])},
        "launch": {"type": ltype, "release_time": hms(T[rel]), "release_alt_m": round(A[rel]),
                   "height_m": round(A[rel] - A[tk]), "found_by": method},
        "engine_runs": [rng(r) for r in engine],
        "suspected_engine_runs": [rng(r) for r in suspected],
        "segments": seg_out,
        "max_gain": best,
        "badge_gain": badge,
        "climbs": climbs,
        "highest_climb": best_climb,
        "warnings": warnings,
        "_series": {"T": T, "A": A, "tk": tk, "ld": ld, "rel": rel},
    }


def clean(obj):
    """Drop private keys for JSON output."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def report(name, r):
    out = []
    w = out.append
    w("IMSOARING climb analysis: %s" % name)
    who = ", ".join(x for x in (r["pilot"], r["glider"], r["reg"] or r["comp"]) if x)
    w("  %s%s" % (r["date"] or "date unknown", (" · " + who) if who else ""))
    w("  Altitude: %s · Engine sensor: %s" % (r["altitude_source"], r["engine_sensor"] or "none"))
    w("")
    L = r["launch"]
    w("  Take-off  %s UTC  %5d m" % (r["takeoff"]["time"], r["takeoff"]["alt_m"]))
    w("  Release   %s UTC  %5d m  %s, %d m launch (%s)" % (L["release_time"], L["release_alt_m"],
                                                         L["type"], L["height_m"], L["found_by"]))
    for e in r["engine_runs"]:
        w("  Engine    %s–%s  %s, %d m → %d m%s" % (e["start"], e["end"], dur(e["duration_s"]),
                                                   e["start_alt_m"], e["end_alt_m"],
                                                   "  (peak ENL %d)" % e["peak_enl"] if e["peak_enl"] else ""))
    for e in r["suspected_engine_runs"]:
        w("  Suspect?  %s–%s  %s straight climb, +%d m at %.1f m/s" % (e["start"], e["end"], dur(e["duration_s"]),
                                                                     e["gain_m"], e["rate"]))
    w("  Landing   %s UTC  %5d m" % (r["landing"]["time"], r["landing"]["alt_m"]))
    w("")
    if len(r["climbs"]) > 1:
        w("  Climbs (one per engine-free section):")
        for c in r["climbs"]:
            w("   %d. %s → %s  %5d m → %5d m  %+5d m  %4.1f m/s  %s%s" % (
                c["climb"], c["low_time"], c["high_time"], c["low_m"], c["high_m"], c["gain_m"],
                c["avg_rate"], dur(c["duration_s"]), "   ◀ HIGHEST" if c["highest"] else ""))
        w("")
    hc = r["highest_climb"]
    if hc:
        w("  HEIGHT GAIN  %d m  (%d ft)" % (hc["gain_m"], hc["gain_ft"]))
        w("    from %d m at %s to %d m at %s UTC, avg %.1f m/s over %s" % (
            hc["low_m"], hc["low_time"], hc["high_m"], hc["high_time"], hc["avg_rate"], dur(hc["duration_s"])))
        if r["badge_gain"]:
            w("    Enough gain for %s (indicative only)" % r["badge_gain"])
    else:
        w("  No climb found after the launch.")
    for msg in r["warnings"]:
        w("  ! " + msg)
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description="Find the maximum height gain after launch in IGC flight logs, "
                                            "excluding the launch and any engine runs.")
    p.add_argument("files", nargs="+", help="IGC file(s) to analyse")
    p.add_argument("--alt", choices=["auto", "pressure", "gps"], default="auto",
                   help="altitude source (default: pressure if recorded)")
    p.add_argument("--release", help="set the release time manually, HH:MM:SS UTC")
    p.add_argument("--enl-threshold", type=int, default=DEFAULTS["enl_threshold"],
                   help="ENL value counted as engine running (default %(default)s)")
    p.add_argument("--mop-threshold", type=int, default=DEFAULTS["mop_threshold"],
                   help="MOP value counted as engine running (default %(default)s)")
    p.add_argument("--treat-suspected", action="store_true",
                   help="exclude suspected engine runs (long straight climbs) when there is no engine sensor")
    p.add_argument("--min-climb", type=int, default=DEFAULTS["min_climb_m"],
                   help="smallest climb counted, m (default %(default)s)")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    a = p.parse_args(argv)

    results, status = [], 0
    for f in a.files:
        try:
            with open(f, "r", encoding="latin-1") as fh:
                igc = parse_igc(fh.read())
            r = analyse(igc, alt=a.alt, release=a.release, enl_threshold=a.enl_threshold,
                        mop_threshold=a.mop_threshold, treat_suspected=a.treat_suspected,
                        min_climb_m=a.min_climb)
        except (OSError, ValueError) as e:
            print("%s: %s" % (f, e), file=sys.stderr); status = 1; continue
        if a.json:
            results.append({"file": f, **clean(r)})
        else:
            print(report(f, r)); print()
    if a.json:
        print(json.dumps(results if len(results) != 1 else results[0], indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
