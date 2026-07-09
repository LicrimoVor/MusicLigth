const app = document.querySelector("#app");

const state = {
	token: localStorage.getItem("ml_token") || "",
	role: localStorage.getItem("ml_role") || "user",
	authRole: localStorage.getItem("ml_role") || "",
	lamps: [],
	presets: [],
	runtime: {},
	lampRuntime: {},
	selectedPresetId: "",
	selectedLampId: "",
	draft: null,
	status: "",
	busy: false,
};

const hasOwn = (object, key) =>
	Object.prototype.hasOwnProperty.call(object || {}, key);

const api = async (path, options = {}) => {
	const headers = {
		"Content-Type": "application/json",
		...(options.headers || {}),
	};
	if (state.token) headers.Authorization = `Bearer ${state.token}`;
	const response = await fetch("music" + path, { ...options, headers });
	const data = await response.json().catch(() => ({}));
	if (!response.ok) {
		throw new Error(data.error || `HTTP ${response.status}`);
	}
	return data;
};

const escapeHtml = (value) =>
	String(value ?? "")
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;")
		.replaceAll('"', "&quot;");

const currentPreset = () =>
	state.presets.find((preset) => preset.id === state.selectedPresetId) ||
	state.presets[0];

const clonePreset = (preset) => JSON.parse(JSON.stringify(preset));

const effectLabel = (effect) =>
	({
		static: "Статика",
		fire: "Огонь",
		pulse: "Пульс",
		wave: "Волна",
	})[effect] || "Статика";

const sourcePreset = () => state.draft || currentPreset();

const includedLampIds = (preset = sourcePreset()) =>
	Object.keys(preset?.colors || {});

const isLampIncluded = (lampId, preset = sourcePreset()) =>
	hasOwn(preset?.colors, lampId);

const colorForLamp = (lampId) => {
	const source = sourcePreset();
	return source?.colors?.[lampId] || "#000000";
};

const diagnosticsResult = () => state.runtime?.last_diagnostics_result || null;

const formatDateTime = (value) => {
	if (!value) return "еще не запускалась";
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return String(value);
	return date.toLocaleString("ru-RU", {
		day: "2-digit",
		month: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
	});
};

const diagnosticText = (row) => {
	if (row?.ok === true) return "OK";
	if (row?.ok === false) return row.error || "Ошибка";
	if (row?.status === "dry-run") return "dry-run";
	if (row?.status === "disabled") return "выключена";
	return "нет данных";
};

const diagnosticClass = (row) => {
	if (row?.ok === true) return "ok";
	if (row?.ok === false) return "bad";
	return "idle";
};

const setStatus = (message) => {
	state.status = message;
	const node = document.querySelector("[data-status]");
	if (node) node.textContent = message;
};

const applyServerState = (payload) => {
	if (!payload) return;
	state.authRole = payload.role || state.authRole;
	state.lamps = payload.lamps || [];
	state.presets = payload.presets || [];
	state.runtime = payload.runtime || {};
	state.lampRuntime = payload.lamp_runtime || {};
	if (
		!state.draft &&
		(!state.selectedPresetId ||
			!state.presets.some(
				(preset) => preset.id === state.selectedPresetId,
			))
	) {
		state.selectedPresetId =
			state.runtime.current_preset_id || state.presets[0]?.id || "";
	}
	if (
		!state.selectedLampId ||
		!state.lamps.some((lamp) => lamp.id === state.selectedLampId)
	) {
		state.selectedLampId =
			includedLampIds(currentPreset())[0] || state.lamps[0]?.id || "";
	}
	if (state.draft?.id && state.selectedPresetId !== state.draft.id) {
		state.draft = null;
	}
};

const refreshState = async () => {
	const payload = await api("/api/state");
	applyServerState(payload);
};

const login = async (event) => {
	event.preventDefault();
	const form = new FormData(event.currentTarget);
	state.busy = true;
	render();
	try {
		const payload = await api("/api/login", {
			method: "POST",
			body: JSON.stringify({
				role: state.role,
				password: form.get("password"),
			}),
		});
		state.token = payload.token;
		state.authRole = payload.role;
		localStorage.setItem("ml_token", payload.token);
		localStorage.setItem("ml_role", payload.role);
		applyServerState(payload.state);
		state.status = "";
	} catch (error) {
		state.status = error.message;
	} finally {
		state.busy = false;
		render();
	}
};

