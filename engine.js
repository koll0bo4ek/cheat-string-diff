"use strict";

/*
 * Запуск Python-движка (engine/cheat_string_diff.py).
 *
 * Этот модуль не зависит от Electron, поэтому его можно проверять
 * обычным `node test/smoke.js`.
 *
 * Безопасность: аргументы передаются массивом (без shell), а все пути
 * и числа проверяются до запуска.
 */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const MIN_PYTHON_MINOR = 8;

const PROBE_CODE =
  "import sys;print(sys.executable);print('.'.join(map(str,sys.version_info[:3])))";

const UTF8_ENV = { PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };

let cachedPython;

/* ==================================
   PYTHON
   ==================================
   Спрашиваем у самого интерпретатора его настоящий путь.
   Так мы запускаем python.exe напрямую (а не лаунчер `py`),
   и отмена анализа гарантированно убивает нужный процесс.
   ================================== */

function candidates() {
  const list = [];

  if (process.env.CHEAT_DIFF_PYTHON) {
    list.push([process.env.CHEAT_DIFF_PYTHON, []]);
  }

  if (process.platform === "win32") {
    list.push(["py", ["-3"]], ["python", []], ["python3", []]);
  } else {
    list.push(["python3", []], ["python", []]);
  }

  return list;
}

function probe(command, preArgs) {
  const result = spawnSync(command, [...preArgs, "-c", PROBE_CODE], {
    encoding: "utf8",
    windowsHide: true,
    timeout: 8000,
    env: { ...process.env, ...UTF8_ENV },
  });

  if (result.error || result.status !== 0) return null;

  const [executable, version] = String(result.stdout).trim().split(/\r?\n/);
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version || "");

  if (!executable || !match) return null;
  if (Number(match[1]) !== 3 || Number(match[2]) < MIN_PYTHON_MINOR) return null;

  return { command: executable.trim(), version };
}

function findPython(force = false) {
  if (cachedPython !== undefined && !force) return cachedPython;

  cachedPython = null;

  for (const [command, preArgs] of candidates()) {
    const found = probe(command, preArgs);
    if (found) {
      cachedPython = found;
      break;
    }
  }

  return cachedPython;
}

/* ==================================
   VALIDATION
   ================================== */

function checkFiles(list, label, minCount) {
  if (!Array.isArray(list) || list.length < minCount) {
    throw new Error(`${label}: нужно минимум ${minCount} файл(а).`);
  }

  const unique = [...new Set(list)];

  for (const file of unique) {
    if (typeof file !== "string" || !path.isAbsolute(file)) {
      throw new Error(`${label}: путь должен быть абсолютным.`);
    }

    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      throw new Error(`${label}: файл не найден: ${file}`);
    }

    if (!stat.isFile()) {
      throw new Error(`${label}: это не файл: ${file}`);
    }

    if (stat.size === 0) {
      throw new Error(`${label}: файл пустой: ${file}`);
    }
  }

  return unique;
}

function checkNumber(value, label, min, max, integer) {
  const number = Number(value);

  if (!Number.isFinite(number) || number < min || number > max) {
    throw new Error(`${label}: допустимо от ${min} до ${max}.`);
  }

  if (integer && !Number.isInteger(number)) {
    throw new Error(`${label}: нужно целое число.`);
  }

  return number;
}

function validateRequest(request) {
  if (!request || typeof request !== "object") {
    throw new Error("Некорректный запрос.");
  }

  const clean = checkFiles(request.clean, "Clean-дампы", 2);
  const cheat = checkFiles(request.cheat, "Cheat-дампы", 1);

  const overlap = clean.find((file) => cheat.includes(file));
  if (overlap) {
    throw new Error(`Один и тот же файл указан и как clean, и как cheat: ${overlap}`);
  }

  const config = {
    clean,
    cheat,
    minLen: checkNumber(request.minLen, "Минимальная длина", 1, 256, true),
    minEntropy: checkNumber(request.minEntropy, "Минимальная энтропия", 0, 8, false),
    maxVowel: checkNumber(request.maxVowel, "Максимальная доля гласных", 0, 1, false),
    minUnique: checkNumber(request.minUnique, "Минимум уникальных символов", 1, 64, true),
    minHits: null,
    jobs: null,
  };

  if (request.jobs !== null && request.jobs !== undefined && request.jobs !== "") {
    config.jobs = checkNumber(request.jobs, "Число процессов", 1, 32, true);
  }

  if (request.minHits !== null && request.minHits !== undefined && request.minHits !== "") {
    config.minHits = checkNumber(
      request.minHits,
      "Минимум cheat-дампов",
      1,
      cheat.length,
      true
    );
  }

  return config;
}

