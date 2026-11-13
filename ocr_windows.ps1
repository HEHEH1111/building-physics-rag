# Windows built-in OCR backend (no third-party dependency).
# Keep this file pure ASCII: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File ocr_windows.ps1 -ImagePath <png> -OutputPath <txt> [-Language zh-Hans-CN|en-US]
# Writes UTF-8 (no BOM) text to OutputPath; stdout carries status only.
param(
    [Parameter(Mandatory=$true)][string]$ImagePath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [string]$Language = 'zh-Hans-CN'
)
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1)

if ($null -eq $asTaskGeneric) { throw 'Cannot locate AsTask(IAsyncOperation<T>)' }

function Await-WinRt($operation, $resultType) {
    $method = $asTaskGeneric.MakeGenericMethod($resultType)
    $task = $method.Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

[void][Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]
[void][Windows.Media.Ocr.OcrResult,Windows.Media.Ocr,ContentType=WindowsRuntime]
[void][Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]

$full = [System.IO.Path]::GetFullPath($ImagePath)
if (-not (Test-Path -LiteralPath $full)) { throw "Image not found: $full" }

$file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language -ArgumentList $Language
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} catch { }
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw 'No OCR engine available for requested language' }

$result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$text = [string]$result.Text
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath), $text, $utf8)
Write-Output ("OCR_OK chars=" + $text.Length)
