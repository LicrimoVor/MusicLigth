const app = document.querySelector("#app");
const assetBase = new URL(".", document.currentScript.src);
const readLocal = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const state = {
  token: localStorage.getItem("ml_token") || "", role: localStorage.getItem("ml_role") || "user",
  lamps: [], presets: [], runtime: {}, lampRuntime: {}, selected: "", view: "scenes", filter: "all",
  search: "", favorites: readLocal("ml_favorites", []), brightness: 100, draft: null,
  busy: false, connected: false, message: "", error: false, loading: true,
};
const effects = { static: "Постоянный свет", fire: "Мерцание огня", pulse: "Пульс", wave: "Волна" };
const palette = ["#ffffff", "#ffd18b", "#ff7849", "#ff456c", "#bd66ff", "#3455ff", "#249bff", "#16d9d0", "#62d68c", "#000000"];
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const clone = (value) => JSON.parse(JSON.stringify(value));
const selected = () => state.presets.find(p => p.id === state.selected);
const admin = () => state.role === "admin";
const disabled = () => state.busy ? "disabled" : "";
const icon = (name) => {
  const paths = {
    light: '<path d="M9 18h6m-5 3h4M8.5 14.5a6 6 0 1 1 7 0c-.8.6-1.5 1.5-1.5 2.5h-4c0-1-.7-1.9-1.5-2.5Z"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    sliders: '<path d="M4 7h8m4 0h4M4 17h3m4 0h9"/><circle cx="14" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    power: '<path d="M12 3v9m-6-7a9 9 0 1 0 12 0"/>',
    arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0l-1 1-1-1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
  };
  return `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.light}</svg>`;
};

async function api(path, body, method = body === undefined ? "GET" : "POST") {
  const response = await fetch(new URL(`api/${path}`, assetBase), {
    method, headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(45000),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "login") {
      state.token = ""; state.draft = null; localStorage.removeItem("ml_token");
    }
    const result = data.apply_result;
    const details = result?.errors?.map(row => `${row.id.slice(-6)}: ${row.error}`).join("; ");
    throw new Error(data.error || details || result?.error || `Ошибка сервера (${response.status})`);
  }
  state.connected = true;
  return data;
}

function accept(payload) {
  state.role = payload.role;
  state.lamps = payload.lamps || [];
  state.presets = payload.presets || [];
  state.runtime = payload.runtime || {};
  state.lampRuntime = payload.lamp_runtime || {};
  if (!selected()) {
    state.selected = state.runtime.current_preset_id || state.presets[0]?.id || "";
    state.brightness = selected()?.brightness ?? 100;
  }
}

async function action(work, success = "") {
  if (state.busy) return;
  state.busy = true; state.message = ""; state.error = false;
  app.querySelectorAll("button").forEach(button => button.disabled = true);
  app.setAttribute("aria-busy", "true");
  try { await work(); state.message = success; }
  catch (error) {
    state.message = error.name === "TimeoutError" ? "Сервер не ответил вовремя. Обновите состояние перед повторной командой." : error.message;
    state.error = true;
    if (error instanceof TypeError || error.name === "TimeoutError") state.connected = false;
  }
  finally { state.busy = false; app.removeAttribute("aria-busy"); render(); }
}

function choose(id) { state.selected = id; state.brightness = selected()?.brightness ?? 100; render(); }
function apply(id = state.selected) {
  const brightness = id === state.selected ? state.brightness : state.presets.find(p => p.id === id)?.brightness;
  return action(async () => {
    accept(await api("apply", { preset_id: id, brightness }));
    state.selected = id; state.brightness = brightness;
  }, state.lampRuntime.dry_run ? "Проверка выполнена. Команды на лампы не отправлялись." : "Сцена включена");
}
function notify() { return `<div class="notification ${state.error ? "error" : ""}" role="${state.error ? "alert" : "status"}" ${state.message ? "" : "hidden"}>${esc(state.message)}</div>`; }

