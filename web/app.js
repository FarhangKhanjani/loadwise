/* LoadWise phone app.
 *
 * Plain JavaScript, no framework and no build step. Every number shown here comes from the
 * API: the server does all calculations (and has unit tests for them); this file only
 * displays them and sends what you enter. Text from the server is always inserted as text
 * (textContent), never as HTML.
 */
'use strict';

const KEY_STORE = 'loadwise.apiKey';
const TITLES = { today: 'Today', food: 'Food', training: 'Training', history: 'History' };
const SPORT_LABELS = {
  gym: 'Gym', table_tennis: 'Table tennis', table_tennis_match: 'Table tennis match',
  padel: 'Padel', running: 'Running', cycling: 'Cycling', hiking: 'Hiking',
  football: 'Football', other: 'Other',
};
const RPE_WORDS = ['', 'Very easy', 'Easy', 'Moderate', 'Somewhat hard', 'Hard', 'Hard',
  'Very hard', 'Very hard', 'Extremely hard', 'Maximal'];

const view = document.getElementById('view');
const titleEl = document.getElementById('title');
const subtitleEl = document.getElementById('subtitle');
const toastEl = document.getElementById('toast');

let apiKey = readKey();
let meta = null;          // {today, sports, slots, suggested_slot} from /meta
let renderToken = 0;      // ignore results of a screen the user already left
const ui = { foodMode: 'meals', trainingMode: 'session', historyMonth: null };

// ---------- small helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== 'string') el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  el.append(...children);
  return el;
}

function storage(action) {
  try { return action(window.localStorage); } catch { return null; }  // private mode, blocked storage
}
function readKey() { return storage((s) => s.getItem(KEY_STORE)); }
function saveKey(key) { apiKey = key; storage((s) => s.setItem(KEY_STORE, key)); }
function forgetKey() { apiKey = null; storage((s) => s.removeItem(KEY_STORE)); }

function parseDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}
function fmtDay(iso, options = { weekday: 'short', day: 'numeric', month: 'short' }) {
  return parseDate(iso).toLocaleDateString('en-GB', options);
}
function fmt(n, digits = 0) {
  return n == null ? '–' : Number(n).toLocaleString('en-GB', { maximumFractionDigits: digits });
}
function cap(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}
function sportLabel(sport) {
  return SPORT_LABELS[sport] || cap(sport.replace(/_/g, ' '));
}
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
function card(title, ...children) {
  return h('section', { class: 'card' }, title ? h('h2', {}, title) : null, ...children);
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function detailText(data, status) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {  // FastAPI validation errors
    return detail.map((e) => {
      const field = (e.loc || []).filter((p) => p !== 'body' && p !== 'query').join('.');
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return `Request failed (${status})`;
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'X-API-Key': apiKey || '' };
  if (body) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, { method, headers, cache: 'no-store', body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, "Can't reach LoadWise. Is your PC on, LoadWise running and Tailscale connected?");
  }
  const data = await response.json().catch(() => null);
  if (response.status === 401) {
    forgetKey();
    showPairing('This phone is not paired, or the API key changed. Pair it again.');
    throw new ApiError(401, 'Not paired');
  }
  if (!response.ok) throw new ApiError(response.status, detailText(data, response.status));
  return data;
}

// ---------- toast ----------

let toastTimer;
function toast(text, action) {
  const parts = [h('span', {}, text)];
  if (action) {
    parts.push(h('button', {
      type: 'button',
      onclick: async () => {
        toastEl.hidden = true;
        try { await action.run(); } catch (e) { toast(e.message); }
      },
    }, action.label));
  }
  toastEl.replaceChildren(...parts);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, action ? 7000 : 3500);
}

// ---------- routing ----------

function currentTab() {
  const tab = location.hash.slice(1);
  return TITLES[tab] ? tab : 'today';
}

