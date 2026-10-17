/* IMSOARING climb analyser: analysis core. Line-for-line port of igc_climb.py. */
const IGCClimb = (() => {
'use strict';
const DEFAULTS = {alt:'auto', enl_threshold:500, mop_threshold:500, min_engine_s:10, merge_gap_s:30,
  release_drop_m:30, max_launch_s:1200, selflaunch_window_s:120, suspect_min_s:90, suspect_rate:1.5,
  suspect_turn_deg:120, treat_suspected:false, release:null, min_climb_m:100};
const M_TO_FT = 3.28084;
const pyRound = x => { const r = Math.round(x); return (Math.abs(x % 1) === 0.5 && r % 2 !== 0) ? r - 1 : r; };

function parseIGC(text){
  let exts = [], date = null;
  const hdr = {pilot:'', glider:'', reg:'', comp:''};
  const T=[], LAT=[], LON=[], PA=[], GA=[], ENL=[], MOP=[];
  let last = -1, day = 0;
  for (const raw of text.split(/\r\n|\r|\n/)){
    const L = raw.replace(/\s+$/, '');
    if (!L) continue;
    const c = L[0];
    if (c === 'I' && /^I\d{2}/.test(L)){
      exts = [];
      const n = parseInt(L.slice(1,3), 10);
      for (let k=0; k<n; k++){
        const o = 3 + k*7;
        if (L.length < o+7) break;
        const s = +L.slice(o,o+2), e = +L.slice(o+2,o+4);
        if (!Number.isInteger(s) || !Number.isInteger(e)) continue;
        exts.push([L.slice(o+4,o+7).toUpperCase(), s, e]);
      }
    } else if (c === 'H'){
      const u = L.toUpperCase();
      const val = L.includes(':') ? L.slice(L.indexOf(':')+1).trim() : '';
      if (u.startsWith('HFDTE')){
        const m = L.slice(5).match(/(\d{2})(\d{2})(\d{2})/);
        if (m){ const yy = +m[3]; date = `${yy<80?2000+yy:1900+yy}-${m[2]}-${m[1]}`; }
      } else if (/^H[FOP]PLT/.test(u)) hdr.pilot = val;
      else if (/^H[FOP]GTY/.test(u)) hdr.glider = val;
      else if (/^H[FOP]GID/.test(u)) hdr.reg = val;
      else if (/^H[FOP]CID/.test(u)) hdr.comp = val;
    } else if (c === 'B' && L.length >= 35){
      const num = (a,b) => { const s = L.slice(a,b); return /^\s*-?\d+\s*$/.test(s) ? parseInt(s,10) : NaN; };
      let t = num(1,3)*3600 + num(3,5)*60 + num(5,7);
      const lat = (num(7,9) + num(9,14)/60000) * (L[14]==='S' ? -1 : 1);
      const lon = (num(15,18) + num(18,23)/60000) * (L[23]==='W' ? -1 : 1);
      const pa = num(25,30), ga = num(30,35);
      if ([t,lat,lon,pa,ga].some(Number.isNaN)) continue;
      if (last >= 0 && t + day < last - 43200) day += 86400;
      t += day;
      if (t <= last) continue;
      if (lat === 0 && lon === 0) continue;
      let enl = null, mop = null;
      for (const [code,s,e] of exts){
        if ((code === 'ENL' || code === 'MOP') && L.length >= e){
          const v = L.slice(s-1, e).trim();
          if (/^\d+$/.test(v)){ if (code === 'ENL') enl = +v; else mop = +v; }
        }
      }
      T.push(t); LAT.push(lat); LON.push(lon); PA.push(pa); GA.push(ga); ENL.push(enl); MOP.push(mop);
      last = t;
    }
  }
  if (T.length < 20) throw new Error('not enough position fixes (B records) in the file');
  const codes = new Set(exts.map(x => x[0]));
  return {date, ...hdr, T, LAT, LON, PA, GA, ENL, MOP,
    has_enl: codes.has('ENL') && ENL.some(v => v !== null),
    has_mop: codes.has('MOP') && MOP.some(v => v !== null)};
}

const RAD = Math.PI/180;
function dist(la1, lo1, la2, lo2){
  const R = 6371008.8, p1 = la1*RAD, p2 = la2*RAD, dp = p2-p1, dl = (lo2-lo1)*RAD;
  const a = Math.sin(dp/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
  return 2*R*Math.asin(Math.min(1, Math.sqrt(a)));
}
function bearing(la1, lo1, la2, lo2){
  const p1 = la1*RAD, p2 = la2*RAD, dl = (lo2-lo1)*RAD;
  const y = Math.sin(dl)*Math.cos(p2), x = Math.cos(p1)*Math.sin(p2) - Math.sin(p1)*Math.cos(p2)*Math.cos(dl);
  return ((Math.atan2(y,x)/RAD) + 360) % 360;
}
const pmod = (a, m) => ((a % m) + m) % m;
const pad = n => String(n).padStart(2,'0');
function hms(t){ t = pmod(pyRound(t), 86400); return `${pad(Math.floor(t/3600))}:${pad(Math.floor(t/60)%60)}:${pad(t%60)}`; }
function dur(s){ s = pyRound(s); return s >= 3600 ? `${Math.floor(s/3600)}h ${pad(Math.floor(s/60)%60)}m` : `${Math.floor(s/60)}m ${pad(s%60)}s`; }

function runsWhere(T, flags, min_s, gap_s){
  const runs = []; let start = null;
  flags.forEach((f,i) => {
    if (f && start === null) start = i;
    else if (!f && start !== null){ runs.push([start, i-1]); start = null; }
  });
  if (start !== null) runs.push([start, flags.length-1]);
  const merged = [];
  for (const r of runs){
    if (merged.length && T[r[0]] - T[merged[merged.length-1][1]] <= gap_s) merged[merged.length-1][1] = r[1];
    else merged.push(r);
  }
  return merged.filter(r => T[r[1]] - T[r[0]] >= min_s);
}
function sustainedIndex(T, flags, need_s, fromEnd=false){
  const n = flags.length; let start = null;
  for (let k=0; k<n; k++){
    const i = fromEnd ? n-1-k : k;
    if (flags[i]){
      if (start === null) start = i;
      if (Math.abs(T[i] - T[start]) >= need_s) return start;
    } else start = null;
  }
  return null;
}

function analyse(igc, opts={}){
  const o = Object.assign({}, DEFAULTS);
  for (const [k,v] of Object.entries(opts)) if (v !== null && v !== undefined) o[k] = v;
  const T = igc.T, n = T.length, warnings = [];

  const maxP = Math.max(...igc.PA), minP = Math.min(...igc.PA);
  const has_p = igc.PA.some(p => p !== 0) && maxP - minP > 0;
  const has_g = igc.GA.some(g => g !== 0);
  const use_p = o.alt === 'auto' ? has_p : (o.alt === 'pressure' && has_p) || (o.alt === 'gps' && !has_g);
  if (o.alt === 'pressure' && !has_p) warnings.push('No pressure altitude in this file, so GPS altitude was used.');
  const A = (use_p ? igc.PA : igc.GA).map(Number);

  let spikes = 0;
  for (let i=1; i<n-1; i++){
    const d1 = (A[i]-A[i-1]) / Math.max(1, T[i]-T[i-1]);
    const d2 = (A[i+1]-A[i]) / Math.max(1, T[i+1]-T[i]);
    if (Math.abs(d1) > 30 && Math.abs(d2) > 30 && d1*d2 < 0){ A[i] = (A[i-1]+A[i+1])/2; spikes++; }
  }
  if (spikes) warnings.push(`${spikes} altitude spike${spikes===1?'':'s'} removed.`);

  const LA = igc.LAT, LO = igc.LON;
  const GS = new Array(n).fill(0);
  for (let i=0; i<n; i++){
    const a = Math.max(0,i-1), b = Math.min(n-1,i+1), dt = T[b]-T[a];
    GS[i] = dt > 0 ? dist(LA[a],LO[a],LA[b],LO[b]) / dt : 0;
  }
  const TURN = new Array(n).fill(0);
  let prevH = null;
  for (let i=1; i<n; i++){
    if (dist(LA[i-1],LO[i-1],LA[i],LO[i]) < 3) continue;
    const h = bearing(LA[i-1],LO[i-1],LA[i],LO[i]);
    if (prevH !== null){ const d = pmod(h - prevH + 540, 360) - 180; TURN[i] = Math.max(-90, Math.min(90, d)); }
    prevH = h;
  }

  const moving = GS.map(g => g > 8);
  let tk = sustainedIndex(T, moving, 10), ld = sustainedIndex(T, moving, 10, true);
  if (tk === null){ tk = 0; warnings.push('Take-off not found from ground speed; analysing from the start of the log.'); }
  if (ld === null || ld <= tk){ ld = n-1; warnings.push('Landing not found from ground speed; analysing to the end of the log.'); }

  const has_sensor = igc.has_enl || igc.has_mop;
  const on = igc.ENL.map((e,i) => (e !== null && e >= o.enl_threshold) || (igc.MOP[i] !== null && igc.MOP[i] >= o.mop_threshold));
  const engine = runsWhere(T, on, o.min_engine_s, o.merge_gap_s).map(([a,b]) => {
    const pe = igc.ENL.slice(a,b+1).filter(v => v !== null), pm = igc.MOP.slice(a,b+1).filter(v => v !== null);
    return {a, b, kind:'sensor', peak_enl: pe.length ? Math.max(...pe) : 0, peak_mop: pm.length ? Math.max(...pm) : 0};
  });

  let launch_end = null, method = '';
  const selfLaunch = engine.find(r => r.a <= tk || T[r.a] - T[tk] <= o.selflaunch_window_s) || null;
  if (o.release){
    const m = String(o.release).trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) throw new Error('Release time must be HH:MM or HH:MM:SS');
    let want = +m[1]*3600 + +m[2]*60 + +(m[3]||0);
    while (want < T[0] - 43200) want += 86400;
    launch_end = T.findIndex(t => t >= want); if (launch_end < 0) launch_end = n-1;
    method = 'set manually';
  } else if (selfLaunch){
    launch_end = selfLaunch.b; method = 'engine stopped';
  } else {
    let runmax = A[tk], ri = tk, ws = tk, wsum = 0;
    for (let i=tk; i<=ld; i++){
      if (A[i] > runmax){ runmax = A[i]; ri = i; }
      wsum += TURN[i];
      while (T[i] - T[ws] > 30){ wsum -= TURN[ws]; ws++; }
      const launched = runmax - A[tk] >= 40;
      if (!launched && A[i] < A[tk] - o.release_drop_m){ launch_end = tk; method = 'no launch climb (hill or foot launch)'; break; }
      if (launched && A[i] < runmax - o.release_drop_m){ launch_end = ri; method = 'height loss after launch climb'; break; }
      if (launched && Math.abs(wsum) >= 300){ launch_end = Math.min(ri, ws); method = 'circling started'; break; }
      if (T[i] - T[tk] > o.max_launch_s){
        launch_end = ri; method = 'launch top (no clear release found)';
        warnings.push(`No clear release found in the first ${Math.floor(o.max_launch_s/60)} minutes. Check the release point, or set it manually.`);
        break;
      }
    }
    if (launch_end === null){ launch_end = ri; method = 'launch top (no clear release found)'; }
  }
  const rel = launch_end;

  let ltype;
  if (method === 'engine stopped') ltype = 'self-launch';
  else if (method.startsWith('no launch climb')) ltype = 'hill/foot launch';
  else { const ldur = Math.max(1, T[rel]-T[tk]), lgain = A[rel]-A[tk]; ltype = ldur <= 150 && lgain/ldur >= 4 ? 'winch' : 'aerotow'; }

  let suspected = [];
  if (rel < ld){
    const cum = new Array(n+1).fill(0);
    for (let i=0; i<n; i++) cum[i+1] = cum[i] + Math.abs(TURN[i]);
    const flag = new Array(n).fill(false);
    let j = rel;
    for (let i=rel; i<=ld; i++){
      j = Math.max(j, i);
      while (j <= ld && T[j] - T[i] < o.suspect_min_s) j++;
      if (j > ld) break;
      const rate = (A[j]-A[i]) / (T[j]-T[i]), turn = cum[j+1] - cum[i+1];
      if (rate >= o.suspect_rate && turn <= o.suspect_turn_deg) for (let k=i; k<=j; k++) flag[k] = true;
    }
    suspected = runsWhere(T, flag, o.suspect_min_s, o.merge_gap_s).map(([a,b]) =>
      ({a, b, kind:'suspected', gain_m: pyRound(A[b]-A[a]), rate: pyRound((A[b]-A[a]) / Math.max(1, T[b]-T[a]) * 10)/10}));
  }
  if (has_sensor) suspected = [];
  else if (suspected.length){
    const k = suspected.length;
    warnings.push(`No engine sensor (ENL/MOP) in this file, and ${k} long straight climb${k===1?'':'s'} look${k===1?'s':''} like a possible engine run. ` +
      (o.treat_suspected ? 'They were treated as engine runs.' : 'They were NOT excluded; use --treat-suspected to exclude them.'));
  }

  let breaks = engine.slice();
  if (o.treat_suspected) breaks = breaks.concat(suspected);
  breaks.sort((x,y) => x.a - y.a);

  let segs = [], start = rel;
  for (const r of breaks){
    if (r.b < start) continue;
    if (r.a > start) segs.push([start, Math.min(r.a-1, ld)]);
    start = Math.max(start, r.b+1);
    if (start > ld) break;
  }
  if (start <= ld) segs.push([start, ld]);
  segs = segs.filter(s => s[1] > s[0]);

  // one climb per engine-free section: highest point back to the lowest point before it
  const sectionClimb = (a, b) => {
    while (a < b){
      let h = a; for (let k=a; k<=b; k++) if (A[k] > A[h]) h = k;
      let l = a; for (let k=a; k<=h; k++) if (A[k] < A[l]) l = k;
      if (A[h] - A[l] >= o.min_climb_m) return [l, h];
      a = h + 1;
    }
    return null;
  };
  const segOut = [], climbs = [];
  segs.forEach(([a,b], si) => {
    const lh = sectionClimb(a, b);
    const [bl, bh] = lh || [a, a];
    const g = A[bh] - A[bl];
    segOut.push({segment: si+1, start: hms(T[a]), end: hms(T[b]), low_time: hms(T[bl]), low_m: pyRound(A[bl]),
      high_time: hms(T[bh]), high_m: pyRound(A[bh]), gain_m: pyRound(g), gain_ft: pyRound(g*M_TO_FT), _i:[a,b,bl,bh], _g: g});
    if (lh) climbs.push({climb: climbs.length+1, segment: si+1, low_time: hms(T[bl]), low_m: pyRound(A[bl]),
      high_time: hms(T[bh]), high_m: pyRound(A[bh]), gain_m: pyRound(g), gain_ft: pyRound(g*M_TO_FT), duration_s: T[bh]-T[bl],
      avg_rate: pyRound(g / Math.max(1, T[bh]-T[bl]) * 10)/10, highest: false, _i:[bl,bh], _g: g});
  });
  let bestClimb = null, best = null;
  for (const c of climbs) if (bestClimb === null || c._g > bestClimb._g){ bestClimb = c; best = segOut[c.segment-1]; }
  if (bestClimb) bestClimb.highest = true;

  const rng = r => { const {a, b, ...rest} = r;
    return {start: hms(T[a]), end: hms(T[b]), duration_s: T[b]-T[a], start_alt_m: pyRound(A[a]), end_alt_m: pyRound(A[b]), ...rest, _i:[a,b]}; };

  let badge = null;
  if (best){ const g = best.gain_m;
    badge = g >= 5000 ? 'Diamond height (5,000 m)' : g >= 3000 ? 'Gold height (3,000 m)' : g >= 1000 ? 'Silver height (1,000 m)' : null; }

  return {
    date: igc.date, pilot: igc.pilot, glider: igc.glider, reg: igc.reg, comp: igc.comp,
    altitude_source: use_p ? 'pressure' : 'GPS',
    engine_sensor: [['ENL', igc.has_enl], ['MOP', igc.has_mop]].filter(x => x[1]).map(x => x[0]).join('+') || null,
    takeoff: {time: hms(T[tk]), alt_m: pyRound(A[tk])},
    landing: {time: hms(T[ld]), alt_m: pyRound(A[ld])},
    launch: {type: ltype, release_time: hms(T[rel]), release_alt_m: pyRound(A[rel]), height_m: pyRound(A[rel]-A[tk]), found_by: method},
    engine_runs: engine.map(rng),
    suspected_engine_runs: suspected.map(rng),
    segments: segOut,
    max_gain: best,
    badge_gain: badge,
    climbs,
    highest_climb: bestClimb,
    warnings,
    _series: {T, A, tk, ld, rel},
  };
}

function clean(obj){
  if (Array.isArray(obj)) return obj.map(clean);
  if (obj && typeof obj === 'object'){ const out = {}; for (const [k,v] of Object.entries(obj)) if (!k.startsWith('_')) out[k] = clean(v); return out; }
  return obj;
}

return {parseIGC, analyse, clean, hms, dur, M_TO_FT, DEFAULTS};
})();
if (typeof module !== 'undefined') module.exports = IGCClimb;
