"use strict";

/*
 * Интерфейс Cheat String Diff.
 *
 * ВАЖНО: строки из дампов - это недоверенные данные (читы могут
 * подсунуть в память что угодно, в том числе HTML). Поэтому все они
 * выводятся только через textContent и никогда через innerHTML.
 * Подсветка поиска тоже собирается из текстовых узлов и <mark>.
 */

// Не называем константу api: contextBridge создаёт неизменяемое window.api,
// и повторное объявление того же имени на верхнем уровне роняет весь скрипт.
const bridge = window.api;
const $ = (id) => document.getElementById(id);

const DEFAULTS = {
  minLen: "8",
  minEntropy: "2.6",
  maxVowel: "0.3",
  minUnique: "4",
  minHits: "",
};

const MIN_FILES = { clean: 2, cheat: 1 };

const PAGE_SIZE = 500;
const MAX_LOG_LINES = 300;
const MAX_DOTS = 8;
const ENTROPY_FULL_BAR = 6;

const GROUP_LABEL = { clean: "Clean", cheat: "Cheat" };
const ENC_LABEL = { 1: "ASCII", 2: "UTF-16", 3: "ASCII + UTF-16" };

const numberFormat = new Intl.NumberFormat("ru-RU");
const fmt = (value) => numberFormat.format(value);

const SORTERS = {
  reliable: null, // порядок движка: больше попаданий, затем длиннее
  length: (a, b) => b.s.length - a.s.length,
  entropy: (a, b) => b.n - a.n,
  alpha: (a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0),
};

const state = {
  files: { clean: [], cheat: [] },
  pythonOk: false,
  running: false,
  progress: newProgress(0),
  items: [],
  meta: null,
  unchecked: new Set(),
  view: [],
  shown: PAGE_SIZE,
  query: "",
  enc: "",
  logLines: [],
};

/* ==================================
   ХЕЛПЕРЫ
================================== */

const basename = (filePath) => filePath.split(/[\\/]/).pop() || filePath;

function setStatus(message, kind = "info") {
  const element = $("status");

  element.textContent = message;
  element.dataset.kind = kind;
  element.hidden = !message;
}

/* Три состояния правой части: пусто, идёт анализ, результат. */
function showView(name) {
  $("viewEmpty").hidden = name !== "empty";
  $("viewRunning").hidden = name !== "running";
  $("viewResults").hidden = name !== "results";
  $("stats").hidden = name !== "results";

  if (name !== "results") $("truncNote").hidden = true;
}

/* ==================================
   СПИСКИ ФАЙЛОВ
================================== */

function renderFiles(group) {
  const files = state.files[group];
  const list = document.querySelector(`.filelist[data-group="${group}"]`);
  const zone = document.querySelector(`.dropzone[data-group="${group}"]`);

  list.replaceChildren();
  zone.classList.toggle("has-files", files.length > 0);

  files.forEach((file, index) => {
    const item = document.createElement("li");

    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = basename(file);
    name.title = file;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.dataset.index = String(index);
    remove.setAttribute("aria-label", `Убрать ${basename(file)}`);
    remove.textContent = "×";

    item.append(name, remove);
    list.append(item);
  });

  const minimum = MIN_FILES[group];
  const chip = $(`count-${group}`);

  chip.textContent = files.length >= minimum ? String(files.length) : `${files.length} из ${minimum}`;
  chip.dataset.state = files.length >= minimum ? "ok" : "need";

  if (!state.running) resetProgress();
  updateRunState();
}

function addFiles(group, paths) {
  if (state.running) return;

  const other = group === "clean" ? "cheat" : "clean";
  let conflicts = 0;

  for (const filePath of paths) {
    if (!filePath) continue;

    if (state.files[other].includes(filePath)) {
      conflicts += 1;
    } else if (!state.files[group].includes(filePath)) {
      state.files[group].push(filePath);
    }
  }

  renderFiles(group);

  setStatus(
    conflicts
      ? "Файл уже есть в другом списке: его нельзя использовать и как clean, и как cheat."
      : "",
    "warn"
  );
}

async function pickFiles(group) {
  if (state.running) return;
  addFiles(group, await bridge.pickFiles(group));
}

