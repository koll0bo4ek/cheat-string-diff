"use strict";

const { contextBridge, ipcRenderer, webUtils } = require("electron");

/*
 * Единственная точка связи между интерфейсом и системой.
 * Страница получает только эти методы, а не весь Node.js.
 */
contextBridge.exposeInMainWorld("api", {
  checkPython: () => ipcRenderer.invoke("python:check"),
  pickFiles: (group) => ipcRenderer.invoke("dialog:pick", group),
  pathForFile: (file) => webUtils.getPathForFile(file),
  runEngine: (request) => ipcRenderer.invoke("engine:run", request),
  cancelEngine: () => ipcRenderer.invoke("engine:cancel"),
  exportItems: (payload) => ipcRenderer.invoke("export:save", payload),

  onEngineEvent: (callback) => {
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on("engine:event", handler);
    return () => ipcRenderer.removeListener("engine:event", handler);
  },
});
