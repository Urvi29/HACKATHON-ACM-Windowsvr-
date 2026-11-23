# Windows equivalent of env.sh: keeps all libs/models inside the project, no venv.
# Usage:  . .\env.ps1     (dot-source it so the variables stay in your session)
$Root = $PSScriptRoot

$libs = Join-Path $Root "libs"
$src  = Join-Path $Root "src"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$libs;$src;$env:PYTHONPATH" } else { "$libs;$src" }

# pip --target puts console scripts in libs\bin (and libs\Scripts on some setups)
$base = (Get-Command python).Source
$env:PATH = "$libs\bin;$libs\Scripts;" + (Split-Path $base) + ";" + $env:PATH

$env:HF_HOME = Join-Path $Root "models"
$env:TORCH_HOME = Join-Path $Root "models"
$env:SENTENCE_TRANSFORMERS_HOME = Join-Path $Root "models"
$env:PYTHONPYCACHEPREFIX = Join-Path $Root ".pycache"   # bytecode cache stays inside the project (torch import is slow without it)
$env:HF_HUB_OFFLINE = "1"   # models are cached in .\models; no network calls at runtime
# To download models for the first time, run:  $env:HF_HUB_OFFLINE = "0"

Write-Host "ME3 env ready (PYTHONPATH=$libs;$src)"
