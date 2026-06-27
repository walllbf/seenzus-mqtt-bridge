<#
.SYNOPSIS
  打包 seenzus_bridge 集成到项目 dist/（文件夹版 + 压缩包版）。

.DESCRIPTION
  把最新的 custom_components/seenzus_bridge/（排除 __pycache__）输出为：
    dist/seenzus_bridge/       —— 解压即用的集成文件夹
    dist/seenzus_bridge.zip    —— 压缩包，根目录为 seenzus_bridge/（可直接丢进 HA custom_components/）
  dist/ 已被 .gitignore 忽略，不进版本库。

.EXAMPLE
  pwsh ./tools/build.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# 仓库根 = 本脚本(tools/)的上一级；多路兜底取脚本自身路径
$scriptPath = $PSCommandPath
if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Path }
if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Definition }
if (-not $scriptPath) { throw "无法确定脚本路径，请用 -File 方式运行" }
$proj   = Split-Path -Parent (Split-Path -Parent $scriptPath)
$src    = Join-Path $proj 'custom_components\seenzus_bridge'
$dist   = Join-Path $proj 'dist'
$folder = Join-Path $dist 'seenzus_bridge'
$zip    = Join-Path $dist 'seenzus_bridge.zip'

if (-not (Test-Path $src)) { throw "找不到源目录: $src" }

New-Item -ItemType Directory -Path $folder -Force | Out-Null

# robocopy /MIR 镜像（删除 dist 里多余文件），排除 __pycache__
robocopy $src $folder /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
# robocopy 退出码 0-7 均为成功，>=8 才是失败
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败 (exit $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path $folder -DestinationPath $zip -Force

$ver  = (Get-Content (Join-Path $folder 'manifest.json') -Raw | ConvertFrom-Json).version
$n    = (Get-ChildItem $folder -Recurse -File).Count
$size = [math]::Round((Get-Item $zip).Length / 1KB, 1)

Write-Host "打包完成 (manifest $ver, $n 个文件)"
Write-Host "  文件夹: $folder"
Write-Host "  压缩包: $zip ($size KB)"
