"use strict";

const { app, BrowserWindow, dialog, ipcMain, nativeTheme, session } = require("electron");
const fs = require("fs");
const path = require("path");

const { findPython, validateRequest, startEngine } = require("./lib/engine");
const { buildYaraRule } = require("./lib/yara");

/* ==================================
   PATHS
   ================================== */

const enginePath = app.isPackaged
  ? path.join(process.resourcesPath, "engine", "cheat_string_diff.py")
  : path.join(__dirname, "engine", "cheat_string_diff.py");

const MAX_EXPORT_ITEMS = 500000;

let win = null;
let activeRun = null;

/* ==================================
   WINDOW
   ==================================
   Интерфейс изолирован от Node.js: у страницы нет прямого доступа
   к файлам и процессам, только к методам из preload.js.
   ================================== */

function createWindow() {
  nativeTheme.themeSource = "dark";

  win = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 980,
    minHeight: 640,
    title: "Cheat String Diff",
    backgroundColor: "#12141f",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.removeMenu();

  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event) => event.preventDefault());

  win.on("closed", () => {
    win = null;
  });

  win.loadFile(path.join(__dirname, "renderer", "index.html"));
}

function send(event) {
  if (win && !win.isDestroyed()) {
    win.webContents.send("engine:event", event);
  }
}

function assertTrusted(event) {
  if (!win || event.sender !== win.webContents) {
    throw new Error("Запрос из неизвестного источника.");
  }
}

/* ==================================
   IPC
   ================================== */

function setupIPC() {
  ipcMain.handle("python:check", (event) => {
    assertTrusted(event);

    const python = findPython();
    return python
      ? { ok: true, version: python.version, command: python.command }
      : { ok: false };
  });

  ipcMain.handle("dialog:pick", async (event, group) => {
    assertTrusted(event);

    const result = await dialog.showOpenDialog(win, {
      title: group === "clean" ? "Выберите clean-дампы" : "Выберите cheat-дампы",
      properties: ["openFile", "multiSelections"],
      filters: [
        { name: "Все файлы", extensions: ["*"] },
        { name: "Дампы памяти", extensions: ["dmp", "mdmp", "raw", "bin", "mem", "vmem", "core"] },
      ],
    });

    return result.canceled ? [] : result.filePaths;
  });

  ipcMain.handle("engine:run", (event, request) => {
    assertTrusted(event);

    if (activeRun) {
      return { ok: false, error: "Анализ уже запущен." };
    }

    let run;

    try {
      const config = validateRequest(request);

      run = startEngine(config, {
        enginePath,
        onEvent: send,
        onLog: (line) => send({ type: "log", line }),
      });
    } catch (error) {
      return { ok: false, error: error.message };
    }

    activeRun = run;

    run.done
      .then((info) => {
        if (info.cancelled) {
          send({ type: "finished", cancelled: true });
        } else if (info.gotError) {
          // Движок уже сам прислал понятное сообщение об ошибке.
        } else if (info.code !== 0 || !info.gotResult) {
          const tail = info.stderrTail.slice(-4).join("\n");
          send({
            type: "error",
            message: `Движок завершился с ошибкой (код ${info.code}).${tail ? `\n${tail}` : ""}`,
          });
        } else {
          send({ type: "finished", cancelled: false });
        }
      })
      .catch((error) => {
        send({ type: "error", message: `Не удалось запустить движок: ${error.message}` });
      })
      .finally(() => {
        if (activeRun === run) activeRun = null;
      });

    return { ok: true };
  });

  ipcMain.handle("engine:cancel", (event) => {
    assertTrusted(event);
    if (activeRun) activeRun.cancel();
    return { ok: true };
  });

  ipcMain.handle("export:save", async (event, payload) => {
    assertTrusted(event);

    try {
      if (!payload || !Array.isArray(payload.items) || payload.items.length === 0) {
        return { ok: false, error: "Нет строк для экспорта." };
      }

      if (payload.items.length > MAX_EXPORT_ITEMS) {
        return { ok: false, error: "Слишком много строк для экспорта." };
      }

      const items = payload.items.filter(
        (item) => item && typeof item.value === "string" && item.value.length > 0
      );

      let content;
      let defaultName;
      let filters;
      let skipped = 0;

      if (payload.kind === "yara") {
        const rule = buildYaraRule(items, {
          ruleName: payload.ruleName,
          minMatches: payload.minMatches,
        });

        content = rule.text;
        skipped = rule.skipped;
        defaultName = `${rule.ruleName}.yar`;
        filters = [{ name: "YARA-правило", extensions: ["yar", "yara"] }];
      } else if (payload.kind === "txt") {
        const values = [...new Set(items.map((item) => item.value))].sort();

        content = `${values.join("\n")}\n`;
        defaultName = "candidates.txt";
        filters = [{ name: "Текст", extensions: ["txt"] }];
      } else {
        return { ok: false, error: "Неизвестный формат экспорта." };
      }

      const target = await dialog.showSaveDialog(win, {
        title: "Сохранить результат",
        defaultPath: defaultName,
        filters,
      });

      if (target.canceled || !target.filePath) {
        return { ok: false, cancelled: true };
      }

      fs.writeFileSync(target.filePath, content, { encoding: "utf8" });

      return { ok: true, path: target.filePath, skipped };
    } catch (error) {
      return { ok: false, error: error.message };
    }
  });
}

/* ==================================
   APPLICATION
   ================================== */

function startApp() {
  setupIPC();

  app.whenReady().then(() => {
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) =>
      callback(false)
    );

    createWindow();
  });

  app.on("before-quit", () => {
    if (activeRun) activeRun.cancel();
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}

startApp();