function renderLogin() {
  app.innerHTML = `<div class="login-layout"><section class="login-art"><div class="wordmark">${icon("light")} music light<span class="tiny">HOME LIGHTING</span></div><div class="login-orbit"><i></i><i></i><i></i><div>${icon("light")}</div></div><div><span class="eyebrow">ВАШ ДОМ. ВАШ СВЕТ.</span><h1>Настроение<br>начинается<br>со света<span>.</span></h1><p>Тихий вечер, любимая музыка или новый день.<br>Найдите свет для своего момента.</p></div></section>
    <section class="login-side"><form id="login-form" class="login-form"><span class="eyebrow">ДОБРО ПОЖАЛОВАТЬ</span><h2>Включите своё настроение</h2><p class="muted">Войдите, чтобы управлять освещением.</p>
    <div class="segmented" aria-label="Роль"><button type="button" data-role="user" aria-pressed="${!admin()}" class="${!admin() ? "active" : ""}">Пользователь</button><button type="button" data-role="admin" aria-pressed="${admin()}" class="${admin() ? "active" : ""}">Администратор</button></div>
    <label>Пароль<div class="password-wrap"><input name="password" id="password" type="password" autocomplete="current-password" required placeholder="Введите пароль" ${disabled()}><button type="button" data-action="password" aria-label="Показать пароль">Показать</button></div></label>
    <button class="primary full" type="submit" ${disabled()}>${state.busy ? "Входим…" : "Войти"} ${icon("arrow")}</button>${notify()}<p class="login-note">${admin() ? "Управление пресетами и диагностика устройств" : "Готовые сцены и простое управление светом"}</p></form></section></div>`;
}

function sidebar() {
  const nav = (view, title, symbol) => `<button data-view="${view}" aria-label="${title}" class="nav-item ${state.view === view ? "active" : ""}" ${state.view === view ? 'aria-current="page"' : ""}>${icon(symbol)}${title}</button>`;
  return `<aside class="sidebar"><div class="wordmark">${icon("light")} music light</div><div class="sidebar-caption">ПРОСТРАНСТВО</div><nav aria-label="Основная навигация">${nav("scenes", "Мои сцены", "grid")}${admin() ? `${nav("manage", "Редактор пресетов", "sliders")}${nav("devices", "Устройства", "light")}` : ""}</nav><div class="sidebar-bottom"><div class="connection"><span class="state-dot ${state.connected ? "ok" : "bad"}"></span>${state.connected ? "Сервер подключён" : "Нет связи с сервером"}</div><div class="account"><span class="avatar">${admin() ? "А" : "П"}</span><div><strong>${admin() ? "Администратор" : "Пользователь"}</strong><small>${state.lampRuntime.dry_run ? "Режим проверки" : "Управление домом"}</small></div><button class="icon-button" data-action="logout" title="Выйти" aria-label="Выйти">${icon("arrow")}</button></div></div></aside>`;
}

function sceneCard(preset, index) {
  const colors = [...new Set(Object.values(preset.colors))];
  const a = colors[0] || "#ffffff", b = colors[1] || a, c = colors[2] || b;
  const active = state.runtime.current_preset_id === preset.id;
  const favorite = state.favorites.includes(preset.id);
  return `<article class="scene-card ${preset.id === state.selected ? "selected" : ""}"><div class="scene-art" style="--c1:${a};--c2:${b};--c3:${c}"><span class="scene-number">${String(index + 1).padStart(2, "0")}</span><div class="light-shape"></div><button class="favorite ${favorite ? "is-favorite" : ""}" data-favorite="${esc(preset.id)}" aria-label="${favorite ? "Убрать из избранного" : "В избранное"}: ${esc(preset.name)}" aria-pressed="${favorite}">${icon("heart")}</button>${active ? `<span class="playing">${icon("check")} Включено${state.runtime.last_result?.brightness === 0 ? " · 0%" : ""}</span>` : ""}</div><div class="scene-body"><button class="scene-title" data-select="${esc(preset.id)}">${esc(preset.name)}</button><p>${esc(preset.description || effects[preset.effect])}</p><div class="scene-meta"><span>${effects[preset.effect]}</span><span>${preset.brightness}%</span></div><div class="scene-actions"><button data-select="${esc(preset.id)}">Настроить</button><button class="apply-card" data-apply="${esc(preset.id)}" aria-label="Включить ${esc(preset.name)}">${icon("power")}</button></div>${state.view === "manage" ? `<div class="manage-actions"><button data-edit="${esc(preset.id)}">Изменить</button><button data-copy="${esc(preset.id)}">Копия</button><button class="danger-text" data-delete="${esc(preset.id)}">Удалить</button></div>` : ""}</div></article>`;
}

