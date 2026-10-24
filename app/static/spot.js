// Shared browser helpers for every Spot page. Loaded once from base.html and
// exposed as window.Spot so page scripts stay short.
(function () {
  'use strict';

  const pad = n => String(n).padStart(2, '0');

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Number formatting: honour a monitor's decimal-places setting when known,
  // otherwise show up to 4 significant decimals without trailing zeros.
  function fmtValue(v, decimals) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    if (typeof v !== 'number') return String(v);
    if (decimals !== null && decimals !== undefined && decimals !== '') {
      return v.toFixed(Number(decimals));
    }
    const abs = Math.abs(v);
    const digits = abs >= 1000 ? 1 : abs >= 1 ? 2 : 4;
    return Number(v.toFixed(digits)).toLocaleString('en-US', { maximumFractionDigits: digits });
  }

  // "12s ago", "4m ago", "3h ago", "2d ago".
  function timeAgo(ts, now) {
    if (!ts) return 'never';
    const sec = Math.max(0, Math.round(((now || Date.now()) - new Date(ts).valueOf()) / 1000));
    if (sec < 5) return 'just now';
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
  }

  // "15 min", "24 h", "7 days" for a window length in seconds.
  function fmtDuration(sec) {
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    if (sec < 2 * 86400) return `${Math.round(sec / 3600)} h`;
    return `${Math.round(sec / 86400)} days`;
  }

  // ---- Time: everything is shown in Spot's display zone (SPOT_TZ, default
  // America/New_York), 24-hour, regardless of the viewer's computer. Data
  // travels as UTC ISO-8601; only display and form inputs use wall time.
  const TZ = window.SPOT_TZ || 'America/New_York';
  const wallFmt = new Intl.DateTimeFormat('en-US', {
    timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  });

  // Wall-clock fields of an instant in SPOT_TZ: {year, month, day, hour, minute, second, zone}.
  function wall(d) {
    const o = {};
    for (const p of wallFmt.formatToParts(new Date(d))) o[p.type] = p.value;
    return { year: +o.year, month: +o.month, day: +o.day, hour: +o.hour % 24,
             minute: +o.minute, second: +o.second, zone: o.timeZoneName };
  }

  // Offset of SPOT_TZ from UTC at instant `ms`, in ms (EDT = -4h).
  function zoneOffset(ms) {
    const w = wall(ms);
    return Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second) - Math.floor(ms / 1000) * 1000;
  }

  // The instant at which SPOT_TZ's clock reads the given wall time.
  // Month is 1-based; out-of-range days roll over (day 0 = last of prev month).
  function fromWall(year, month, day, hour = 0, minute = 0, second = 0) {
    const guess = Date.UTC(year, month - 1, day, hour, minute, second);
    let t = guess - zoneOffset(guess);
    const again = guess - zoneOffset(t);  // correct across a DST change
    if (again !== t) t = again;
    return new Date(t);
  }

  // "2026-10-24 17:39:24" (+ " EDT" with {zone: true}; no seconds with {seconds: false}).
  function fmtDateTime(ts, opts) {
    if (!ts) return '—';
    const o = Object.assign({ seconds: true, zone: false }, opts);
    const w = wall(ts);
    let out = `${w.year}-${pad(w.month)}-${pad(w.day)} ${pad(w.hour)}:${pad(w.minute)}`;
    if (o.seconds) out += `:${pad(w.second)}`;
    return o.zone ? `${out} ${w.zone}` : out;
  }
  // "17:39:24"
  function fmtTime(ts) {
    if (!ts) return '—';
    const w = wall(ts);
    return `${pad(w.hour)}:${pad(w.minute)}:${pad(w.second)}`;
  }
  // "EDT" / "EST" right now (for "times are Eastern" hints).
  function zoneName() { return wall(Date.now()).zone; }

  // datetime-local inputs hold SPOT_TZ wall time.
  function toLocalInput(d) {
    const w = wall(d);
    return `${w.year}-${pad(w.month)}-${pad(w.day)}T${pad(w.hour)}:${pad(w.minute)}:${pad(w.second)}`;
  }
  function toIso(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(s || '');
    return m ? fromWall(+m[1], +m[2], +m[3], +m[4], +m[5], +(m[6] || 0)).toISOString() : '';
  }

  // Named quick ranges shared by the Query and Reports pages. "Today",
  // "yesterday" and "this week" use SPOT_TZ midnights.
  function presetRange(name) {
    const now = new Date();
    const w = wall(now);
    const hours = h => [new Date(now - h * 3600e3), now];
    switch (name) {
      case '1h': return hours(1);
      case '6h': return hours(6);
      case '24h': return hours(24);
      case '7d': return hours(24 * 7);
      case '30d': return hours(24 * 30);
      case 'today': return [fromWall(w.year, w.month, w.day), now];
      case 'yesterday': return [fromWall(w.year, w.month, w.day - 1), fromWall(w.year, w.month, w.day)];
      case 'week': {
        const dow = new Date(Date.UTC(w.year, w.month - 1, w.day)).getUTCDay();  // 0 = Sunday
        return [fromWall(w.year, w.month, w.day - ((dow + 6) % 7)), now];
      }
      default: return null;
    }
  }

  // Wire a row of <button data-preset="..."> chips to a form's start/end inputs.
  function bindPresets(container, form, onPick) {
    if (!container) return;
    container.addEventListener('click', e => {
      const btn = e.target.closest('[data-preset]');
      if (!btn) return;
      const r = presetRange(btn.dataset.preset);
      if (!r) return;
      form.start.value = toLocalInput(r[0]);
      form.end.value = toLocalInput(r[1]);
      container.querySelectorAll('[data-preset]').forEach(b => b.classList.toggle('active', b === btn));
      if (onPick) onPick();
    });
    // Editing a date by hand means no preset is active any more.
    [form.start, form.end].forEach(inp => inp.addEventListener('input', () =>
      container.querySelectorAll('[data-preset]').forEach(b => b.classList.remove('active'))));
  }

  // localStorage can throw (private mode, blocked storage) — never let that break a page.
  const store = {
    get(k, dflt) { try { const v = localStorage.getItem(k); return v === null ? dflt : v; } catch (_) { return dflt; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* ignore */ } },
  };

  // Chart.js plugin: vertical dashed markers for label-only events.
  // Configure with options.plugins.spotEvents = { events: [{x, label}] }.
  const eventLinesPlugin = {
    id: 'spotEvents',
    afterDatasetsDraw(chart, _args, opts) {
      const events = (opts && opts.events) || [];
      const x = chart.scales.x;
      if (!events.length || !x) return;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = 'rgba(248, 81, 73, 0.75)';
      ctx.fillStyle = '#ff9b94';
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.font = '11px system-ui, sans-serif';
      for (const ev of events) {
        const px = x.getPixelForValue(ev.x);
        if (px < chart.chartArea.left || px > chart.chartArea.right) continue;
        ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
        if (ev.label) {
          ctx.save();
          ctx.translate(px + 3, top + 4);
          ctx.rotate(Math.PI / 2);
          ctx.fillText(String(ev.label).slice(0, 40), 0, 0);
          ctx.restore();
        }
      }
      ctx.restore();
    },
  };
  // Chart.js plugin: horizontal dashed lines for alert thresholds.
  // Configure with options.plugins.spotThresholds = { lines: [{y, label, color}] }.
  const thresholdLinesPlugin = {
    id: 'spotThresholds',
    afterDatasetsDraw(chart, _args, opts) {
      const lines = (opts && opts.lines) || [];
      const y = chart.scales.y;
      if (!lines.length || !y) return;
      const { left, right, top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1.2;
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'right';
      for (const ln of lines) {
        const py = y.getPixelForValue(ln.y);
        if (py < top || py > bottom) continue;
        ctx.strokeStyle = ctx.fillStyle = ln.color || '#e3b341';
        ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(right, py); ctx.stroke();
        if (ln.label) ctx.fillText(ln.label, right - 4, py - 4);
      }
      ctx.restore();
    },
  };
  // Chart.js plugin: show a detected range against the schedule. Everything
  // outside [start, end] is dimmed, the detected edges are solid green lines
  // and the scheduled times dotted grey ones.
  // options.plugins.spotRange = { start, end, scheduledStart, scheduledEnd } (epoch ms)
  const rangePlugin = {
    id: 'spotRange',
    beforeDatasetsDraw(chart, _args, o) {
      const x = chart.scales.x;
      if (!o || o.start == null || !x) return;
      const { left, right, top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      const px = v => Math.min(right, Math.max(left, x.getPixelForValue(v)));
      ctx.save();
      ctx.fillStyle = cssVar('--range-dim', 'rgba(0, 0, 0, 0.35)');
      ctx.fillRect(left, top, px(o.start) - left, bottom - top);
      ctx.fillRect(px(o.end), top, right - px(o.end), bottom - top);
      ctx.restore();
    },
    afterDatasetsDraw(chart, _args, o) {
      const x = chart.scales.x;
      if (!o || o.start == null || !x) return;
      const { left, right, top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      const line = (v, color, dash, label, align, atBottom) => {
        const p = x.getPixelForValue(v);
        if (p < left || p > right) return;
        ctx.strokeStyle = ctx.fillStyle = color;
        ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(p, top); ctx.lineTo(p, bottom); ctx.stroke();
        ctx.textAlign = align;
        ctx.fillText(label, p + (align === 'left' ? 4 : -4), atBottom ? bottom - 5 : top + 12);
      };
      ctx.save();
      ctx.lineWidth = 1.5;
      ctx.font = '11px system-ui, sans-serif';
      // Scheduled labels sit at the bottom so they never collide with the detected ones.
      if (o.scheduledStart != null) line(o.scheduledStart, '#8a929d', [2, 3], 'scheduled', 'right', true);
      if (o.scheduledEnd != null) line(o.scheduledEnd, '#8a929d', [2, 3], 'scheduled', 'left', true);
      line(o.start, '#56d364', [], o.startLabel || 'start', 'left');
      line(o.end, '#56d364', [], o.endLabel || 'end', 'right');
      ctx.restore();
    },
  };
  if (window.Chart) window.Chart.register(eventLinesPlugin, thresholdLinesPlugin, rangePlugin);

  // ---- Data gaps. A chart line drawn straight across a stretch with no
  // readings looks like a steady value; instead the line is broken there and
  // the stretch shaded. A gap is a pause much longer than the sensor's usual
  // spacing: more than 5× the median interval, at least 3 buckets when the
  // series is downsampled, and never under 30 s.
  function findGaps(points, opts) {
    const o = Object.assign({ bucketMs: 0, start: null, end: null }, opts);
    const xs = points.map(p => p.x);
    const gaps = [];
    if (xs.length >= 3) {
      const d = [];
      for (let i = 1; i < xs.length; i++) d.push(xs[i] - xs[i - 1]);
      const sorted = [...d].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      const limit = Math.max(5 * median, 3 * o.bucketMs, 30e3);
      for (let i = 0; i < d.length; i++) if (d[i] > limit) gaps.push([xs[i], xs[i + 1]]);
      // Edges: nothing yet at the start of the window / nothing lately.
      if (o.start != null && xs[0] - o.start > limit) gaps.unshift([o.start, xs[0]]);
      if (o.end != null && o.end - xs[xs.length - 1] > limit) gaps.push([xs[xs.length - 1], o.end]);
    }
    return gaps;
  }
  // Insert a null after each gap start so Chart.js breaks the line there.
  function breakAtGaps(points, gaps) {
    if (!gaps.length) return points;
    const starts = new Set(gaps.map(g => g[0]));
    const out = [];
    for (const p of points) { out.push(p); if (starts.has(p.x)) out.push({ x: p.x + 1, y: null }); }
    return out;
  }
  // Chart.js plugin: hatch the gaps. options.plugins.spotGaps = { gaps: [[a, b], …] }
  const gapsPlugin = {
    id: 'spotGaps',
    beforeDatasetsDraw(chart, _args, o) {
      const gaps = (o && o.gaps) || [];
      const x = chart.scales.x;
      if (!gaps.length || !x) return;
      const { left, right, top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
      ctx.fillStyle = cssVar('--gap-fill', 'rgba(138, 146, 157, 0.10)');
      ctx.strokeStyle = cssVar('--gap-hatch', 'rgba(138, 146, 157, 0.22)');
      ctx.lineWidth = 1;
      for (const [a, b] of gaps) {
        const x0 = Math.max(left, x.getPixelForValue(a)), x1 = Math.min(right, x.getPixelForValue(b));
        if (x1 - x0 < 1) continue;
        ctx.fillRect(x0, top, x1 - x0, bottom - top);
        ctx.beginPath();
        for (let k = x0 - (bottom - top); k < x1; k += 8) {
          ctx.moveTo(Math.max(k, x0), top + Math.max(0, x0 - k));
          ctx.lineTo(Math.min(k + (bottom - top), x1), top + Math.min(bottom - top, x1 - k));
        }
        ctx.stroke();
        if (x1 - x0 > 46) {
          ctx.fillStyle = cssVar('--muted', '#8a929d');
          ctx.font = '11px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText('no data', (x0 + x1) / 2, bottom - 6);
          ctx.fillStyle = cssVar('--gap-fill', 'rgba(138, 146, 157, 0.10)');
        }
      }
      ctx.restore();
    },
  };
  if (window.Chart) window.Chart.register(gapsPlugin);

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // One line describing a detection result, for pages.
  function describeDetection(d) {
    if (!d) return '';
    const hm = iso => iso ? iso.slice(11, 16) : '—';
    const edge = k => {
      if (!d[`${k}_detected`]) return `${k} as scheduled (${d[`${k}_reason`]})`;
      const m = Math.round(d[`${k}_offset_seconds`] / 60);
      return `${k} ${m >= 0 ? '+' : '−'}${Math.abs(m)} min (${d[`${k}_reason`]})`;
    };
    return `Scheduled ${hm(d.scheduled_start)}–${hm(d.scheduled_end)} · actual ${hm(d.start)}–${hm(d.end)}: ` +
           `${edge('start')}; ${edge('end')}` + (d.monitor_name ? ` · from ${d.monitor_name}` : '');
  }

  // Tiny dependency-free sparkline; points are [[epochMs, value], ...].
  function sparkline(canvas, points, opts) {
    const o = Object.assign({ color: '#58a6ff', fill: 'rgba(88,166,255,0.15)', start: null, end: null }, opts);
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!points || points.length < 2) return;
    const x0 = o.start ?? points[0][0], x1 = o.end ?? points[points.length - 1][0];
    let lo = Infinity, hi = -Infinity;
    for (const p of points) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
    if (hi === lo) { hi += 1; lo -= 1; }
    const X = t => ((t - x0) / Math.max(1, x1 - x0)) * (w - 2) + 1;
    const Y = v => h - 2 - ((v - lo) / (hi - lo)) * (h - 4);
    ctx.beginPath();
    points.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
    ctx.strokeStyle = o.color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.stroke();
    ctx.lineTo(X(points[points.length - 1][0]), h); ctx.lineTo(X(points[0][0]), h); ctx.closePath();
    ctx.fillStyle = o.fill; ctx.fill();
  }

  // Poll `fn` every `ms` without ever overlapping requests, and pause while
  // the tab is hidden so background tabs don't hammer the server.
  // Returns { setInterval(ms), pause(), resume(), now(), stop() }.
  function poller(fn, ms) {
    let timer = null, running = false, paused = false, interval = ms, stopped = false;
    async function run() {
      timer = null;
      if (stopped || paused || document.hidden) return;
      if (running) return;
      running = true;
      try { await fn(); } catch (e) { console.warn('Spot poll failed:', e); }
      running = false;
      schedule();
    }
    function schedule() {
      if (stopped || paused || document.hidden || timer) return;
      timer = setTimeout(run, interval);
    }
    function clear() { if (timer) { clearTimeout(timer); timer = null; } }
    const onVis = () => { if (document.hidden) clear(); else { clear(); run(); } };
    document.addEventListener('visibilitychange', onVis);
    run();
    return {
      setInterval(n) { interval = n; clear(); schedule(); },
      pause() { paused = true; clear(); },
      resume() { paused = false; clear(); run(); },
      now() { clear(); run(); },
      stop() { stopped = true; clear(); document.removeEventListener('visibilitychange', onVis); },
      get paused() { return paused; },
    };
  }

  // Forms with data-confirm="message" ask before submitting. Reading the
  // message from a data attribute (instead of inline JS) keeps monitor
  // names with quotes from breaking — or injecting into — the handler.
  document.addEventListener('submit', e => {
    const f = e.target;
    if (f instanceof HTMLFormElement && f.dataset.confirm && !window.confirm(f.dataset.confirm)) {
      e.preventDefault();
    }
  });

  // Copy-to-clipboard buttons: <button data-copy="text">.
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const text = btn.dataset.copy;
    const done = () => { const t = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => (btn.textContent = t), 1200); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, () => {});
    } else {
      // Fallback for plain-http deployments where the async clipboard API is unavailable.
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (_) { /* ignore */ }
      ta.remove();
    }
  });

  // '#58a6ff' + 0.15 -> 'rgba(88,166,255,0.15)'.
  function alpha(hex, a) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    if (!m) return `rgba(88,166,255,${a})`;
    return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})`;
  }

  const fmtInt = n => Number(n || 0).toLocaleString('en-US');

  // Native datetime-local controls follow the viewer's OS clock format (often
  // 12-hour) and can't be forced to 24-hour. Each one is swapped for a date
  // picker plus a 24-hour "HH:MM:SS" text box. The original input stays in the
  // form (hidden) and its .value keeps the same "YYYY-MM-DDTHH:MM:SS" shape,
  // so page scripts and form posts don't notice the difference.
  const nativeValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  function normTime(t) {
    const m = /^\s*(\d{1,2})(?::(\d{1,2}))?(?::(\d{1,2}))?\s*$/.exec(t || '');
    if (!m || +m[1] > 23 || +(m[2] || 0) > 59 || +(m[3] || 0) > 59) return null;
    return `${pad(+m[1])}:${pad(+(m[2] || 0))}:${pad(+(m[3] || 0))}`;
  }
  function upgradeDateTime(inp) {
    const initial = nativeValue.get.call(inp);
    const wrap = document.createElement('span');
    wrap.className = 'dt-field';
    const d = document.createElement('input');
    d.type = 'date';
    const t = document.createElement('input');
    Object.assign(t, { type: 'text', inputMode: 'numeric', placeholder: 'HH:MM:SS', size: 8,
                       title: '24-hour time, e.g. 19:30 or 19:30:00' });
    d.required = t.required = inp.required;
    d.setAttribute('aria-label', 'Date');
    t.setAttribute('aria-label', 'Time (24-hour)');
    inp.type = 'hidden';
    inp.required = false;
    inp.after(wrap);
    wrap.append(d, t);
    Object.defineProperty(inp, 'value', {
      configurable: true,
      get() { const tt = normTime(t.value); return d.value && tt ? `${d.value}T${tt}` : ''; },
      set(v) {
        const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)/.exec(v || '');
        d.value = m ? m[1] : '';
        t.value = m ? normTime(m[2]) : '';
        nativeValue.set.call(inp, v || '');
      },
    });
    inp.value = initial;
    const sync = () => {
      t.setCustomValidity(t.value && !normTime(t.value) ? 'Use 24-hour time, e.g. 19:30' : '');
      nativeValue.set.call(inp, inp.value);
      inp.dispatchEvent(new Event('input', { bubbles: true }));
    };
    d.addEventListener('input', sync);
    t.addEventListener('input', sync);
    t.addEventListener('blur', () => { const n = normTime(t.value); if (n) t.value = n; });
  }
  // Pages' inline scripts may already have set values; the swap carries them over.
  document.addEventListener('DOMContentLoaded', () =>
    document.querySelectorAll('input[type=datetime-local]').forEach(upgradeDateTime));

  // Light / dark theme toggle (remembered per browser). Charts read their
  // colours when created, so the page reloads to redraw them.
  document.addEventListener('click', e => {
    if (!e.target.closest('#theme-toggle')) return;
    const light = document.documentElement.dataset.theme !== 'light';
    store.set('spot.theme', light ? 'light' : 'dark');
    location.reload();
  });

  // Fill every .tz-note with the zone in use, e.g. "Times are US Eastern (EDT), 24-hour."
  function zoneLabel() {
    return TZ === 'America/New_York' ? 'US Eastern' : TZ.replace(/_/g, ' ');
  }
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tz-note').forEach(el => {
      el.textContent = `Times are ${zoneLabel()} (${zoneName()}), 24-hour.`;
    });
  });

  window.Spot = { findGaps, breakAtGaps, cssVar, describeDetection, TZ, wall, fromWall, fmtDateTime, fmtTime, zoneName, fmtInt, alpha, escapeHtml, fmtValue, timeAgo, fmtDuration, toLocalInput, toIso,
                  presetRange, bindPresets, store, sparkline, poller };
})();