const logout = async () => {
	try {
		await api("/api/logout", { method: "POST", body: "{}" });
	} catch {
		// Local logout is enough if the server session already expired.
	}
	state.token = "";
	state.authRole = "";
	state.draft = null;
	localStorage.removeItem("ml_token");
	render();
};

const selectPreset = (presetId) => {
	state.selectedPresetId = presetId;
	state.draft = null;
	const preset = currentPreset();
	state.selectedLampId =
		includedLampIds(preset)[0] || state.lamps[0]?.id || "";
	render();
};

const startEdit = (preset) => {
	state.selectedPresetId = preset.id;
	state.draft = clonePreset(preset);
	state.selectedLampId =
		includedLampIds(state.draft)[0] || state.lamps[0]?.id || "";
	render();
};

const newPreset = () => {
	const firstLamp = state.lamps[0]?.id || "";
	const colors = firstLamp ? { [firstLamp]: "#ff9f43" } : {};
	state.draft = {
		id: "",
		name: "Новый пресет",
		description: "",
		brightness: 80,
		effect: "static",
		colors,
	};
	state.selectedPresetId = "";
	state.selectedLampId = firstLamp;
	render();
};

const updateDraft = (patch, shouldRender = true) => {
	if (!state.draft) return;
	state.draft = { ...state.draft, ...patch };
	if (shouldRender) render();
};

const setLampIncluded = (lampId, included) => {
	if (!state.draft) return;
	const colors = { ...(state.draft.colors || {}) };
	if (included) {
		colors[lampId] = colors[lampId] || "#ff9f43";
	} else {
		delete colors[lampId];
	}
	state.draft = { ...state.draft, colors };
	state.selectedLampId = lampId;
	render();
};

const syncLampColorDom = (lampId, color) => {
	document.querySelectorAll("[data-lamp-color]").forEach((input) => {
		if (input.dataset.lampColor === lampId && input.value !== color) {
			input.value = color;
		}
	});
	document.querySelectorAll("[data-lamp]").forEach((button) => {
		if (button.dataset.lamp !== lampId) return;
		button.style.setProperty("--lamp-color", color);
		button.classList.toggle("off", color === "#000000");
	});
	document.querySelectorAll("[data-select-lamp]").forEach((row) => {
		row.classList.toggle("selected", row.dataset.selectLamp === lampId);
	});
	document.querySelectorAll("[data-lamp]").forEach((button) => {
		button.classList.toggle("selected", button.dataset.lamp === lampId);
	});
};

const updateLampColor = (lampId, color, shouldRender = false) => {
	if (!state.draft) return;
	state.draft.colors = { ...(state.draft.colors || {}), [lampId]: color };
	state.selectedLampId = lampId;
	if (shouldRender) {
		render();
	} else {
		syncLampColorDom(lampId, color);
	}
};

const fillAll = () => {
	if (!state.draft) return;
	const color = state.draft.colors?.[state.selectedLampId] || "#ff9f43";
	state.draft.colors = Object.fromEntries(
		state.lamps.map((lamp) => [lamp.id, color]),
	);
	render();
};

const clearAll = () => {
	if (!state.draft) return;
	state.draft.colors = {};
	render();
};

const saveDraft = async () => {
	if (!state.draft) return;
	if (!Object.keys(state.draft.colors || {}).length) {
		setStatus("Выберите хотя бы одну лампу для пресета");
		return;
	}

	state.busy = true;
	setStatus("Сохраняю...");
	try {
		const payload = state.draft.id
			? await api(`/api/presets/${state.draft.id}`, {
					method: "PUT",
					body: JSON.stringify(state.draft),
				})
			: await api("/api/presets", {
					method: "POST",
					body: JSON.stringify(state.draft),
				});
		applyServerState(payload.state);
		state.selectedPresetId = payload.preset.id;
		state.draft = null;
		state.status = "Сохранено";
	} catch (error) {
		state.status = error.message;
	} finally {
		state.busy = false;
		render();
	}
};

const deletePreset = async (presetId) => {
	if (!window.confirm("Удалить пресет?")) return;
	state.busy = true;
	try {
		const payload = await api(`/api/presets/${presetId}`, {
			method: "DELETE",
		});
		applyServerState(payload.state);
		state.draft = null;
		state.status = "Удалено";
	} catch (error) {
		state.status = error.message;
	} finally {
		state.busy = false;
		render();
	}
};