function filteredPresets() {
  return state.presets.filter(p => `${p.name} ${p.description}`.toLocaleLowerCase("ru").includes(state.search.toLocaleLowerCase("ru")))
    .filter(p => state.filter === "all" || (state.filter === "favorites" ? state.favorites.includes(p.id) : state.filter === "static" ? p.effect === "static" : p.effect !== "static"));
}
function cards() {
  const presets = filteredPresets();
  return presets.length ? presets.map(sceneCard).join("") : `<div class="empty"><h3>${state.presets.length ? "Сцен не найдено" : "Пока нет сцен"}</h3><p>${state.lamps.length ? "Создайте пресет или измените фильтры." : admin() ? "Добавьте лампы в app/core/lamps.json и перезапустите сервер." : "Администратор ещё не настроил освещение."}</p></div>`;
}

function lampMap(preset) {
  return `<div class="lamp-map" aria-label="Предпросмотр расположения ламп"><div class="room-outline"><span>ВАШЕ ПРОСТРАНСТВО</span><div class="room-table"></div></div>${state.lamps.map((lamp, i) => {
    const color = preset?.colors[lamp.id] || "#000000";
    const off = !lamp.enabled || color === "#000000" || !state.brightness;
    const [x, y] = lamp.position || [0.5, 0.5];
    return `<span class="lamp-dot ${off ? "off" : ""}" style="left:${8 + x * 84}%;top:${8 + (1 - y) * 84}%;--lamp-color:${color};--intensity:${state.brightness / 100}" title="${esc(lamp.label)} · ${color.toUpperCase()}${!lamp.enabled ? " · отключена" : ""}">${i + 1}</span>`;
  }).join("")}</div>`;
}

function preview() {
  const preset = selected();
  return `<aside class="preview panel"><div class="row"><span class="eyebrow">ПРЕДПРОСМОТР</span><span class="pill">${Object.keys(preset?.colors || {}).filter(id => state.lamps.some(l => l.id === id && l.enabled)).length} ламп</span></div><div id="preview-map">${lampMap(preset)}</div><h2>${esc(preset?.name || "Выберите сцену")}</h2><p class="muted preview-description">${esc(preset?.description || "Выберите пресет, чтобы увидеть цвета ламп.")}</p><div class="divider"></div><label for="live-brightness" class="row">Яркость <output id="live-output">${state.brightness}%</output></label><input id="live-brightness" type="range" min="0" max="100" value="${state.brightness}" ${!preset ? "disabled" : ""}><div class="range-labels"><span>Тихий свет</span><span>Яркий</span></div><button class="primary full" data-action="apply" ${!preset || !state.lamps.some(l => l.enabled) ? "disabled" : ""}>${icon("power")} Включить сцену</button><p class="hint">Яркость применится при включении сцены.<br>Сохранённый пресет останется прежним.</p>${admin() && preset ? `<button class="text-button full" data-edit="${esc(preset.id)}">Редактировать пресет ${icon("arrow")}</button>` : ""}</aside>`;
}