function clearFiles(group) {
  if (state.running) return;

  state.files[group] = [];
  renderFiles(group);
}

function updateRunState() {
  const cleanMissing = Math.max(0, MIN_FILES.clean - state.files.clean.length);
  const cheatMissing = Math.max(0, MIN_FILES.cheat - state.files.cheat.length);

  let hint = "";

  if (!state.pythonOk) {
    hint = "Python 3.8+ не найден: установите его и перезапустите программу.";
  } else if (cleanMissing) {
    hint = `Добавьте ещё clean-дампов: ${cleanMissing}.`;
  } else if (cheatMissing) {
    hint = "Добавьте хотя бы один cheat-дамп.";
  }

  $("runHint").textContent = hint;
  $("runBtn").disabled = state.running || Boolean(hint);
}

/* ==================================
   ПАРАМЕТРЫ
================================== */

function updateParamsSummary() {
  const hits = $("minHits").value.trim();

  $("paramsSummary").textContent =
    `длина от ${$("minLen").value}, энтропия от ${$("minEntropy").value}` +
    (hits ? `, в ${hits} дампах` : "");
}

function resetParams() {
  for (const [id, value] of Object.entries(DEFAULTS)) {
    $(id).value = value;
  }

  updateParamsSummary();
}

function readParams() {
  const hits = $("minHits").value.trim();

  return {
    clean: state.files.clean,
    cheat: state.files.cheat,
    minLen: Number($("minLen").value),
    minEntropy: Number($("minEntropy").value),
    maxVowel: Number($("maxVowel").value),
    minUnique: Number($("minUnique").value),
    minHits: hits === "" ? null : Number(hits),
  };
}

/* ==================================
   ЗАПУСК И ПРОГРЕСС
================================== */

function setRunning(value) {
  state.running = value;

  $("runBtn").hidden = value;
  $("cancelBtn").hidden = !value;
  $("params").disabled = value;

  updateRunState();
}

function setStage(text) {
  $("stage").textContent = text;
  $("runStage").textContent = text;
}

/*
 * Прогресс. Движок может обрабатывать несколько файлов одновременно, поэтому
 * храним долю по каждому файлу, а общий процент берём из событий движка.
 */
function newProgress(total) {
  return {
    total,
    overall: 0,
    fractions: new Map(), // номер сегмента -> доля файла
    names: new Map(), // номер сегмента -> подпись для строки состояния
    finished: new Set(), // номера завершённых сегментов
  };
}

/* Номер сегмента для файла: сначала clean, потом cheat. */
function segmentOf(group, index) {
  return group === "clean" ? index - 1 : state.files.clean.length + index - 1;
}

/* Запасной расчёт, если движок не прислал общий процент. */
function computeOverall() {
  const { total, fractions, finished } = state.progress;

  if (!total) return 0;

  let sum = finished.size;

  for (const [segment, fraction] of fractions) {
    if (!finished.has(segment)) sum += fraction;
  }

  return Math.min(1, sum / total);
}

function describeActive() {
  const { fractions, finished, names, total } = state.progress;
  const active = [...fractions.keys()].filter((segment) => !finished.has(segment));

  if (active.length === 1) return names.get(active[0]);

  if (active.length > 1) {
    return `Обрабатывается файлов: ${active.length}, готово ${finished.size} из ${total}`;
  }

  return `Обработано файлов: ${finished.size} из ${total}`;
}

/* Один сегмент на каждый дамп: сразу видно, на каком файле идёт работа. */
function paintSegments() {
  const box = $("steps");
  const { total, fractions, finished } = state.progress;
  const count = total || state.files.clean.length + state.files.cheat.length;

  if (box.children.length !== count) {
    box.replaceChildren(
      ...Array.from({ length: count }, () => document.createElement("span"))
    );
  }

  [...box.children].forEach((segment, index) => {
    const fraction = fractions.get(index) || 0;

    segment.className = "";
    segment.style.removeProperty("--p");

    if (finished.has(index)) {
      segment.className = "done";
    } else if (state.running && fraction > 0) {
      segment.className = "now";
      segment.style.setProperty("--p", `${Math.round(fraction * 100)}%`);
    }
  });
}