const applyPreset = async (presetId) => {
	state.busy = true;
	setStatus("Применяю...");
	try {
		const payload = await api("/api/apply", {
			method: "POST",
			body: JSON.stringify({ preset_id: presetId }),
		});
		applyServerState(payload);
		const result = payload.apply_result || {};
		const dry = result.dry_run ? " dry-run" : "";
		const mode = result.animation ? "анимация" : "применено";
		const off = result.off_lamp_count
			? `, выключено ${result.off_lamp_count}`
			: "";
		state.status = `${mode}:${dry} ${result.lamp_count ?? 0} ламп${off}`;
	} catch (error) {
		state.status = error.message;
	} finally {
		state.busy = false;
		render();
	}
};

const runDiagnostics = async () => {
	state.busy = true;
	setStatus("Проверяю лампы...");
	try {
		const payload = await api("/api/diagnostics", {
			method: "POST",
			body: "{}",
		});
		applyServerState(payload);
		const result = payload.diagnostics_result;
		const dry = result?.dry_run ? " dry-run" : "";
		state.status = `Диагностика:${dry} ${result?.ok_count ?? 0}/${result?.lamp_count ?? 0} OK`;
	} catch (error) {
		state.status = error.message;
	} finally {
		state.busy = false;
		render();
	}
};

const renderLogin = () => {
	app.innerHTML = `
    <form class="panel login" data-login>
      <div class="brand">
        <h1>Music Light</h1>
        <span>Пресеты ламп</span>
      </div>
      <div class="segmented" role="tablist">
        <button type="button" class="${state.role === "user" ? "active" : ""}" data-role="user">Пользователь</button>
        <button type="button" class="${state.role === "admin" ? "active" : ""}" data-role="admin">Админ</button>
      </div>
      <label>
        Пароль
        <input name="password" type="password" autocomplete="current-password" required autofocus />
      </label>
      <button class="primary" type="submit" ${state.busy ? "disabled" : ""}>Войти</button>
      <div class="status" data-status>${escapeHtml(state.status)}</div>
    </form>
  `;
	app.querySelector("[data-login]").addEventListener("submit", login);
	app.querySelectorAll("[data-role]").forEach((button) => {
		button.addEventListener("click", () => {
			state.role = button.dataset.role;
			localStorage.setItem("ml_role", state.role);
			render();
		});
	});
};

const renderMap = () => {
	const selected = state.selectedLampId;
	const source = sourcePreset();
	const effect = source?.effect || "static";
	return `
    <section class="panel map-wrap">
      <div class="row">
        <h2 class="panel-title">Лампы</h2>
        <span class="badge">${includedLampIds(source).length}/${state.lamps.length}</span>
      </div>
      <div class="lamp-map effect-${effect}" data-map>
        ${state.lamps
			.map((lamp, index) => {
				const included = isLampIncluded(lamp.id, source);
				const color = colorForLamp(lamp.id);
				const [x, y] = lamp.position || [0.5, 0.5];
				const classes = [
					"lamp-dot",
					!included || color === "#000000" ? "off" : "",
					!included ? "excluded" : "",
					selected === lamp.id ? "selected" : "",
				]
					.filter(Boolean)
					.join(" ");
				return `
              <button
                class="${classes}"
                type="button"
                title="${escapeHtml(lamp.id)}"
                data-lamp="${escapeHtml(lamp.id)}"
                style="left:${x * 100}%; top:${(1 - y) * 100}%; --lamp-color:${color};"
              >${index + 1}</button>
            `;
			})
			.join("")}
      </div>
    </section>
  `;
};

const renderPresetCard = (preset) => {
	const active = preset.id === state.runtime.current_preset_id;
	const selected = preset.id === state.selectedPresetId;
	const presetLamps = state.lamps.filter((lamp) =>
		isLampIncluded(lamp.id, preset),
	);
	const swatches = presetLamps
		.map(
			(lamp) =>
				`<span class="swatch" style="--swatch-color:${preset.colors[lamp.id] || "#000000"}"></span>`,
		)
		.join("");
	return `
    <article class="preset-card ${active || selected ? "active" : ""}" data-preset="${escapeHtml(preset.id)}">
      <div class="preset-head">
        <div>
          <div class="preset-title">${escapeHtml(preset.name)}</div>
          <div class="muted">${presetLamps.length} ламп · ${preset.brightness}% · ${effectLabel(preset.effect)}</div>
        </div>
        <button class="primary" type="button" data-apply="${escapeHtml(preset.id)}" ${state.busy ? "disabled" : ""}>Вкл</button>
      </div>
      <div class="swatches">${swatches}</div>
      ${
			state.authRole === "admin"
				? `<div class="actions">
              <button type="button" data-edit="${escapeHtml(preset.id)}">Править</button>
              <button class="danger" type="button" data-delete="${escapeHtml(preset.id)}">Удалить</button>
            </div>`
				: ""
		}
    </article>
  `;
};