function devices() {
  const result = state.runtime.last_diagnostics_result;
  const date = state.runtime.last_diagnostics_at ? new Date(state.runtime.last_diagnostics_at).toLocaleString("ru-RU") : "Проверка ещё не выполнялась";
  const rows = state.lamps.map(lamp => {
    const row = result?.rows?.find(r => r.id === lamp.id);
    const status = !lamp.enabled ? "Отключена" : state.lampRuntime.dry_run ? "Без проверки связи" : row?.ok === true ? "На связи" : row?.ok === false ? "Недоступна" : "Нет данных";
    return `<tr><td><strong>${esc(lamp.label)}</strong><small>${esc(lamp.short_id)}</small></td><td>${esc(lamp.ip || "Не задан")}</td><td><span class="device-state"><span class="state-dot ${lamp.enabled && row?.ok === true ? "ok" : lamp.enabled && row?.ok === false ? "bad" : ""}"></span>${status}</span>${row?.error ? `<small class="danger-text">${esc(row.error)}</small>` : ""}</td><td>${row?.latency_ms != null ? `${row.latency_ms} мс` : "—"}</td></tr>`;
  }).join("");
  return `<section class="panel devices"><div class="row"><div><h2>Состояние ламп</h2><p class="muted">${esc(date)}</p></div><button class="primary" data-action="diagnostics" ${state.runtime.diagnostics_running ? "disabled" : ""}>${state.runtime.diagnostics_running ? "Проверка…" : "Проверить связь"}</button></div><div class="table-scroll"><table><thead><tr><th>Устройство</th><th>IP-адрес</th><th>Состояние</th><th>Отклик</th></tr></thead><tbody>${rows || '<tr><td colspan="4">Лампы ещё не настроены</td></tr>'}</tbody></table></div><p class="hint">${state.lampRuntime.dry_run ? "Режим проверки: команды не отправляются, доступность устройств не проверяется." : "Диагностика проверяет доступность ламп в локальной сети."}</p>${state.lampRuntime.init_error ? `<p class="notification error">${esc(state.lampRuntime.init_error)}</p>` : ""}</section>`;
}

function editor() {
  const draft = state.draft;
  if (!draft) return "";
  return `<dialog id="editor-dialog" aria-labelledby="editor-title"><form id="editor-form"><header class="row"><div><span class="eyebrow">СТУДИЯ СВЕТА</span><h2 id="editor-title">${draft.id ? "Редактировать пресет" : "Новый пресет"}</h2></div><button type="button" class="icon-button" data-action="close-editor" aria-label="Закрыть редактор">${icon("close")}</button></header><div class="editor-content"><div class="field-grid"><label>Название<input name="name" data-draft="name" maxlength="80" value="${esc(draft.name)}" required placeholder="Например, уютный вечер"></label><label>Эффект<select data-draft="effect">${Object.entries(effects).map(([key, label]) => `<option value="${key}" ${draft.effect === key ? "selected" : ""}>${label}</option>`).join("")}</select></label></div><label>Описание<textarea data-draft="description" maxlength="240" rows="2" placeholder="Для какого настроения эта сцена?">${esc(draft.description)}</textarea></label><div class="field-grid"><label>Яркость, %<div class="paired-input"><input type="range" data-draft="brightness" min="0" max="100" value="${draft.brightness}" aria-label="Яркость ползунок"><input type="number" data-draft="brightness" min="0" max="100" step="1" required value="${draft.brightness}" aria-label="Яркость числом"></div></label><label>Скорость эффекта, ×<input type="number" data-draft="speed" min="0.25" max="3" step="0.05" value="${draft.speed ?? 1}" required><small>0,25 — медленно · 3 — быстро</small></label></div><div class="white-settings"><div class="field-grid"><label>Белый цвет<select data-draft="white_mode"><option value="auto" ${draft.white_mode !== "rgb" ? "selected" : ""}>Белый канал лампы</option><option value="rgb" ${draft.white_mode === "rgb" ? "selected" : ""}>Смешение RGB (совместимость)</option></select></label><label>Температура белого, %<input type="number" data-draft="white_temperature" min="0" max="100" step="1" required value="${draft.white_temperature ?? 50}"><small>0 — тёплый · 50 — нейтральный · 100 — холодный</small></label></div><p class="hint">Белый канал используется для #FFFFFF и серых оттенков. Для ламп без белых светодиодов выберите RGB.</p></div><div class="row"><h3>Цвета ламп</h3><span class="muted">HEX или палитра</span></div><div class="bulk-colors"><label>Общий цвет<input id="bulk-color" type="color" value="#ffffff"></label><div class="palette">${palette.map(color => `<button type="button" data-palette="${color}" style="--swatch:${color}" title="${color.toUpperCase()}" aria-label="Общий цвет ${color.toUpperCase()}"></button>`).join("")}</div><button type="button" data-action="fill">Для выбранных</button></div><div class="row selection-actions"><button type="button" class="text-button" data-action="select-all">Выбрать все</button><button type="button" class="text-button" data-action="select-none">Снять выбор</button></div><div class="lamp-editor-list">${state.lamps.map((lamp, i) => {
    const included = Object.hasOwn(draft.colors, lamp.id), color = draft.colors[lamp.id] || "#ffffff";
    return `<div class="lamp-edit-row"><label class="lamp-check"><input type="checkbox" data-include="${esc(lamp.id)}" ${included ? "checked" : ""}><span><strong>${i + 1}. ${esc(lamp.label)}</strong><small>${esc(lamp.short_id)}${!lamp.enabled ? " · отключена в конфигурации" : ""}</small></span></label><input type="color" data-color="${esc(lamp.id)}" value="${color}" aria-label="Цвет ${esc(lamp.label)}" ${included ? "" : "disabled"}><input class="hex-input" data-hex="${esc(lamp.id)}" value="${color.toUpperCase()}" pattern="#[0-9a-fA-F]{6}" maxlength="7" required aria-label="HEX ${esc(lamp.label)}" ${included ? "" : "disabled"}></div>`;
  }).join("") || '<p class="empty">Для создания пресета добавьте лампы в конфигурацию.</p>'}</div><p class="hint">Лампы без галочки будут выключены при включении пресета.</p><p id="editor-error" role="alert" class="danger-text">${state.error ? esc(state.message) : ""}</p></div><footer class="editor-footer"><button type="button" data-action="close-editor">Отмена</button><button type="submit" class="primary" ${!state.lamps.length ? "disabled" : ""}>${icon("check")} Сохранить пресет</button></footer></form></dialog>`;
}