function updateProgress() {
  const percent = Math.floor(state.progress.overall * 100);
  const text = `${percent}%`;

  $("percent").textContent = text;
  $("runPercent").textContent = text;
  $("steps").setAttribute("aria-valuenow", String(percent));

  paintSegments();
}

/* Общий процент не должен откатываться назад. */
function bumpOverall(event) {
  const value = typeof event.overall === "number" ? event.overall : computeOverall();

  state.progress.overall = Math.max(state.progress.overall, Math.min(1, value));
}

function resetProgress() {
  state.progress = newProgress(0);
  setStage("Готов к запуску");
  updateProgress();
}

function appendLog(line) {
  state.logLines.push(line);

  if (state.logLines.length > MAX_LOG_LINES) {
    state.logLines.shift();
  }

  $("log").textContent = state.logLines.join("\n");
}

async function startRun() {
  setStatus("");

  state.items = [];
  state.meta = null;
  state.logLines = [];
  $("log").textContent = "";

  state.progress = newProgress(state.files.clean.length + state.files.cheat.length);

  setStage("Запуск движка");
  showView("running");
  setRunning(true);
  updateProgress();

  const response = await bridge.runEngine(readParams());

  if (!response.ok) {
    setRunning(false);
    showView("empty");
    resetProgress();
    setStatus(response.error, "error");
  }
}

function handleEngineEvent(event) {
  switch (event.type) {
    case "file_progress": {
      const segment = segmentOf(event.group, event.index);

      state.progress.fractions.set(segment, event.fraction);
      state.progress.names.set(
        segment,
        `${GROUP_LABEL[event.group] || event.group}: ${basename(event.path)} ` +
        `(${event.index} из ${event.total})`
      );

      bumpOverall(event);
      setStage(describeActive());
      updateProgress();
      break;
    }

    case "file_done": {
      const segment = segmentOf(event.group, event.index);

      state.progress.fractions.set(segment, 1);
      state.progress.finished.add(segment);

      bumpOverall(event);
      setStage(describeActive());
      updateProgress();
      break;
    }

    case "result":
      for (let i = 0; i < state.progress.total; i += 1) {
        state.progress.finished.add(i);
      }

      state.progress.overall = 1;
      updateProgress();
      showResults(event);
      break;

    case "log":
      appendLog(event.line);
      break;

    case "finished":
      setRunning(false);

      if (event.cancelled) {
        showView("empty");
        setStage("Анализ отменён");
        setStatus("Анализ отменён.");
      } else {
        setStage("Готово");
      }
      break;

    case "error":
      setRunning(false);
      showView("empty");
      setStage("Ошибка");
      setStatus(event.message, "error");
      break;

    default:
      break;
  }
}

/* ==================================
   РЕЗУЛЬТАТЫ
================================== */

function showResults(event) {
  state.items = event.items;
  state.meta = event;
  state.unchecked = new Set();

  showView("results");
  applyView();
}

function applyView() {
  state.query = $("search").value.trim();

  const query = state.query.toLowerCase();
  const sorter = SORTERS[$("sortBy").value];

  const view = [];

  state.items.forEach((item, index) => {
    if (query && !item.s.toLowerCase().includes(query)) return;
    if (state.enc === "ascii" && !(item.e & 1)) return;
    if (state.enc === "utf16" && !(item.e & 2)) return;

    view.push(index);
  });

  if (sorter) {
    view.sort((a, b) => sorter(state.items[a], state.items[b]) || a - b);
  }

  state.view = view;
  state.shown = PAGE_SIZE;

  renderRows();
}

/* Подсветка найденного текста: только текстовые узлы и <mark>. */
function fillHighlighted(target, text, query) {
  if (!query) {
    target.textContent = text;
    return;
  }

  const lower = text.toLowerCase();
  const needle = query.toLowerCase();

  let position = 0;

  for (;;) {
    const at = lower.indexOf(needle, position);
    if (at === -1) break;

    if (at > position) {
      target.append(document.createTextNode(text.slice(position, at)));
    }

    const mark = document.createElement("mark");
    mark.textContent = text.slice(at, at + needle.length);
    target.append(mark);

    position = at + needle.length;
  }

  if (position < text.length) {
    target.append(document.createTextNode(text.slice(position)));
  }
}

