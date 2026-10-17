# Gain of Height – IGC climb analyser

Handover of the IMSOARING climb analyser work, 17 Oct 2026.

## What it does
Finds the height gain in an IGC flight log. The gain runs from the lowest point
before the flight's highest point, up to that highest point, after the launch.
Climbs are only split where an engine run lies between the low and high points.

## Rules (as agreed)
1. **Take-off and landing:** found from ground speed sustained above 8 m/s for 10 s.
2. **Launch is excluded:**
   - **Self-launch:** an engine run starting within 120 s of take-off is the launch, and release is when the engine stops.
   - **Winch or aerotow:** release is the top of the launch climb. It's detected when the glider then loses 30 m or starts circling (300° of turn in 30 s).
   - **Hill or foot launch:** if altitude drops 30 m below take-off before any 40 m launch climb, release is take-off.
   - **No release found** within 20 minutes: the analyser warns, and the release can be set manually.
3. **Engine runs:** taken from the ENL and/or MOP sensors in the IGC I record. A reading of 500 or more counts as the engine running. A run must last at least 10 s, and runs less than 30 s apart are merged.
4. **Climbs:** each engine-free stretch after release gets one climb. It runs from the stretch's highest point back to the lowest point before it. If the stretch starts at its top (for example, the engine was stopped high), the search moves past that top. Climbs smaller than 100 m are ignored.
5. **Height gain:** the biggest of those climbs. It's highlighted as the "highest climb".
6. **No engine sensor:** a straight climb lasting at least 90 s, at 1.5 m/s or more, with no more than 120° of total turning, is flagged as a suspected engine run. It's a warning only, unless `--treat-suspected` is set.
7. **Altitude:** pressure altitude if logged, otherwise GPS. Single-fix spikes over 30 m/s in opposite directions are smoothed out.
8. **Badge notes:** Silver (1,000 m), Gold (3,000 m) and Diamond (5,000 m) are shown as indicative only.

Open question: if the absolute lowest point comes after the highest point (for example, a late low save), it does not count against that highest point. Using the absolute lowest point instead was offered but not yet decided.

## Files
| File | What it is |
|---|---|
| `igc_climb.py` | The analyser as a command-line tool, standard library only. `python igc_climb.py FLIGHT.igc [--json] [--release HH:MM:SS] [--alt gps] [--treat-suspected] [--min-climb 100]` |
| `core.js` | A line-for-line JavaScript port of the analysis, giving identical JSON output. |
| `ui.html` | The page template: barograph, climbs table, events and settings. |
| `build.py` | Builds `climb-analyser.html` from `ui.html`, `core.js` and the sample flight. |
| `climb-analyser.html` | The built page, as published at https://claude.ai/artifact/T9GGGYG4qdrYWDsbK2bw5J |
| `dist/climb-analyser.html` | A standalone copy that opens in any browser. |
| `ladder_agent.py` | The BGA ladder checker. Playwright opens the ladder, picks the top-scoring cells, downloads each IGC file and checks it with `igc_climb.py`. It has been tested on a mock ladder only; the real site couldn't be reached from the cloud workspace. |
| `tests/make_samples.py` | Generates synthetic test flights: winch, aerotow, self-launch, an engine restart, no ENL, and the engine stopped at the top. |
| `tests/js_run.js` | A Node runner used to check that Python and JavaScript agree. |
| `tests/samples/`, `tests/real/` | The test flights. The real logs are from public open-source test sets. |

## Checking parity after changes
```
for f in tests/samples/*.igc tests/real/*.igc; do
  python3 igc_climb.py $f --json > py.json; node tests/js_run.js $f > js.json
  python3 -c "import json;assert json.load(open('py.json'))==json.load(open('js.json'))" && echo ok $f
done
```
`tests/real/example.igc` has too few fixes and is expected to fail parsing.

## Results on test flights
| Flight | Height gain |
|---|---|
| Synthetic self-launch, three thermals, engine restart | 1,004 m (645 → 1,649 m); 498 m after the restart |
| Synthetic winch | 1,354 m |
| Real winch flight (apf-bug554) | 1,252 m (364 → 1,616 m) |
| Real paraglider hill launch (napret) | 661 m |

## Next steps
- Run `ladder_agent.py` against https://bgaladder.net/Ladders/NationalLadder/2026/H on a computer, and adjust it to the real page layout. Check whether the IGC downloads need a sign-in.
- Check the ENL threshold of 500 against a real motor-glider log.
- Decide the open question above.
