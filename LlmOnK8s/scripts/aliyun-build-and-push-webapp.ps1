param(
    [string]$ImageTag = "latest"
)

$ErrorActionPreference = "Stop"

function Require-Env([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing environment variable: $Name"
    }
    return $value
}

function Normalize-Registry([string]$Registry) {
    if ($Registry -match '^crpi-[^.]+\.cn-[^.]+\.aliyuncs\.com$') {
        return $Registry -replace '\.aliyuncs\.com$', '.personal.cr.aliyuncs.com'
    }
    return $Registry
}

function Normalize-Repo([string]$Repo, [string]$Namespace) {
    $r = $Repo.TrimStart('/')
    if ([string]::IsNullOrWhiteSpace($Namespace)) {
        return $r
    }

    $ns = $Namespace.Trim('/').Trim()
    if ($r -eq $ns -or $r.StartsWith("$ns/") -or $r.Contains('/')) {
        return $r
    }
    return "$ns/$r"
}

$aliyunRegion = Require-Env "ALIYUN_REGION"
$registry = Normalize-Registry (Require-Env "ALIYUN_REGISTRY")
$webRepo = Require-Env "ACR_WEBAPP_REPO"
$registryUser = Require-Env "ALIYUN_REGISTRY_USERNAME"
$registryPass = Require-Env "ALIYUN_REGISTRY_PASSWORD"
$namespace = [Environment]::GetEnvironmentVariable("ALIYUN_NAMESPACE")

if (-not [string]::IsNullOrWhiteSpace($env:IMAGE_TAG)) {
    $ImageTag = $env:IMAGE_TAG
}

$normalizedRepo = Normalize-Repo $webRepo $namespace
$webImage = "$registry/${normalizedRepo}:$ImageTag"

$env:ALIYUN_REGISTRY = $registry
$env:IMAGE_TAG = $ImageTag
$env:WEBAPP_IMAGE = $webImage

Write-Host "======================================"
Write-Host "Build and Push WebApp image only"
Write-Host "======================================"
Write-Host "Region: $aliyunRegion"
Write-Host "Registry: $registry"
Write-Host "WebApp Image: $webImage"
Write-Host "======================================"

Push-Location $PSScriptRoot/..
try {
    $registryPass | docker login --username $registryUser --password-stdin $registry
    docker build -f webapp/Dockerfile -t $webImage .
    docker push $webImage
}
finally {
    Pop-Location
}

Write-Host "Done: $webImage"