function buildHits(item, total) {
  const cell = document.createElement("td");
  cell.className = "r";

  if (total > MAX_DOTS) {
    const label = document.createElement("span");
    label.className = "num";
    label.textContent = `${item.h} из ${total}`;
    cell.append(label);
    return cell;
  }

  const dots = document.createElement("div");
  dots.className = "dots";
  dots.title = `Найдена в ${item.h} из ${total} cheat-дампов`;

  for (let i = 0; i < total; i += 1) {
    const dot = document.createElement("i");
    if (i < item.h) dot.className = "on";
    dots.append(dot);
  }

  cell.append(dots);
  return cell;
}

function buildRow(index) {
  const item = state.items[index];
  const total = state.meta ? state.meta.cheat_counts.length : 0;

  const row = document.createElement("tr");
  row.classList.toggle("off", state.unchecked.has(index));

  const checkCell = document.createElement("td");
  checkCell.className = "c-check";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = !state.unchecked.has(index);
  checkbox.dataset.index = String(index);
  checkbox.setAttribute("aria-label", "Включить строку в экспорт");
  checkCell.append(checkbox);

  const stringCell = document.createElement("td");
  stringCell.className = "str";

  const code = document.createElement("code");
  fillHighlighted(code, item.s, state.query);
  stringCell.append(code);

  const encCell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.dataset.enc = String(item.e);
  badge.textContent = ENC_LABEL[item.e] || "ASCII";
  encCell.append(badge);

  const lengthCell = document.createElement("td");
  lengthCell.className = "r num";
  lengthCell.textContent = String(item.s.length);

  const entropyCell = document.createElement("td");
  entropyCell.className = "r";

  const entropy = document.createElement("div");
  entropy.className = "ent";

  const bar = document.createElement("i");
  bar.className = "bar";
  bar.style.setProperty("--w", `${Math.min(100, (item.n / ENTROPY_FULL_BAR) * 100).toFixed(0)}%`);

  const entropyValue = document.createElement("span");
  entropyValue.className = "num";
  entropyValue.textContent = item.n.toFixed(2);

  entropy.append(bar, entropyValue);
  entropyCell.append(entropy);

  row.append(checkCell, stringCell, encCell, lengthCell, entropyCell, buildHits(item, total));

  return row;
}

function renderRows() {
  const fragment = document.createDocumentFragment();

  for (const index of state.view.slice(0, state.shown)) {
    fragment.append(buildRow(index));
  }

  $("rows").replaceChildren(fragment);

  const remaining = state.view.length - state.shown;

  $("moreBtn").hidden = remaining <= 0;
  $("moreBtn").textContent = `Показать ещё ${fmt(Math.min(PAGE_SIZE, Math.max(remaining, 0)))}`;

  const empty = $("empty");

  if (state.items.length === 0) {
    empty.textContent =
      "Уникальных строк не нашлось: всё из cheat-дампов есть и в clean-дампах. " +
      "Попробуйте снизить энтропию или разрешить строкам быть не во всех cheat-дампах.";
  } else if (state.view.length === 0) {
    empty.textContent = "По этому фильтру ничего нет.";
  }

  empty.hidden = state.view.length > 0;

  updateStats();
}

function updateStats() {
  if (!state.meta) return;

  const { total, truncated, elapsed } = state.meta;
  const selected = state.items.length - state.unchecked.size;
  const shown = Math.min(state.shown, state.view.length);

  $("statTotal").textContent = fmt(total);
  $("statSelected").textContent = fmt(selected);
  $("statElapsed").textContent = `${numberFormat.format(elapsed)} с`;

  $("shownInfo").textContent = state.view.length
    ? `Показано ${fmt(shown)} из ${fmt(state.view.length)}`
    : "";

  const note = $("truncNote");
  note.hidden = !truncated;

  if (truncated) {
    note.textContent = `Движок вернул только лучшие ${fmt(state.items.length)} из ${fmt(total)}.`;
  }
}

