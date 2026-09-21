"use strict";

/*
 * Только для автотеста (test/e2e_electron.py). Не входит в сборку.
 *
 * Системные диалоги выбора файла нельзя нажать автоматически,
 * поэтому подменяем их ответы, а дальше запускаем настоящий main.js.
 */

const { dialog } = require("electron");
const path = require("path");

const dir = process.env.E2E_DIR;

dialog.showOpenDialog = async (_window, options) => ({
  canceled: false,
  filePaths: /clean/i.test(options.title)
    ? ["clean1.dmp", "clean2.dmp"].map((name) => path.join(dir, name))
    : ["cheat1.dmp", "cheat2.dmp", "cheat3.dmp"].map((name) => path.join(dir, name)),
});

dialog.showSaveDialog = async (_window, options) => ({
  canceled: false,
  filePath: path.join(dir, options.defaultPath),
});

require("../main.js");
