param(
  [Parameter(Mandatory)][string]$EngineSource,
  [Parameter(Mandatory)][string]$RuntimeSource,
  [Parameter(Mandatory)][string]$SampleSource,
  [Parameter(Mandatory)][string]$DeliveryRoot,
  [string]$BuildPython = 'python',
  [switch]$Fresh,
  [switch]$Rebuild
)
$ErrorActionPreference = 'Stop'
$extra = @()
if ($Fresh) { $extra += '--fresh' }
if ($Rebuild) { $extra += '--rebuild' }
& $BuildPython -I -B "$PSScriptRoot/runtime_pack.py" build --engine $EngineSource --runtime $RuntimeSource --samples $SampleSource --delivery $DeliveryRoot @extra
exit $LASTEXITCODE
