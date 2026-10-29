# APP3：补齐资产。在 Windows 上运行一次，把当前档案（v5.7）需要、而资产目录里还没有的文件都补进去，
# 并重写资产目录里的 资产清单.md（文件名、版本、许可、SHA256、原始来源）。
#
#   ./scripts/packaging/sync-asset-mirror.ps1 [-BuildPython <宿主 Python 3.12>] [-AssetMirror <资产目录>]
#
# 每个文件先进 .cache/runtime-downloads（已有且 SHA 一致就直接用），按锁定 SHA256 校验后再复制到资产目录；
# 资产目录里已有的文件不覆盖，SHA 不一致时失败并报出文件名。只下载，不组装、不打包。
param(
  [string]$BuildPython = 'python',
  [string]$AssetMirror = (Join-Path $PSScriptRoot '../../assets-mirror')
)
$ErrorActionPreference = 'Stop'
$pack = Join-Path $PSScriptRoot '../runtime/runtime_pack.py'
$env:A2V_RUNTIME_PROFILE = 'v5.7'
try {
  & $BuildPython -I -B -X utf8 $pack sync-mirror --mirror $AssetMirror
  if ($LASTEXITCODE -ne 0) { Write-Host "失败：补齐资产（退出码 $LASTEXITCODE）"; exit $LASTEXITCODE }
  Write-Host "完成：资产目录 $AssetMirror（清单见 资产清单.md）"
} finally {
  Remove-Item Env:A2V_RUNTIME_PROFILE -ErrorAction SilentlyContinue
}