function buildArgs(config, enginePath) {
  const args = [
    "-u",
    enginePath,
    "--json",
    "--clean", ...config.clean,
    "--cheat", ...config.cheat,
    "--min-len", String(config.minLen),
    "--min-entropy", String(config.minEntropy),
    "--max-vowel-ratio", String(config.maxVowel),
    "--min-unique", String(config.minUnique),
  ];

  if (config.minHits !== null) {
    args.push("--min-cheat-hits", String(config.minHits));
  }

  if (config.jobs !== null) {
    args.push("--jobs", String(config.jobs));
  }

  return args;
}

/* ==================================
   PROCESS
   ================================== */

function lineSplitter(onLine) {
  let buffer = "";

  return {
    push(chunk) {
      buffer += chunk;

      let index;
      while ((index = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, index).replace(/\r$/, "");
        buffer = buffer.slice(index + 1);
        if (line) onLine(line);
      }
    },
    flush() {
      const line = buffer.replace(/\r$/, "");
      buffer = "";
      if (line) onLine(line);
    },
  };
}

/*
 * Отмена: движок запускает рабочие процессы, поэтому мало убить один.
 * Windows: taskkill /T убивает всё дерево. Остальные системы: сначала
 * SIGTERM (движок сам завершит рабочих), через 3 секунды SIGKILL.
 */
function killTree(child) {
  if (process.platform === "win32") {
    try {
      spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
    } catch {
      child.kill();
    }
    return;
  }

  child.kill("SIGTERM");

  const timer = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }, 3000);

  timer.unref();
}

/**
 * @param {object} config результат validateRequest
 * @param {{enginePath: string, onEvent: Function, onLog: Function}} options
 * @returns {{cancel: Function, done: Promise<{code: number|null, cancelled: boolean, gotResult: boolean, stderrTail: string[]}>}}
 */
function startEngine(config, { enginePath, onEvent, onLog }) {
  const python = findPython();

  if (!python) {
    throw new Error(
      `Python 3.${MIN_PYTHON_MINOR}+ не найден. Установите его с python.org ` +
      "(с галочкой «Add to PATH») или задайте путь в переменной CHEAT_DIFF_PYTHON."
    );
  }

  const child = spawn(python.command, buildArgs(config, enginePath), {
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ...UTF8_ENV },
  });

  let cancelled = false;
  let gotResult = false;
  let gotError = false;
  const stderrTail = [];

  const stdout = lineSplitter((line) => {
    let event;

    try {
      event = JSON.parse(line);
    } catch {
      onLog(line);
      return;
    }

    if (event && typeof event === "object" && typeof event.type === "string") {
      if (event.type === "result") gotResult = true;
      if (event.type === "error") gotError = true;
      onEvent(event);
    }
  });

  const stderr = lineSplitter((line) => {
    stderrTail.push(line);
    if (stderrTail.length > 20) stderrTail.shift();
    onLog(line);
  });

  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => stdout.push(chunk));
  child.stderr.on("data", (chunk) => stderr.push(chunk));

  const done = new Promise((resolve, reject) => {
    child.once("error", reject);

    child.once("close", (code) => {
      stdout.flush();
      stderr.flush();
      resolve({ code, cancelled, gotResult, gotError, stderrTail });
    });
  });

  return {
    pid: child.pid,
    cancel() {
      cancelled = true;
      killTree(child);
    },
    done,
  };
}

module.exports = { findPython, validateRequest, buildArgs, startEngine };