function render() {
  const previousDialog = document.querySelector("#editor-dialog");
  if (previousDialog?.open) previousDialog.close();
  if (!state.token) { renderLogin(); return; }
  if (state.loading) { app.innerHTML = '<div class="loading" role="status">Загружаем ваше пространство…</div>'; return; }
  const active = state.presets.find(p => p.id === state.runtime.current_preset_id);
  const heading = state.view === "devices" ? "Устройства" : state.view === "manage" ? "Ваша студия света" : "Свет под ваше настроение";
  app.innerHTML = `${sidebar()}<div class="main-content"><header class="topbar"><div class="breadcrumbs">Мой дом <span>/</span> ${state.view === "devices" ? "Устройства" : "Освещение"}</div><div class="topbar-actions"><span class="mode-label">${state.lampRuntime.dry_run ? "Режим проверки" : "Управление лампами"}</span><button class="icon-button" data-action="refresh" title="Обновить состояние" aria-label="Обновить состояние">↻</button></div></header><section class="page-heading"><div><span class="eyebrow">MUSIC LIGHT · ${admin() && state.view !== "scenes" ? "АДМИНИСТРАТОР" : "КОЛЛЕКЦИЯ СЦЕН"}</span><h1>${heading}</h1><p class="muted">${state.view === "devices" ? "Проверяйте связь и находите устройства, которым нужно внимание." : state.view === "manage" ? "Создавайте сцены, сочетайте цвета и настраивайте каждую лампу." : "От первого утреннего света до последней песни вечера."}</p></div>${admin() && state.view !== "devices" ? `<button class="primary" data-action="new" ${!state.lamps.length ? "disabled" : ""}>${icon("plus")} Новый пресет</button>` : ""}</section>${notify()}<section class="now-playing"><div class="now-icon">${icon("light")}</div><div><span class="eyebrow">СЕЙЧАС ВКЛЮЧЕНО</span><strong>${esc(active?.name || (state.runtime.last_result?.brightness === 0 ? "Свет выключен" : "Сцена ещё не выбрана"))}</strong></div><div class="now-meta">${active ? `${state.runtime.last_result?.brightness ?? active.brightness}% яркости` : `${state.lamps.filter(l => l.enabled).length} ламп в пространстве`}</div><button class="off-button" data-action="off" ${!state.lamps.some(l => l.enabled) ? "disabled" : ""}>${icon("power")} Выключить всё</button></section>${state.lampRuntime.animation?.error ? `<div class="notification error" role="alert">Анимация остановлена: ${esc(state.lampRuntime.animation.error)}</div>` : ""}${state.view === "devices" ? devices() : `<div class="workspace"><section><div class="collection-toolbar"><div class="filters" aria-label="Фильтр сцен">${[["all", "Все сцены"], ["static", "Спокойные"], ["animated", "Динамичные"], ["favorites", "Избранное"]].map(([key, label]) => `<button data-filter="${key}" aria-pressed="${state.filter === key}" class="${state.filter === key ? "active" : ""}">${label}</button>`).join("")}</div><input type="search" id="search" placeholder="Найти сцену…" value="${esc(state.search)}" aria-label="Поиск сцен"></div><div class="scene-grid" id="scene-grid">${cards()}</div></section>${preview()}</div>`}<footer class="page-footer"><span>music light</span><span>Маленькая деталь. Другое настроение.</span></footer></div>${editor()}`;
  const dialog = document.querySelector("#editor-dialog");
  if (dialog) { dialog.showModal(); dialog.addEventListener("cancel", event => { event.preventDefault(); closeEditor(); }); }
}

