param(
    [string]$GraphDbUrl = "http://localhost:7200",
    [string]$Repository = "regulation"
)

$ErrorActionPreference = "Stop"
$baseUrl = $GraphDbUrl.TrimEnd("/")
$configPath = Join-Path $PSScriptRoot "..\infra\graphdb\repository-config.ttl"
$configPath = (Resolve-Path -LiteralPath $configPath).Path

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $repositories = Invoke-RestMethod -Uri "$baseUrl/rest/repositories" -Method Get
        $ready = $true
        break
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    throw "GraphDB did not become ready at $baseUrl."
}

$exists = $repositories | Where-Object {
    $_.id -eq $Repository -or $_.repositoryID -eq $Repository
}
if ($exists) {
    Write-Output "Repository '$Repository' already exists."
    exit 0
}

Add-Type -AssemblyName System.Net.Http

$client = [System.Net.Http.HttpClient]::new()
$stream = [System.IO.File]::OpenRead($configPath)
$form = [System.Net.Http.MultipartFormDataContent]::new()
$fileContent = [System.Net.Http.StreamContent]::new($stream)
$fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("text/turtle")
$form.Add($fileContent, "config", [System.IO.Path]::GetFileName($configPath))

try {
    $response = $client.PostAsync(
        "$baseUrl/rest/repositories",
        $form
    ).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        throw "GraphDB repository creation failed ($([int]$response.StatusCode)): $body"
    }
}
finally {
    $form.Dispose()
    $stream.Dispose()
    $client.Dispose()
}

Write-Output "Repository '$Repository' created."
