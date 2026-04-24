$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$RenderDir = Join-Path $RootDir "rendered"
$K8sDir = Join-Path $RootDir "k8s"
$ConfigDir = Join-Path $RootDir "config"

New-Item -ItemType Directory -Force -Path $RenderDir | Out-Null

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

function Render-Template([string]$InputFile, [string]$OutputFile) {
    $text = Get-Content -Path $InputFile -Raw -Encoding UTF8
    $result = [System.Text.RegularExpressions.Regex]::Replace($text, '\$\{([A-Za-z_][A-Za-z0-9_]*)\}', {
        param($m)
        $name = $m.Groups[1].Value
        $val = [Environment]::GetEnvironmentVariable($name)
        if ($null -eq $val) { return "" }
        return $val
    })
    [System.IO.File]::WriteAllText($OutputFile, $result, (New-Object System.Text.UTF8Encoding($false)))
}

function Wait-WebappReady([int]$TimeoutSeconds = 360) {
    $elapsed = 0
    while ($elapsed -lt $TimeoutSeconds) {
        $available = (kubectl get deploy qwen-webapp -n platform -o jsonpath='{.status.availableReplicas}' 2>$null)
        if ($available -match '^[1-9][0-9]*$') { return $true }
        Start-Sleep -Seconds 5
        $elapsed += 5
    }
    return $false
}

function Wait-ExternalAddress([int]$TimeoutSeconds = 900) {
    $elapsed = 0
    while ($elapsed -lt $TimeoutSeconds) {
        $ip = (kubectl get svc qwen-webapp -n platform -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null)
        if (-not [string]::IsNullOrWhiteSpace($ip)) { return $ip }

        $host = (kubectl get svc qwen-webapp -n platform -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>$null)
        if (-not [string]::IsNullOrWhiteSpace($host)) { return $host }

        Start-Sleep -Seconds 10
        $elapsed += 10
    }
    return ""
}

$registry = Normalize-Registry (Require-Env "ALIYUN_REGISTRY")
$repo = Require-Env "ACR_WEBAPP_REPO"
$imageTag = Require-Env "IMAGE_TAG"
$tenantAToken = Require-Env "TENANT_A_TOKEN"
$tenantBToken = Require-Env "TENANT_B_TOKEN"

$vllmApiUrl = Require-Env "VLLM_API_URL"
$vllmApiToken = Require-Env "VLLM_API_TOKEN"
$vllmModelName = Require-Env "VLLM_MODEL_NAME"

$namespace = [Environment]::GetEnvironmentVariable("ALIYUN_NAMESPACE")
$registryUser = [Environment]::GetEnvironmentVariable("ALIYUN_REGISTRY_USERNAME")
$registryPass = [Environment]::GetEnvironmentVariable("ALIYUN_REGISTRY_PASSWORD")

$normalizedRepo = Normalize-Repo $repo $namespace
$webImage = "$registry/${normalizedRepo}:$imageTag"

$env:ALIYUN_REGISTRY = $registry
$env:WEBAPP_IMAGE = $webImage
$env:TENANT_A_TOKEN = $tenantAToken
$env:TENANT_B_TOKEN = $tenantBToken
$env:VLLM_API_URL = $vllmApiUrl
$env:VLLM_API_TOKEN = $vllmApiToken
$env:VLLM_MODEL_NAME = $vllmModelName

Write-Host "======================================"
Write-Host "Deploy WebApp with API mode only"
Write-Host "======================================"
Write-Host "WebApp Image: $webImage"
Write-Host "API URL: $vllmApiUrl"
Write-Host "Model: $vllmModelName"
Write-Host "======================================"

Push-Location $RootDir
try {
    kubectl apply -f (Join-Path $K8sDir "namespaces.yaml")

    $renderedTenantsFile = Join-Path $RenderDir "tenants.json"
    Render-Template (Join-Path $ConfigDir "tenants.template.json") $renderedTenantsFile
    kubectl -n platform create secret generic tenant-routing-config --from-file="tenants.json=$renderedTenantsFile" --dry-run=client -o yaml | kubectl apply -f -

    if (-not [string]::IsNullOrWhiteSpace($registryUser) -and -not [string]::IsNullOrWhiteSpace($registryPass)) {
        kubectl -n platform create secret docker-registry aliyun-registry-secret --docker-server=$registry --docker-username=$registryUser --docker-password=$registryPass --dry-run=client -o yaml | kubectl apply -f -
    }

    Render-Template (Join-Path $K8sDir "platform/webapp.yaml") (Join-Path $RenderDir "webapp.yaml")
    kubectl apply -f (Join-Path $RenderDir "webapp.yaml")

    if (-not (Wait-WebappReady)) {
        kubectl get pods -n platform -o wide
        kubectl describe deploy qwen-webapp -n platform
        throw "WebApp deployment is not ready in expected time"
    }

    $externalAddr = Wait-ExternalAddress
    if ([string]::IsNullOrWhiteSpace($externalAddr)) {
        kubectl get svc -n platform qwen-webapp -o wide
        throw "Cannot get external address for qwen-webapp"
    }

    Write-Host "Deployment completed"
    Write-Host "Public URL: http://$externalAddr/index.html"
    Write-Host "Health URL: http://$externalAddr/api/health"
}
finally {
    Pop-Location
}