async function render() {
  if (!apiKey) { showPairing(); return; }
  document.body.classList.remove('unpaired');
  const tab = currentTab();
  for (const link of document.querySelectorAll('.tabbar a')) {
    if (link.dataset.tab === tab) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  titleEl.textContent = TITLES[tab];
  const token = ++renderToken;
  view.replaceChildren(h('p', { class: 'loading' }, 'Loading…'));
  try {
    meta = await api('/meta');  // every time: the date may have changed since the last screen
    subtitleEl.textContent = fmtDay(meta.today, { weekday: 'long', day: 'numeric', month: 'long' });
    const content = await SCREENS[tab]();
    if (token === renderToken) view.replaceChildren(...[content].flat());
  } catch (e) {
    if (token !== renderToken || e.status === 401) return;
    view.replaceChildren(card(null,
      h('p', {}, e.message),
      h('div', { class: 'buttons' }, h('button', { type: 'button', class: 'button', onclick: render }, 'Try again'))));
  }
}

// ---------- pairing ----------

function showPairing(message) {
  document.body.classList.add('unpaired');
  titleEl.textContent = 'LoadWise';
  subtitleEl.textContent = 'Connect this phone';
  const input = h('input', { type: 'password', autocomplete: 'off', placeholder: 'API key', 'aria-label': 'API key' });
  const error = h('p', { class: 'error', hidden: true });
  const form = h('form', {
    onsubmit: async (event) => {
      event.preventDefault();
      if (!input.value.trim()) return;
      saveKey(input.value.trim());
      try {
        await api('/meta');
        location.hash = '#today';
        render();
      } catch (e) {
        if (e.status !== 401) { error.textContent = e.message; error.hidden = false; }
      }
    },
  }, input, h('button', { type: 'submit', class: 'button' }, 'Connect'));
  view.replaceChildren(h('section', { class: 'card pair' },
    h('h2', {}, 'Pair this phone'),
    message ? h('p', { class: 'error' }, message) : null,
    h('p', {}, 'On your PC, in the LoadWise folder, run'),
    h('code', {}, 'python -m scripts.pair'),
    h('p', {}, 'and scan the QR code with this phone’s camera. That opens the app already paired.'),
    h('p', { class: 'muted small' }, 'Or paste API_KEY from your .env file:'),
    form, error));
}

// ---------- Today ----------

async function screenToday() {
  const [load, flags, food] = await Promise.all([api('/metrics?days=28'), api('/flags'), api('/nutrition/day')]);
  return [
    loadCard(load),
    flagsCard(flags),
    chartCard(load),
    foodTodayCard(food),
    h('p', { class: 'disclaimer' }, 'Informational only, not medical advice.'),
  ];
}

function lastSessionText(days) {
  if (days == null) return 'No sessions logged yet';
  if (days === 0) return 'Last session: today';
  if (days === 1) return 'Last session: yesterday';
  return `Last session: ${days} days ago`;
}

function loadCard(load) {
  const weeks = load.weeks_with_sessions_28d;
  return card('Training load',
    h('div', { class: 'big' }, h('strong', {}, fmt(load.acwr, 2)), h('span', {}, 'ACWR (acute : chronic)')),
    h('div', { class: 'stats' },
      stat(fmt(load.acute_load_7d), 'Acute load, 7 days'),
      stat(fmt(load.chronic_load_weekly_avg_28d), 'Chronic, weekly avg of 28 days'),
      stat(fmt(load.monotony_7d, 2), 'Monotony, 7 days'),
      stat(fmt(load.strain_7d), 'Strain, 7 days')),
    h('div', { class: 'facts' },
      h('span', {}, lastSessionText(load.days_since_last_session)),
      weeks != null ? h('span', {}, `Sessions logged in ${weeks} of the last 4 weeks`) : null),
    weeks != null && weeks < 4
      ? h('p', { class: 'note' }, 'Weeks without logged sessions count as rest, so the ratios may be off.')
      : null,
    load.note ? h('p', { class: 'note' }, load.note) : null);
}

function stat(value, label) {
  return h('div', { class: 'stat' }, h('b', {}, value), h('span', {}, label));
}

function flagsCard(flags) {
  if (!flags.flags.length) {
    return card('Flags', h('p', { class: 'muted' }, flags.note || 'Nothing stands out in the current data.'));
  }
  return card('Flags', flags.flags.map((f) =>
    h('div', { class: `flag sev-${f.severity}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }),
      h('span', {}, f.message))));
}

function chartCard(load) {
  const days = load.daily_loads;
  const max = Math.max(1, ...days.map((d) => d.load));
  const barW = 7, gap = 3, height = 100;
  const bars = days.map((d, i) => {
    const barH = d.load > 0 ? Math.max(3, (d.load / max) * height) : 2;
    const recent = i >= days.length - 7 ? ' recent' : '';
    return svg('rect', {
      x: i * (barW + gap), y: height - barH, width: barW, height: barH, rx: 2,
      class: `bar${d.load > 0 ? recent : ' zero'}`,
    }, svg('title', {}, `${fmtDay(d.date)}: load ${fmt(d.load)}`));
  });
  const chart = svg('svg', {
    class: 'chart', viewBox: `0 0 ${days.length * (barW + gap) - gap} ${height}`,
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': `Daily training load for the last ${days.length} days, highest ${fmt(max)}`,
  }, ...bars);
  return card('Daily load, last 28 days',
    chart,
    h('div', { class: 'chart-axis' }, h('span', {}, fmtDay(days[0].date)), h('span', {}, `max ${fmt(max)}`), h('span', {}, 'today')),
    h('div', { class: 'legend' }, h('span', {}, h('i', { class: 'recent' }), 'last 7 days'), h('span', {}, h('i'), 'earlier')));
}

function foodTodayCard(food) {
  const body = food.items
    ? [
      h('div', { class: 'big' }, h('strong', {}, fmt(food.kcal)), h('span', {}, `kcal · ${fmt(food.protein)} g protein`)),
      h('div', { class: 'rows' }, meta.slots.map((slot) => h('div', { class: 'row' },
        h('span', {}, cap(slot)),
        h('span', { class: 'right' }, food.by_slot[slot].kcal ? `${fmt(food.by_slot[slot].kcal)} kcal · ${fmt(food.by_slot[slot].protein)} g` : '–')))),
    ]
    : [h('p', { class: 'muted' }, 'Nothing logged yet today.')];
  return card('Food today', ...body,
    h('div', { class: 'buttons' }, h('a', { class: 'button', href: '#food' }, 'Log food'),
      h('a', { class: 'button secondary', href: '#training' }, 'Log training')));
}

// ---------- Food ----------

async function screenFood() {
  const meals = await api('/meals');
  const ctx = { date: meta.today, slot: meta.suggested_slot };

  const total = h('div', { class: 'day-total', 'aria-live': 'polite' });
  ctx.refreshTotal = async () => {
    try {
      const day = await api(`/nutrition/day?date=${ctx.date}`);
      const label = ctx.date === meta.today ? 'Today' : fmtDay(ctx.date);
      total.replaceChildren(h('span', {}, label), h('span', { class: 'num' }, `${fmt(day.kcal)} kcal · ${fmt(day.protein)} g protein`));
    } catch (e) { total.textContent = e.message; }
  };
  ctx.refreshTotal();

  const dateInput = h('input', {
    type: 'date', value: ctx.date, max: meta.today, 'aria-label': 'Date',
    onchange: () => { if (dateInput.value) { ctx.date = dateInput.value; ctx.refreshTotal(); } },
  });
  const slots = chipGroup(meta.slots, ctx.slot, (slot) => { ctx.slot = slot; }, 'Meal');

  const panel = h('div');
  const showMode = (mode) => {
    ui.foodMode = mode;
    panel.replaceChildren(...[mode === 'meals' ? mealsPanel(meals, ctx) : foodsPanel(ctx)].flat());
  };
  const modes = segmented([['meals', 'Saved meals'], ['foods', 'Single food']], ui.foodMode, showMode);
  showMode(ui.foodMode);

  return [total,
    h('div', { class: 'field' }, h('span', {}, 'Date'), dateInput),
    h('div', { class: 'field' }, h('span', {}, 'Meal'), slots),
    modes, panel];
}

function chipGroup(values, selected, onChange, label) {
  const group = h('div', { class: 'chips', role: 'group', 'aria-label': label });
  for (const value of values) {
    group.append(h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(value === selected),
      onclick: (event) => {
        for (const chip of group.children) chip.setAttribute('aria-pressed', String(chip === event.currentTarget));
        onChange(value);
      },
    }, cap(value)));
  }
  return group;
}

function segmented(options, selected, onChange) {
  const box = h('div', { class: 'segmented', role: 'group' });
  for (const [value, label] of options) {
    box.append(h('button', {
      type: 'button', 'aria-pressed': String(value === selected),
      onclick: (event) => {
        for (const b of box.children) b.setAttribute('aria-pressed', String(b === event.currentTarget));
        onChange(value);
      },
    }, label));
  }
  return box;
}

/* An expandable card. Only one is open at a time inside the same list. */
function expandable(list, head, buildBody) {
  const item = h('div', { class: 'item' });
  const button = h('button', { type: 'button', class: 'item-head', 'aria-expanded': 'false' },
    head, h('span', { class: 'chevron', 'aria-hidden': 'true' }, '›'));
  let body = null;
  const close = () => { button.setAttribute('aria-expanded', 'false'); if (body) { body.remove(); body = null; } };
  item.close = close;
  button.addEventListener('click', () => {
    if (body) { close(); return; }
    for (const other of list.children) if (other !== item && other.close) other.close();
    button.setAttribute('aria-expanded', 'true');
    body = h('div', { class: 'item-body' }, buildBody(close));
    item.append(body);
  });
  item.append(button);
  return item;
}

function mealsPanel(meals, ctx) {
  const list = h('div');
  if (!meals.length) return h('p', { class: 'muted' }, 'No saved meals yet.');
  for (const meal of meals) {
    const head = h('span', {}, h('span', { class: 'item-name' }, cap(meal.name)),
      h('span', { class: 'item-sub' }, `${fmt(meal.default.kcal)} kcal · ${fmt(meal.default.protein)} g protein`));
    list.append(expandable(list, head, (close) => mealBody(meal, ctx, close)));
  }
  return list;
}

function amountLabel(item) {
  return item.grams != null ? `${fmt(item.grams)} g` : `${fmt(item.count)} × ${item.portion}`;
}

function mealBody(meal, ctx, close) {
  const include = new Set();
  const grams = {};
  let preview = meal.default;

  const totalNum = h('span', { class: 'num' });
  const error = h('p', { class: 'error', hidden: true });
  const saveButton = h('button', { type: 'button', class: 'button' }, 'Save');

  const rows = meal.items.map((item) => {
    const optional = Boolean(item.optional);
    const label = h('span', { class: 'label' }, cap(item.food));
    const input = h('input', {
      type: 'number', inputmode: 'decimal', min: '0', step: '1', 'aria-label': `${item.food} in grams`,
      oninput: () => {
        if (input.value === '') delete grams[item.food];
        else grams[item.food] = Number(input.value);  // 0 leaves the item out
        update();
      },
    });
    const kcal = h('span', { class: 'kcal' });
    const check = optional
      ? h('input', {
        type: 'checkbox', 'aria-label': `Add ${item.food}`,
        onchange: () => { if (check.checked) include.add(item.food); else include.delete(item.food); update(); },
      })
      : h('span', { class: 'spacer' });
    return { item, optional, label, input, kcal, check, el: h('div', { class: 'line' }, check, label, input, kcal) };
  });

  function show() {
    for (const row of rows) {
      const on = !row.optional || include.has(row.item.food);
      const line = preview.items.find((l) => l.food === row.item.food);
      row.label.classList.toggle('off', !on);
      row.input.disabled = !on;
      if (document.activeElement !== row.input) {
        row.input.value = line ? line.grams : (row.item.food in grams ? grams[row.item.food] : '');
        if (!on) row.input.placeholder = amountLabel(row.item);
      }
      row.kcal.textContent = line ? `${fmt(line.kcal)} kcal` : (on ? '–' : 'optional');
    }
    totalNum.textContent = `${fmt(preview.kcal)} kcal · ${fmt(preview.protein)} g protein`;
  }

  const payload = () => ({ meal: meal.name, date: ctx.date, slot: ctx.slot, include_optional: [...include], grams });

  const update = debounce(async () => {
    try {
      preview = await api('/food-log/meal/preview', { method: 'POST', body: payload() });
      error.hidden = true;
      saveButton.disabled = false;
      show();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      saveButton.disabled = true;
    }
  }, 300);

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true;
    try {
      const saved = await api('/food-log/meal', { method: 'POST', body: payload() });
      const firstId = saved.entries[0].id;
      toast(`Saved ${cap(meal.name)}: ${fmt(saved.kcal)} kcal (${ctx.slot})`, {
        label: 'Undo',
        run: async () => {
          await api(`/food-log/${firstId}?whole_meal=true`, { method: 'DELETE' });
          toast('Removed.');
          ctx.refreshTotal();
        },
      });
      ctx.refreshTotal();
      close();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      saveButton.disabled = false;
    }
  });

  show();
  return [
    h('p', { class: 'muted small' }, 'Amounts in grams. Tick optional items to add them; 0 leaves an item out.'),
    ...rows.map((r) => r.el),
    h('div', { class: 'total' }, h('span', {}, 'Total'), totalNum),
    error,
    h('div', { class: 'buttons' }, h('span'), saveButton),
  ];
}

function foodsPanel(ctx) {
  const results = h('div');
  const search = h('input', {
    type: 'search', placeholder: 'Search, e.g. haferflocken, eier, banana', autocomplete: 'off',
    enterkeyhint: 'search', 'aria-label': 'Search food',
  });
  const run = async () => {
    const q = search.value.trim();
    try {
      const foods = await api(`/foods?q=${encodeURIComponent(q)}`);
      results.replaceChildren();
      if (!foods.length) results.append(h('p', { class: 'muted' }, `No food matches “${q}”.`));
      for (const food of foods) {
        const head = h('span', {}, h('span', { class: 'item-name' }, cap(food.name)),
          h('span', { class: 'item-sub' }, `${fmt(food.kcal_per_100g)} kcal · ${fmt(food.protein_per_100g, 1)} g protein per 100 g`));
        results.append(expandable(results, head, (close) => foodBody(food, ctx, close)));
      }
    } catch (e) {
      results.replaceChildren(h('p', { class: 'error' }, e.message));
    }
  };
  search.addEventListener('input', debounce(run, 250));
  run();
  return [h('div', { class: 'field' }, search), results];
}

function foodBody(food, ctx, close) {
  const portions = Object.entries(food.portions || {});
  const units = [...portions.map(([name]) => name), 'grams'];
  let unit = units[0];

  const amount = h('input', { type: 'number', inputmode: 'decimal', min: '0', 'aria-label': 'Amount' });
  const amountLabelEl = h('span', {});
  const result = h('span', { class: 'num' });
  const error = h('p', { class: 'error', hidden: true });
  const saveButton = h('button', { type: 'button', class: 'button' }, 'Save');

  const setUnit = (value) => {
    unit = value;
    const isGrams = unit === 'grams';
    amount.value = isGrams ? 100 : 1;
    amount.step = isGrams ? '1' : '0.5';
    amountLabelEl.textContent = isGrams ? 'Grams' : `How many (${unit})`;
    update();
  };

  const body = () => {
    const value = Number(amount.value);
    const base = { food: food.name };
    return unit === 'grams' ? { ...base, grams: value } : { ...base, portion: unit, count: value };
  };

  const update = debounce(async () => {
    try {
      const line = await api('/food-log/preview', { method: 'POST', body: body() });
      result.textContent = `${fmt(line.grams)} g · ${fmt(line.kcal)} kcal · ${fmt(line.protein, 1)} g protein`;
      error.hidden = true;
      saveButton.disabled = false;
    } catch (e) {
      result.textContent = '–';
      error.textContent = e.message;
      error.hidden = false;
      saveButton.disabled = true;
    }
  }, 250);
  amount.addEventListener('input', update);

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true;
    try {
      const entry = await api('/food-log', { method: 'POST', body: { ...body(), date: ctx.date, slot: ctx.slot } });
      toast(`Saved ${cap(entry.food)}: ${fmt(entry.kcal)} kcal (${ctx.slot})`, {
        label: 'Undo',
        run: async () => {
          await api(`/food-log/${entry.id}`, { method: 'DELETE' });
          toast('Removed.');
          ctx.refreshTotal();
        },
      });
      ctx.refreshTotal();
      close();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      saveButton.disabled = false;
    }
  });

  const unitChips = chipGroup(units, unit, setUnit, 'Unit');
  // chip text: "1 medium · 118 g"
  portions.forEach(([name, g], i) => { unitChips.children[i].textContent = `${name} · ${fmt(g)} g`; });
  setUnit(unit);

  return [
    h('div', { class: 'field' }, h('span', {}, 'Amount'), unitChips),
    h('label', { class: 'field' }, amountLabelEl, amount),
    h('div', { class: 'total' }, h('span', {}, 'Adds'), result),
    error,
    h('div', { class: 'buttons' }, h('span'), saveButton),
    h('p', { class: 'note' }, `Source: ${food.source}`),
  ];
}

// ---------- Training ----------

async function screenTraining() {
  const recent = await api('/sessions/recent?days=14');
  const panel = h('div');
  const showMode = (mode) => {
    ui.trainingMode = mode;
    panel.replaceChildren(mode === 'session' ? sessionForm(recent) : wellnessForm());
  };
  const modes = segmented([['session', 'Session'], ['wellness', 'Wellness']], ui.trainingMode, showMode);
  showMode(ui.trainingMode);
  return [modes, panel, recentCard(recent)];
}

function field(label, control, hint) {
  return h('label', { class: 'field' },
    h('span', {}, label, hint ? h('span', { class: 'hint' }, ` · ${hint}`) : null), control);
}

function rangeField(label, value, hint, describe) {
  const output = h('span', { class: 'hint' });
  const input = h('input', { type: 'range', min: '1', max: '10', step: '1', value: String(value) });
  const show = () => { output.textContent = ` · ${input.value}${describe ? ` ${describe(Number(input.value))}` : ''}`; };
  input.addEventListener('input', show);
  show();
  const el = h('label', { class: 'field' },
    h('span', {}, label, output),
    input,
    hint ? h('span', { class: 'muted small' }, hint) : null);
  return { el, input };
}

function dateField() {
  const input = h('input', { type: 'date', value: meta.today, max: meta.today });
  return { el: field('Date', input), input };
}

function sessionForm(recent) {
  const lastSport = recent.sessions.length ? recent.sessions[0].sport_type : meta.sports[0];
  const sport = h('select', {}, meta.sports.map((s) =>
    h('option', { value: s, selected: s === lastSport }, sportLabel(s))));
  const duration = h('input', { type: 'number', inputmode: 'numeric', min: '1', max: '600', value: '60' });
  const rpe = rangeField('Effort (RPE)', 6, 'How hard was the whole session? 1 very easy, 10 maximal.',
    (v) => `– ${RPE_WORDS[v]}`);
  const date = dateField();
  const notes = h('input', { type: 'text', placeholder: 'Optional', maxlength: '200' });

  return proposalForm({
    fields: [field('Sport', sport), field('Duration', duration, 'minutes'), rpe.el, date.el, field('Notes', notes)],
    submitLabel: 'Review session',
    submit: () => api('/sessions?notify_telegram=false', {
      method: 'POST',
      body: {
        sport_type: sport.value, duration_min: Number(duration.value), rpe: Number(rpe.input.value),
        date: date.input.value || null, notes: notes.value.trim() || null,
      },
    }),
  });
}

function wellnessForm() {
  const sleep = h('input', { type: 'number', inputmode: 'decimal', min: '0', max: '24', step: '0.25', placeholder: 'e.g. 7.5' });
  const soreness = rangeField('Soreness', 3, '1 none · 10 very sore');
  const fatigue = rangeField('Fatigue', 3, '1 fresh · 10 exhausted');
  const stress = rangeField('Stress', 3, '1 calm · 10 very stressed');
  const hr = h('input', { type: 'number', inputmode: 'numeric', min: '25', max: '220', placeholder: 'Optional' });
  const date = dateField();
  const notes = h('input', { type: 'text', placeholder: 'Optional', maxlength: '200' });

  return proposalForm({
    fields: [field('Sleep', sleep, 'hours last night'), soreness.el, fatigue.el, stress.el,
      field('Resting heart rate', hr, 'bpm'), date.el, field('Notes', notes)],
    submitLabel: 'Review wellness',
    submit: () => {
      if (sleep.value === '') throw new ApiError(422, 'Enter how many hours you slept.');
      return api('/wellness?notify_telegram=false', {
        method: 'POST',
        body: {
          sleep_hours: Number(sleep.value), soreness: Number(soreness.input.value),
          fatigue: Number(fatigue.input.value), stress: Number(stress.input.value),
          resting_hr: hr.value === '' ? null : Number(hr.value),
          date: date.input.value || null, notes: notes.value.trim() || null,
        },
      });
    },
  });
}

/* Form -> proposal (nothing saved yet) -> Save or Discard. Same flow as Telegram's buttons:
 * the server's confirm_action() is the only thing that writes training or wellness data. */
function proposalForm({ fields, submitLabel, submit }) {
  const error = h('p', { class: 'error', hidden: true });
  const button = h('button', { type: 'submit', class: 'button' }, submitLabel);
  const box = h('div');
  const form = h('form', {
    class: 'card',
    onsubmit: async (event) => {
      event.preventDefault();
      button.disabled = true;
      try {
        const proposal = await submit();
        box.replaceChildren(proposalCard(proposal, () => box.replaceChildren(form)));
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
      } finally {
        button.disabled = false;
      }
    },
  }, ...fields, error, button);
  box.append(form);
  return box;
}

function proposalCard(proposal, backToForm) {
  const error = h('p', { class: 'error', hidden: true });
  const decide = (verb) => async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/actions/${proposal.pending_action_id}/${verb}`, { method: 'POST' });
      if (verb === 'confirm') {
        toast(result.result === 'confirmed' ? 'Saved.' : 'Already logged, nothing added.');
        render();
      } else {
        toast('Discarded, nothing saved.');
        backToForm();
      }
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      button.disabled = false;
    }
  };
  return h('section', { class: 'card proposal' },
    h('h2', {}, 'Check and save'),
    h('p', {}, proposal.summary),
    error,
    h('div', { class: 'buttons' },
      h('button', { type: 'button', class: 'button secondary', onclick: decide('reject') }, 'Discard'),
      h('button', { type: 'button', class: 'button', onclick: decide('confirm') }, 'Save')));
}

function recentCard(recent) {
  if (!recent.sessions.length) return card('Last 14 days', h('p', { class: 'muted' }, 'No sessions in the last 14 days.'));
  return card('Last 14 days', h('div', { class: 'rows' }, recent.sessions.map((s) => h('div', { class: 'row' },
    h('span', {}, sportLabel(s.sport_type),
      h('span', { class: 'sub' }, `${fmtDay(s.date)} · ${s.duration_min} min · RPE ${s.rpe}`)),
    h('span', { class: 'right' }, `load ${fmt(s.load)}`)))));
}

// ---------- History ----------

function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

async function screenHistory() {
  const current = meta.today.slice(0, 7);
  if (!ui.historyMonth || ui.historyMonth > current) ui.historyMonth = current;
  const month = await api(`/nutrition/month?month=${ui.historyMonth}`);
  const label = parseDate(`${month.month}-01`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
  const go = (delta) => () => { ui.historyMonth = shiftMonth(ui.historyMonth, delta); render(); };

  const nav = h('div', { class: 'month-nav' },
    h('button', { type: 'button', class: 'icon-button', 'aria-label': 'Previous month', onclick: go(-1) }, '‹'),
    h('h2', {}, label),
    h('button', { type: 'button', class: 'icon-button', 'aria-label': 'Next month', onclick: go(1), disabled: month.month >= current }, '›'));

  const summary = month.days_logged
    ? card('Average per logged day',
      h('div', { class: 'big' }, h('strong', {}, fmt(month.avg_kcal_per_logged_day)), h('span', {}, `kcal · ${fmt(month.avg_protein_per_logged_day)} g protein`)),
      h('div', { class: 'facts' },
        h('span', {}, `Logged ${month.days_logged} of ${month.days_in_range} days`),
        h('span', {}, `Total ${fmt(month.total_kcal)} kcal · ${fmt(month.total_protein)} g protein`)),
      h('p', { class: 'note' }, 'Days without entries are left out of the average: they are more likely not logged than days without food.'))
    : card(null, h('p', { class: 'muted' }, 'Nothing logged this month.'));

  const max = Math.max(1, ...month.days.map((d) => d.kcal));
  const list = h('div');
  for (const day of [...month.days].reverse()) {
    const meter = h('div', { class: 'meter' }, h('div'));
    meter.firstChild.style.width = `${(day.kcal / max) * 100}%`;
    const head = h('span', { class: 'day-head' },
      h('span', {}, fmtDay(day.date, { weekday: 'short', day: 'numeric' })),
      meter,
      h('span', { class: 'num' + (day.logged ? '' : ' muted') }, day.logged ? `${fmt(day.kcal)} kcal` : 'not logged'));
    if (day.logged) {
      list.append(expandable(list, head, () => dayBody(day.date)));
    } else {
      list.append(h('div', { class: 'item' }, h('div', { class: 'item-head' }, head, h('span', { class: 'chevron' }))));
    }
  }
  return [nav, summary, h('h2', { class: 'section-title' }, 'Days'), list];
}

function dayBody(date) {
  const box = h('div', {}, h('p', { class: 'muted small' }, 'Loading…'));
  api(`/nutrition/day?date=${date}`).then((day) => {
    const parts = [];
    for (const slot of meta.slots) {
      const entries = day.entries.filter((e) => e.slot === slot);
      if (!entries.length) continue;
      parts.push(h('div', { class: 'slot-title' }, h('span', {}, cap(slot)),
        h('span', { class: 'num' }, `${fmt(day.by_slot[slot].kcal)} kcal · ${fmt(day.by_slot[slot].protein)} g`)));
      const seenGroups = new Set();
      for (const e of entries) {
        if (e.group_id && e.meal) {
          if (seenGroups.has(e.group_id)) continue;
          seenGroups.add(e.group_id);
          const items = entries.filter((x) => x.group_id === e.group_id);
          parts.push(h('div', { class: 'meal-title' }, h('span', {}, cap(e.meal)),
            deleteButton(`Delete ${e.meal} (${items.length} items)?`, `/food-log/${e.id}?whole_meal=true`, `Delete ${e.meal}`)));
          for (const x of items) parts.push(entryRow(x, true));
        } else {
          parts.push(entryRow(e, false));
        }
      }
    }
    box.replaceChildren(...parts);
  }).catch((e) => box.replaceChildren(h('p', { class: 'error' }, e.message)));
  return box;
}

function entryRow(e, inMeal) {
  return h('div', { class: 'entry' + (inMeal ? ' in-meal' : '') },
    h('span', {}, cap(e.food), h('span', { class: 'muted' }, ` ${fmt(e.grams)} g`)),
    h('span', { class: 'right' }, `${fmt(e.kcal)} kcal`),
    deleteButton(`Delete ${e.food} (${fmt(e.kcal)} kcal)?`, `/food-log/${e.id}`, `Delete ${e.food}`));
}

function deleteButton(question, path, label) {
  return h('button', {
    type: 'button', class: 'icon-button', 'aria-label': label,
    onclick: async () => {
      if (!window.confirm(question)) return;
      try {
        await api(path, { method: 'DELETE' });
        toast('Deleted.');
        render();
      } catch (e) { toast(e.message); }
    },
  }, '×');
}

// ---------- start ----------

const SCREENS = { today: screenToday, food: screenFood, training: screenTraining, history: screenHistory };

/* Pairing link from `python -m scripts.pair`: /app/#key=...  The part after # never leaves
 * the phone (browsers don't send it to the server), and it is removed from the address bar.
 * Checked on start and on every hash change (the link can open in an already open app). */
function takeKeyFromLink() {
  const match = location.hash.match(/^#key=([^&]+)/);
  if (!match) return;
  saveKey(decodeURIComponent(match[1]));
  history.replaceState(null, '', `${location.pathname}#today`);
}

function start() {
  takeKeyFromLink();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
  window.addEventListener('hashchange', () => { takeKeyFromLink(); render(); });
  window.addEventListener('online', render);
  render();
}

start();
