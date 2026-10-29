# APP2：Windows 上一条命令构建 v5.7 Runtime Pack 并打出安装包（给 APP2W 用；Mac 上不运行）。
#
#   ./scripts/packaging/build-installer-v5.7.ps1 -BuildPython <宿主 Python 3.12> -EngineSource <v5.7 引擎 clean 检出>
#       -RuntimeSource <运行时源> -SampleSource <样本源-v5> -DeliveryRoot <主体交付目录> [-AssetMirror <资产目录>] [-Rebuild]
#
# 步骤：
#   1. 准备外部资产（Python、Node、wheel、FFmpeg LGPL shared、字体许可）：APP3 起先从 -AssetMirror
#      （默认仓库下的 assets-mirror 目录）取，按锁定 SHA256 校验一致才用；那里没有才去原地址下载，
#      下载后同样校验并自动复制一份回资产目录；SHA 不一致直接失败。-AssetMirror '' 表示不用资产目录；
#   2. 组装 .cache/runtime-pack-v5.7（引擎白名单 196 个文件 + 引擎目录里的 VERSION、FFmpeg/ffprobe、字体、许可；密钥不进包）；
#   3. 组装审计 verify-assembly（18 项，APP3 新增 engine-version-file）；
#   4. 临时把 src/shared/runtime-baseline.json 换成 v5.7 的五个字段，typecheck + 构建 + electron-builder 打 NSIS，
#      最后无论成败都还原；
#   5. 逐文件核对安装目录里的 Runtime Pack 和 .cache/runtime-pack-v5.7 一致。
# 退出码：0 成功；非 0 为失败的那一步的退出码。
param(
  [Parameter(Mandatory)][string]$EngineSource,
  [Parameter(Mandatory)][string]$RuntimeSource,
  [Parameter(Mandatory)][string]$SampleSource,
  [Parameter(Mandatory)][string]$DeliveryRoot,
  [string]$BuildPython = 'python',
  [string]$AssetMirror = (Join-Path $PSScriptRoot '../../assets-mirror'),
  [switch]$Rebuild
)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '../..')
$pack = Join-Path $PSScriptRoot '../runtime/runtime_pack.py'
$env:A2V_RUNTIME_PROFILE = 'v5.7'
# CLN1W：pytest 会在 scripts 下留下带开发机路径的 .pyc，path-hygiene 会判为污染；
# 组装前清掉，并让构建自身的 Python 子进程不再写字节码。
$previousBytecodeEnv = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'
Get-ChildItem -LiteralPath (Join-Path $repo 'scripts') -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
function Step($name, [scriptblock]$body) {
  Write-Host "== $name"
  & $body
  if ($LASTEXITCODE -ne 0) { Write-Host "失败：$name（退出码 $LASTEXITCODE）"; exit $LASTEXITCODE }
}
try {
  Step '准备资产（先从资产目录取）' { & $BuildPython -I -B -X utf8 $pack fetch-assets --mirror $AssetMirror }
  $extra = @(); if ($Rebuild) { $extra += '--rebuild' }
  Step '组装 Runtime Pack v5.7' { & $BuildPython -I -B $pack build --engine $EngineSource --runtime $RuntimeSource --samples $SampleSource --delivery $DeliveryRoot @extra }
  Step '组装审计' { & $BuildPython -I -B -X utf8 $pack verify-assembly }
  $baseline = Join-Path $repo 'src/shared/runtime-baseline.json'
  $backup = Join-Path $repo '.cache/runtime-baseline.v5.backup.json'
  Copy-Item $baseline $backup -Force
  try {
    $v57 = Get-Content (Join-Path $repo 'src/shared/runtime-baseline-v5.7.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $swap = [ordered]@{ version = $v57.version; engineCommit = $v57.engineCommit; whitelistSha256 = $v57.whitelistSha256;
      longestRelativePath = $v57.longestRelativePath; maxTargetCharacters = $v57.maxTargetCharacters }
    [IO.File]::WriteAllText($baseline, ($swap | ConvertTo-Json) + "`n", (New-Object Text.UTF8Encoding $false))
    Push-Location $repo
    Step '构建 APP' { npm run build }
    Step '打安装包' { npx electron-builder --win nsis --x64 --publish never --config electron-builder-v5.7.yml }
    Step '核对安装目录里的 Runtime Pack' { node scripts/packaging/verify-unpacked.cjs .cache/runtime-pack-v5.7 release/win-unpacked/resources/runtime-pack-v5.7 }
    Pop-Location
  } finally {
    Copy-Item $backup $baseline -Force
  }
  Write-Host '完成：安装包在 release/ 目录'
} finally {
  Remove-Item Env:A2V_RUNTIME_PROFILE -ErrorAction SilentlyContinue
  if ($null -eq $previousBytecodeEnv) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue }
  else { $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeEnv }
}
