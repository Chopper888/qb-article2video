param(
    [string]$BuildPython = '',
    [ValidateSet('frozen', 'assembly-source')][string]$Mode = 'frozen',
    [switch]$Fresh
)
$ErrorActionPreference = 'Stop'
# CLN1W：pytest 留下的 scripts/**/__pycache__（.pyc 含开发机路径）会被 path-hygiene 判为污染。
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot '..') -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not $BuildPython) {
    $BuildPython = Join-Path $PSScriptRoot '../../.cache/runtime-pack-v5/python/python.exe'
}
$extra = @()
$command = 'verify'
if ($Mode -eq 'assembly-source') {
    $command = 'verify-assembly'
    if ($Fresh) { $extra += '--fresh' }
} elseif ($Fresh) {
    throw 'Frozen acceptance always verifies both main and fresh Packs.'
}
& $BuildPython -I -B -X utf8 "$PSScriptRoot/runtime_pack.py" $command @extra
exit $LASTEXITCODE