const renderPresetList = () => `
  <section class="grid">
    <div class="row">
      <h2 class="panel-title">Пресеты</h2>
      ${state.authRole === "admin" ? `<button type="button" data-new>Новый</button>` : ""}
    </div>
    <div class="preset-list">
      ${state.presets.length ? state.presets.map(renderPresetCard).join("") : `<div class="panel empty">Пресетов нет</div>`}
    </div>
  </section>
`;

const renderDiagnosticsPanel = () => {
	if (state.authRole !== "admin") return "";
	const result = diagnosticsResult();
	const rows = result?.rows || [];
	const running = state.runtime?.diagnostics_running;
	const summary = result
		? `${result.ok_count ?? 0}/${result.lamp_count ?? 0} OK`
		: "нет данных";
	return `
    <section class="panel diagnostics-panel">
      <div class="row">
        <div>
          <h2 class="panel-title">Диагностика</h2>
          <div class="muted">${running ? "проверка запущена" : formatDateTime(state.runtime?.last_diagnostics_at)} · ${summary}</div>
        </div>
        <button type="button" data-diagnostics ${state.busy || running ? "disabled" : ""}>Проверить</button>
      </div>
      ${
			rows.length
				? `<div class="diagnostic-list">
            ${rows
				.map(
					(row, index) => `
                <div class="diagnostic-row">
                  <span class="state-dot ${diagnosticClass(row)}"></span>
                  <div>
                    <div>${index + 1}. ${escapeHtml(row.label || row.id?.slice(-6) || "Лампа")}</div>
                    <div class="muted">${escapeHtml(row.ip || row.id || "")}</div>
                  </div>
                  <div class="diagnostic-status">${escapeHtml(diagnosticText(row))}</div>
                </div>
              `,
				)
				.join("")}
          </div>`
				: `<div class="empty">Диагностика еще не запускалась</div>`
		}
    </section>
  `;
};

const renderEditor = () => {
	if (state.authRole !== "admin" || !state.draft) return "";
	return `
    <section class="panel editor">
      <div class="row">
        <h2>Редактор</h2>
        <button class="ghost" type="button" data-cancel>Закрыть</button>
      </div>
      <div class="field-grid">
        <label>
          Название
          <input data-draft="name" value="${escapeHtml(state.draft.name)}" />
        </label>
        <label>
          Яркость
          <input data-draft="brightness" type="number" min="0" max="100" value="${state.draft.brightness}" />
        </label>
      </div>
      <label>
        Эффект
        <select data-draft="effect">
          ${["static", "fire", "pulse", "wave"]
				.map(
					(effect) =>
						`<option value="${effect}" ${state.draft.effect === effect ? "selected" : ""}>${effectLabel(effect)}</option>`,
				)
				.join("")}
        </select>
      </label>
      <div class="lamp-editor-list">
        ${state.lamps
			.map((lamp, index) => {
				const included = isLampIncluded(lamp.id, state.draft);
				const color = state.draft.colors?.[lamp.id] || "#000000";
				const selected = state.selectedLampId === lamp.id;
				return `
            <div class="lamp-edit-row ${selected ? "selected" : ""}" data-select-lamp="${escapeHtml(lamp.id)}">
              <input type="checkbox" data-include="${escapeHtml(lamp.id)}" ${included ? "checked" : ""} />
              <div class="lamp-edit-title">
                <strong>${index + 1}. ${escapeHtml(lamp.label)}</strong>
                <span>${escapeHtml(lamp.short_id)} · ${escapeHtml(lamp.ip)}</span>
              </div>
              <input data-lamp-color="${escapeHtml(lamp.id)}" type="color" value="${color}" ${included ? "" : "disabled"} />
            </div>
          `;
			})
			.join("")}
      </div>
      <div class="actions">
        <button type="button" data-fill>Все этим цветом</button>
        <button type="button" data-clear>Убрать все</button>
        <button class="primary wide" type="button" data-save ${state.busy ? "disabled" : ""}>Сохранить</button>
      </div>
    </section>
  `;
};