function openEditor(preset, copy = false) {
  if (!admin()) return;
  state.error = false; state.message = "";
  state.draft = preset ? clone(preset) : { name: "", description: "", brightness: 80, effect: "static", speed: 1, white_mode: "auto", white_temperature: 50, colors: Object.fromEntries(state.lamps.filter(l => l.enabled).map(l => [l.id, "#ffffff"])) };
  if (copy) { delete state.draft.id; state.draft.name = `${state.draft.name.slice(0, 72)} (копия)`; }
  state.originalDraft = JSON.stringify(state.draft); render();
}
function closeEditor() {
  if (state.busy) return;
  if (JSON.stringify(state.draft) !== state.originalDraft && !window.confirm("Закрыть редактор без сохранения изменений?")) return;
  state.draft = null; render();
}

app.addEventListener("submit", event => {
  event.preventDefault();
  if (state.busy) return;
  if (event.target.id === "login-form") {
    const password = new FormData(event.target).get("password");
    action(async () => {
      const payload = await api("login", { role: state.role, password });
      state.token = payload.token; state.loading = false; state.view = "scenes";
      localStorage.setItem("ml_token", payload.token); localStorage.setItem("ml_role", payload.role); accept(payload.state);
    });
  }
  if (event.target.id === "editor-form") {
    if (!Object.keys(state.draft.colors).length) { document.querySelector("#editor-error").textContent = "Выберите хотя бы одну лампу."; return; }
    const draft = clone(state.draft);
    action(async () => {
      const payload = await api(draft.id ? `presets/${encodeURIComponent(draft.id)}` : "presets", draft, draft.id ? "PUT" : "POST");
      accept(payload.state); state.selected = payload.preset.id; state.brightness = payload.preset.brightness; state.draft = null;
    }, "Пресет сохранён. Чтобы включить его, нажмите «Включить сцену».");
  }
});

