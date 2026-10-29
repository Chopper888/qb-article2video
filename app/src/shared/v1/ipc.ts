// 1.0 界面用的 IPC 通道（与旧通道并存，旧通道和旧能力不动）
export const IPC_V1 = {
  getAppInfo: 'v1:get-app-info',
  getPrefs: 'v1:get-prefs',
  updatePrefs: 'v1:update-prefs',
  getKeyStatus: 'v1:get-key-status',
  setKey: 'v1:set-key',
  checkKeys: 'v1:check-keys',
  openExternal: 'v1:open-external',
  openFolder: 'v1:open-folder',
  chooseFolder: 'v1:choose-folder',
  importArticle: 'v1:import',
  estimate: 'v1:estimate',
  run: 'v1:run',
  resume: 'v1:resume',
  stop: 'v1:stop',
  readReport: 'v1:read-report',
  sessionEvent: 'v1:session-event',
  sessionEnd: 'v1:session-end',
  getDemoState: 'v1:get-demo-state',
  setDemoState: 'v1:set-demo-state',
  devScene: 'v1:dev-scene',
  // APP2
  chooseImportFolder: 'v1:choose-import-folder',
  readChapters: 'v1:read-chapters',
  openArticleSource: 'v1:open-article-source',
  listTasks: 'v1:list-tasks',
  listVideos: 'v1:list-videos',
  diskUsage: 'v1:disk-usage',
  cleanupPreview: 'v1:cleanup-preview',
  exportDiagnostics: 'v1:export-diagnostics',
  // APP3
  getRuntimePrep: 'v1:get-runtime-prep',
  retryRuntimePrep: 'v1:retry-runtime-prep',
  runtimePrep: 'v1:runtime-prep',
  cleanupRun: 'v1:cleanup-run',
  listLicenses: 'v1:list-licenses',
  openLicense: 'v1:open-license',
  // APP4
  chooseImportSource: 'v1:choose-import-source',
  importDropped: 'v1:import-dropped',
  articleInfo: 'v1:article-info',
  prefsChanged: 'v1:prefs-changed',
  reloadWindow: 'v1:reload-window',
  openDevTools: 'v1:open-devtools',
  // 1.0.1（R101 J）
  deleteTaskCheck: 'v1:delete-task-check',
  deleteTask: 'v1:delete-task'
} as const

// 成片播放用的协议：a2v-media://local/?p=<绝对路径>，Main 只放行成片保存位置和工作文件夹里的 mp4
export const MEDIA_SCHEME = 'a2v-media'
export function mediaUrl(file: string): string {
  return `${MEDIA_SCHEME}://local/?p=${encodeURIComponent(file)}`
}