const renderApp = () => {
	const dry = state.lampRuntime?.dry_run ? "dry-run" : "live";
	const role = state.authRole === "admin" ? "Админ" : "Пользователь";
	const animation = state.lampRuntime?.animation?.running
		? ` · ${effectLabel(state.lampRuntime.animation.effect)}`
		: "";
	app.innerHTML = `
    <header class="topbar">
      <div class="brand">
        <h1>Music Light</h1>
        <span>${role} · ${dry}${animation}</span>
      </div>
      <button class="ghost" type="button" data-logout>Выйти</button>
    </header>
    <div class="grid workspace">
      <div class="grid">
        ${renderPresetList()}
        ${renderDiagnosticsPanel()}
        ${renderEditor()}
        <div class="status" data-status>${escapeHtml(state.status)}</div>
      </div>
      ${renderMap()}
    </div>
  `;

	app.querySelector("[data-logout]").addEventListener("click", logout);
	app.querySelectorAll("[data-preset]").forEach((card) => {
		card.addEventListener("click", (event) => {
			if (event.target.closest("button")) return;
			selectPreset(card.dataset.preset);
		});
	});
	app.querySelectorAll("[data-apply]").forEach((button) => {
		button.addEventListener("click", () =>
			applyPreset(button.dataset.apply),
		);
	});
	app.querySelectorAll("[data-edit]").forEach((button) => {
		button.addEventListener("click", () => {
			const preset = state.presets.find(
				(item) => item.id === button.dataset.edit,
			);
			if (preset) startEdit(preset);
		});
	});
	app.querySelectorAll("[data-delete]").forEach((button) => {
		button.addEventListener("click", () =>
			deletePreset(button.dataset.delete),
		);
	});
	app.querySelector("[data-new]")?.addEventListener("click", newPreset);
	app.querySelector("[data-diagnostics]")?.addEventListener(
		"click",
		runDiagnostics,
	);
	app.querySelectorAll("[data-lamp], [data-select-lamp]").forEach(
		(button) => {
			button.addEventListener("click", () => {
				state.selectedLampId =
					button.dataset.lamp || button.dataset.selectLamp;
				render();
			});
		},
	);
	app.querySelector("[data-cancel]")?.addEventListener("click", () => {
		state.draft = null;
		render();
	});
	app.querySelector("[data-save]")?.addEventListener("click", saveDraft);
	app.querySelector("[data-fill]")?.addEventListener("click", fillAll);
	app.querySelector("[data-clear]")?.addEventListener("click", clearAll);
	app.querySelectorAll("[data-include]").forEach((input) => {
		input.addEventListener("click", (event) => event.stopPropagation());
		input.addEventListener("change", () =>
			setLampIncluded(input.dataset.include, input.checked),
		);
	});
	app.querySelectorAll("[data-lamp-color]").forEach((input) => {
		input.addEventListener("click", (event) => event.stopPropagation());
		input.addEventListener("pointerdown", (event) =>
			event.stopPropagation(),
		);
		input.addEventListener("input", () =>
			updateLampColor(input.dataset.lampColor, input.value),
		);
		input.addEventListener("change", () =>
			updateLampColor(input.dataset.lampColor, input.value),
		);
	});
	app.querySelectorAll("[data-draft]").forEach((input) => {
		input.addEventListener("input", () => {
			const key = input.dataset.draft;
			updateDraft(
				{
					[key]:
						key === "brightness"
							? Number(input.value)
							: input.value,
				},
				key === "effect",
			);
		});
	});
};

const render = () => {
	if (!state.token) {
		renderLogin();
	} else {
		renderApp();
	}
};

const bootstrap = async () => {
	render();
	if (!state.token) return;
	try {
		await refreshState();
		state.status = "";
	} catch (error) {
		state.status = error.message;
		if (
			String(error.message).includes("авторизация") ||
			String(error.message).includes("401")
		) {
			localStorage.removeItem("ml_token");
			state.token = "";
		}
	}
	render();
};

bootstrap();
