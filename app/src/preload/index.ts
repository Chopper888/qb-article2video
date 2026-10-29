import { contextBridge, ipcRenderer, webUtils } from 'electron'
import { IPC } from '../shared/ipc'
import type { AppApi, AppSettings, AppState, CreateTaskInput } from '../shared/types'
import { IPC_V1, mediaUrl } from '../shared/v1/ipc'
import type { RuntimePrep, SessionEnd, SessionEvent, V1Api, V1Prefs } from '../shared/v1/types'

const api: AppApi = {
  getEnvironmentReadiness: () => ipcRenderer.invoke(IPC.getEnvironmentReadiness),
  getState: () => ipcRenderer.invoke(IPC.getState),
  createTask: (input: CreateTaskInput) => ipcRenderer.invoke(IPC.createTask, input),
  createSampleTask: (input) => ipcRenderer.invoke(IPC.createSampleTask, input),
  startTask: (taskId: string) => ipcRenderer.invoke(IPC.startTask, taskId),
  stopTask: (taskId: string) => ipcRenderer.invoke(IPC.stopTask, taskId),
  retryTask: (taskId: string) => ipcRenderer.invoke(IPC.retryTask, taskId),
  updateSettings: (patch: Partial<AppSettings>) => ipcRenderer.invoke(IPC.updateSettings, patch),
  getDiagnostics: () => ipcRenderer.invoke(IPC.getDiagnostics),
  onStateChanged: (listener: (state: AppState) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, state: AppState): void => listener(state)
    ipcRenderer.on(IPC.stateChanged, handler)
    return () => ipcRenderer.removeListener(IPC.stateChanged, handler)
  }
}

contextBridge.exposeInMainWorld('appApi', api)

// 1.0 界面：固定的一组调用，不提供任意文件、命令或网址接口
const subscribe = <T>(channel: string) => (listener: (payload: T) => void): (() => void) => {
  const handler = (_event: Electron.IpcRendererEvent, payload: T): void => listener(payload)
  ipcRenderer.on(channel, handler)
  return () => ipcRenderer.removeListener(channel, handler)
}

const v1: V1Api = {
  getAppInfo: () => ipcRenderer.invoke(IPC_V1.getAppInfo),
  getPrefs: () => ipcRenderer.invoke(IPC_V1.getPrefs),
  updatePrefs: patch => ipcRenderer.invoke(IPC_V1.updatePrefs, patch),
  getKeyStatus: () => ipcRenderer.invoke(IPC_V1.getKeyStatus),
  setKey: (id, value) => ipcRenderer.invoke(IPC_V1.setKey, id, value),
  checkKeys: () => ipcRenderer.invoke(IPC_V1.checkKeys),
  openExternal: link => ipcRenderer.invoke(IPC_V1.openExternal, link),
  openFolder: target => ipcRenderer.invoke(IPC_V1.openFolder, target),
  chooseFolder: target => ipcRenderer.invoke(IPC_V1.chooseFolder, target),
  importArticle: input => ipcRenderer.invoke(IPC_V1.importArticle, input),
  estimate: article => ipcRenderer.invoke(IPC_V1.estimate, article),
  run: (article, maxCostCny) => ipcRenderer.invoke(IPC_V1.run, article, maxCostCny),
  resume: (article, maxCostCny) => ipcRenderer.invoke(IPC_V1.resume, article, maxCostCny),
  stop: sessionId => ipcRenderer.invoke(IPC_V1.stop, sessionId),
  readReport: article => ipcRenderer.invoke(IPC_V1.readReport, article),
  onSessionEvent: subscribe<SessionEvent>(IPC_V1.sessionEvent),
  onSessionEnd: subscribe<SessionEnd>(IPC_V1.sessionEnd),
  getDemoState: () => ipcRenderer.invoke(IPC_V1.getDemoState),
  setDemoState: patch => ipcRenderer.invoke(IPC_V1.setDemoState, patch),
  onDevScene: subscribe<string>(IPC_V1.devScene),
  chooseImportFolder: () => ipcRenderer.invoke(IPC_V1.chooseImportFolder),
  // 拖进窗口的文件 → 本机路径（Electron 32 起 File.path 不再提供）
  pathForFile: file => webUtils.getPathForFile(file),
  readChapters: article => ipcRenderer.invoke(IPC_V1.readChapters, article),
  openArticleSource: input => ipcRenderer.invoke(IPC_V1.openArticleSource, input),
  listTasks: () => ipcRenderer.invoke(IPC_V1.listTasks),
  listVideos: () => ipcRenderer.invoke(IPC_V1.listVideos),
  diskUsage: () => ipcRenderer.invoke(IPC_V1.diskUsage),
  cleanupPreview: () => ipcRenderer.invoke(IPC_V1.cleanupPreview),
  exportDiagnostics: () => ipcRenderer.invoke(IPC_V1.exportDiagnostics),
  getRuntimePrep: () => ipcRenderer.invoke(IPC_V1.getRuntimePrep),
  retryRuntimePrep: () => ipcRenderer.invoke(IPC_V1.retryRuntimePrep),
  onRuntimePrep: subscribe<RuntimePrep>(IPC_V1.runtimePrep),
  cleanupRun: articles => ipcRenderer.invoke(IPC_V1.cleanupRun, articles),
  listLicenses: () => ipcRenderer.invoke(IPC_V1.listLicenses),
  openLicense: id => ipcRenderer.invoke(IPC_V1.openLicense, id),
  chooseImportSource: kind => ipcRenderer.invoke(IPC_V1.chooseImportSource, kind),
  importDropped: paths => ipcRenderer.invoke(IPC_V1.importDropped, paths),
  articleInfo: article => ipcRenderer.invoke(IPC_V1.articleInfo, article),
  onPrefsChanged: subscribe<V1Prefs>(IPC_V1.prefsChanged),
  reloadWindow: () => ipcRenderer.invoke(IPC_V1.reloadWindow),
  openDevTools: () => ipcRenderer.invoke(IPC_V1.openDevTools),
  deleteTaskCheck: article => ipcRenderer.invoke(IPC_V1.deleteTaskCheck, article),
  deleteTask: article => ipcRenderer.invoke(IPC_V1.deleteTask, article),
  mediaUrl
}

contextBridge.exposeInMainWorld('a2v', v1)
