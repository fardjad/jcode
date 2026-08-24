# Install a patched jcode release from fardjad/jcode without changing PATH.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Version,

    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "jcode\bin")
)

$ErrorActionPreference = "Stop"
$repo = "fardjad/jcode"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

$architecture = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { "x86_64" }
    "Arm64" { "aarch64" }
    default { throw "Unsupported Windows architecture: $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" }
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    try {
        $tag = (Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
            -Headers @{ "User-Agent" = "jcode-installer" }).tag_name
    }
    catch {
        throw "Could not determine the latest jcode release: $($_.Exception.Message)"
    }
}
else {
    $tag = "v$($Version.TrimStart('v'))"
}

$asset = "jcode-windows-$architecture.zip"
$baseUrl = "https://github.com/$repo/releases/download/$tag"
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("jcode-install-" + [guid]::NewGuid())
$archive = Join-Path $tempDir $asset
$checksumFile = Join-Path $tempDir "SHA256SUMS"

try {
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    Write-Host "Installing jcode $tag for Windows $architecture..."
    Invoke-WebRequest -Uri "$baseUrl/$asset" -OutFile $archive
    Invoke-WebRequest -Uri "$baseUrl/SHA256SUMS" -OutFile $checksumFile

    $checksumLine = Get-Content -LiteralPath $checksumFile |
        Where-Object { $_ -match ("\s{2}" + [regex]::Escape($asset) + "$") } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($checksumLine)) {
        throw "No checksum found for $asset in $tag."
    }
    $expected = ($checksumLine -split '\s+')[0].ToLowerInvariant()
    if ((Get-Sha256 $archive) -ne $expected) {
        throw "Checksum verification failed for $asset."
    }

    Expand-Archive -LiteralPath $archive -DestinationPath $tempDir -Force
    $source = Join-Path $tempDir "jcode-windows-$architecture.exe"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Release archive did not contain the expected jcode binary."
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $source -Destination (Join-Path $InstallDir "jcode.exe") -Force
    Write-Host "Installed jcode $tag to $(Join-Path $InstallDir 'jcode.exe')"
}
finally {
    if (Test-Path -LiteralPath $tempDir) {
        Remove-Item -LiteralPath $tempDir -Recurse -Force
    }
}

$pathEntries = $env:Path -split ';' | ForEach-Object { $_.TrimEnd('\') }
if ($pathEntries -notcontains $InstallDir.TrimEnd('\')) {
    Write-Host ""
    Write-Host "To use jcode in a new PowerShell session, run:"
    Write-Host "  `$env:Path = '$InstallDir;' + `$env:Path"
    Write-Host ""
    Write-Host "To add it permanently for your user account, run:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', '$InstallDir;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
}