app.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button || button.disabled || state.busy) return;
  const d = button.dataset;
  if (d.role) { state.role = d.role; render(); }
  if (d.view) { state.view = d.view; state.search = ""; render(); }
  if (d.select) choose(d.select);
  if (d.apply) apply(d.apply);
  if (d.filter) { state.filter = d.filter; render(); }
  if (d.favorite) {
    state.favorites = state.favorites.includes(d.favorite) ? state.favorites.filter(id => id !== d.favorite) : [...state.favorites, d.favorite];
    localStorage.setItem("ml_favorites", JSON.stringify(state.favorites)); render();
  }
  if (d.edit || d.copy) openEditor(state.presets.find(p => p.id === (d.edit || d.copy)), !!d.copy);
  if (d.delete && window.confirm(`Удалить пресет «${state.presets.find(p => p.id === d.delete)?.name}»?`)) action(async () => accept((await api(`presets/${encodeURIComponent(d.delete)}`, {}, "DELETE")).state), "Пресет удалён");
  if (d.palette) document.querySelector("#bulk-color").value = d.palette;
  const actions = {
    password: () => { const input = document.querySelector("#password"); input.type = input.type === "password" ? "text" : "password"; button.textContent = input.type === "password" ? "Показать" : "Скрыть"; button.setAttribute("aria-label", `${button.textContent} пароль`); },
    logout: () => action(async () => { try { await api("logout", {}); } finally { state.token = ""; state.draft = null; state.selected = ""; localStorage.removeItem("ml_token"); } }),
    refresh: () => action(async () => accept(await api("state")), "Состояние обновлено"),
    off: () => action(async () => accept(await api("off", {})), state.lampRuntime.dry_run ? "Выключение проверено без отправки команд" : "Свет выключен"),
    apply: () => apply(), new: () => openEditor(), "close-editor": closeEditor,
    diagnostics: () => action(async () => {
      const payload = await api("diagnostics", {}); accept(payload);
      if (!payload.diagnostics_result.ok) throw new Error(payload.diagnostics_result.error || "Некоторые лампы недоступны");
    }, "Диагностика завершена"),
    fill: () => { const color = document.querySelector("#bulk-color").value; Object.keys(state.draft.colors).forEach(id => state.draft.colors[id] = color); syncColors(); },
    "select-all": () => { state.lamps.forEach(l => state.draft.colors[l.id] ||= "#ffffff"); syncColors(); },
    "select-none": () => { state.draft.colors = {}; syncColors(); },
  };
  actions[d.action]?.();
});

function syncColors() {
  document.querySelectorAll("[data-include]").forEach(input => {
    const id = input.dataset.include, included = Object.hasOwn(state.draft.colors, id);
    input.checked = included;
    input.closest(".lamp-edit-row").querySelectorAll("[data-color], [data-hex]").forEach(field => { field.disabled = !included; field.value = state.draft.colors[id] || "#ffffff"; field.setCustomValidity(""); });
  });
}
app.addEventListener("input", event => {
  const input = event.target;
  if (input.id === "search") { state.search = input.value; document.querySelector("#scene-grid").innerHTML = cards(); }
  if (input.id === "live-brightness") { state.brightness = Number(input.value); document.querySelector("#live-output").textContent = `${state.brightness}%`; document.querySelector("#preview-map").innerHTML = lampMap(selected()); }
  if (!state.draft) return;
  if (input.dataset.draft) {
    const key = input.dataset.draft;
    state.draft[key] = ["brightness", "speed", "white_temperature"].includes(key) ? Number(input.value) : input.value;
    if (key === "brightness") document.querySelectorAll('[data-draft="brightness"]').forEach(field => { if (field !== input) field.value = input.value; });
  }
  const id = input.dataset.color || input.dataset.hex;
  if (id) {
    if (!/^#[0-9a-f]{6}$/i.test(input.value)) { input.setCustomValidity("Введите цвет в формате #FFFFFF"); return; }
    input.setCustomValidity(""); state.draft.colors[id] = input.value.toLowerCase();
    input.closest(".lamp-edit-row").querySelectorAll("[data-color], [data-hex]").forEach(field => { if (field !== input) { field.value = input.value; field.setCustomValidity(""); } });
  }
});
app.addEventListener("change", event => {
  const id = event.target.dataset.include;
  if (!id || !state.draft) return;
  if (event.target.checked) state.draft.colors[id] = "#ffffff"; else delete state.draft.colors[id];
  syncColors();
});
window.addEventListener("beforeunload", event => { if (state.draft && JSON.stringify(state.draft) !== state.originalDraft) { event.preventDefault(); event.returnValue = ""; } });
async function bootstrap() {
  render();
  if (state.token) { try { accept(await api("state")); } catch (error) { state.message = error.message; state.error = true; } }
  state.loading = false; render();
}
let polling = false;
setInterval(async () => {
  if (!state.token || state.busy || state.draft || state.loading || polling || document.hidden || document.activeElement?.matches("input, textarea, select")) return;
  polling = true;
  try { const payload = await api("state"); if (!state.busy && !state.draft) { accept(payload); render(); } }
  catch (error) { state.connected = false; if (!state.busy && !state.draft) { state.message = error.message; state.error = true; render(); } }
  finally { polling = false; }
}, 10000);
bootstrap();
