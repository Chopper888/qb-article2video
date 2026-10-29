// 磁盘占用的显示格式（APP2 起任务列表、成片库、磁盘占用都是真实数据，演示数据只留在演示场景里）
export const gb = (bytes: number): string => `${(bytes / 1e9).toFixed(1)} GB`