function setViewChecked(value) {
  for (const index of state.view) {
    if (value) state.unchecked.delete(index);
    else state.unchecked.add(index);
  }

  renderRows();
}

function setEncoding(value) {
  state.enc = value;

  for (const button of document.querySelectorAll(".seg button")) {
    const active = button.dataset.enc === value;

    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
  }

  applyView();
}

/* ==================================
   ЭКСПОРТ
================================== */

async function exportItems(kind) {
  const items = [];

  state.items.forEach((item, index) => {
    if (!state.unchecked.has(index)) {
      items.push({ value: item.s, enc: item.e });
    }
  });

  if (items.length === 0) {
    setStatus("Отметьте хотя бы одну строку для экспорта.", "warn");
    return;
  }

  const response = await bridge.exportItems({
    kind,
    items,
    ruleName: $("ruleName").value,
    minMatches: Number($("minMatches").value),
  });

  if (response.ok) {
    const note = response.skipped
      ? ` Пропущено строк с недопустимыми символами: ${response.skipped}.`
      : "";

    setStatus(`Сохранено: ${response.path}.${note}`, "ok");
  } else if (!response.cancelled) {
    setStatus(response.error || "Не удалось сохранить файл.", "error");
  }
}

/* ==================================
   ПОДКЛЮЧЕНИЕ СОБЫТИЙ
================================== */

function setupDropzones() {
  for (const zone of document.querySelectorAll(".dropzone")) {
    zone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      zone.classList.add("over");
    });

    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      zone.classList.add("over");
    });

    zone.addEventListener("dragleave", (event) => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove("over");
    });

    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("over");

      const paths = [...event.dataTransfer.files].map((file) => bridge.pathForFile(file));
      addFiles(zone.dataset.group, paths);
    });
  }

  // Чтобы файл, брошенный мимо зоны, не открывался как страница.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
}

function setupEvents() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;

    const { action, group } = button.dataset;

    if (action === "add") pickFiles(group);
    if (action === "clear") clearFiles(group);
  });

  for (const list of document.querySelectorAll(".filelist")) {
    list.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-index]");
      if (!button || state.running) return;

      const group = list.dataset.group;
      state.files[group].splice(Number(button.dataset.index), 1);
      renderFiles(group);
    });
  }

  $("runBtn").addEventListener("click", startRun);
  $("cancelBtn").addEventListener("click", () => bridge.cancelEngine());
  $("resetParams").addEventListener("click", resetParams);
  $("params").addEventListener("input", updateParamsSummary);

  $("search").addEventListener("input", applyView);
  $("sortBy").addEventListener("change", applyView);

  for (const button of document.querySelectorAll(".seg button")) {
    button.addEventListener("click", () => setEncoding(button.dataset.enc));
  }

  $("checkView").addEventListener("click", () => setViewChecked(true));
  $("uncheckView").addEventListener("click", () => setViewChecked(false));

  $("moreBtn").addEventListener("click", () => {
    state.shown += PAGE_SIZE;
    renderRows();
  });

  $("rows").addEventListener("change", (event) => {
    const box = event.target;

    if (!(box instanceof HTMLInputElement) || box.type !== "checkbox") return;

    const index = Number(box.dataset.index);

    if (box.checked) state.unchecked.delete(index);
    else state.unchecked.add(index);

    box.closest("tr").classList.toggle("off", !box.checked);
    updateStats();
  });

  $("exportTxt").addEventListener("click", () => exportItems("txt"));
  $("exportYara").addEventListener("click", () => exportItems("yara"));

  bridge.onEngineEvent(handleEngineEvent);
}

async function checkPython() {
  const label = $("pyStatus");
  const result = await bridge.checkPython();

  state.pythonOk = Boolean(result.ok);

  label.dataset.state = result.ok ? "ok" : "error";
  label.textContent = result.ok ? `Python ${result.version}` : "Python не найден";

  updateRunState();
}

document.addEventListener("DOMContentLoaded", () => {
  setupDropzones();
  setupEvents();
  updateParamsSummary();
  renderFiles("clean");
  renderFiles("cheat");
  showView("empty");
  checkPython();
});
