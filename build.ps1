[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$SkipLaunch,
    [Alias("NoCashe")]
    [switch]$NoCache,
    [switch]$ForceAll,
    [switch]$Clean,
    [Alias("h", "?")]
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($args.Count -gt 0) {
    Write-Host ("Error: Unknown parameter(s): {0}" -f ($args -join ", ")) -ForegroundColor Red
    exit 1
}

if ($Help) {
    Write-Host "Use .\run.ps1 -Help for the public runner help. Without -SkipLaunch, successful CI runs the MCP smoke check." -ForegroundColor Yellow
    exit 0
}

$script:BuildInterruptMessagePrinted = $false

function Write-BuildInterruptMessage {
    if (-not $script:BuildInterruptMessagePrinted) {
        $script:BuildInterruptMessagePrinted = $true
        Write-Host ""
        Write-Host "Stopped by user (Ctrl+C)." -ForegroundColor Yellow
    }
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptRoot

$CacheDir = Join-Path $ScriptRoot ".ci_cache"
$LogsRootDir = Join-Path $CacheDir "logs"
$LogsBuildDir = Join-Path $LogsRootDir "build"
$LogsRuntimeDir = Join-Path $LogsRootDir "runtime"
$BuildLogFile = Join-Path $LogsBuildDir "build.log"
$RuntimeLaunchLogFile = Join-Path $LogsRuntimeDir "launch.log"
$BuildLogRelPath = ".ci_cache/logs/build/build.log"
$ReportPath = Join-Path $CacheDir "report.json"

$EnforcerDir = Join-Path $ScriptRoot ".enforcer"
$EnforcerLastCheckPath = Join-Path $EnforcerDir "Enforcer_last_check.log"
$EnforcerStatsPath = Join-Path $EnforcerDir "Enforcer_stats.log"

$BuildPyPath = Join-Path $ScriptRoot "build.py"
$RunScriptPath = Join-Path $ScriptRoot "run.ps1"
$AnalyzerSettingsPath = Join-Path $ScriptRoot "PSScriptAnalyzerSettings.psd1"
$VSCodeSettingsPath = Join-Path $ScriptRoot ".vscode/settings.json"
$AgentsPath = Join-Path $ScriptRoot "AGENTS.md"
$CiDocPath = Join-Path $ScriptRoot "docs/ci.md"
$RepoHooksDir = Join-Path $ScriptRoot ".githooks"
$RepoPreCommitHookPath = Join-Path $RepoHooksDir "pre-commit"

if ($ForceAll) {
    $NoCache = $true
}

if ($Clean -and (Test-Path -Path $CacheDir)) {
    Remove-Item -Path $CacheDir -Recurse -Force
}

New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
New-Item -ItemType Directory -Path $LogsRootDir -Force | Out-Null
New-Item -ItemType Directory -Path $LogsBuildDir -Force | Out-Null
New-Item -ItemType Directory -Path $LogsRuntimeDir -Force | Out-Null
New-Item -ItemType Directory -Path $EnforcerDir -Force | Out-Null

$buildLogHeader = @(
    "=== Local CI / build log ===",
    ("started_utc: {0}" -f (Get-Date).ToUniversalTime().ToString("o")),
    ""
)
Set-Content -Path $BuildLogFile -Value $buildLogHeader -Encoding utf8

$script:CiStartTime = Get-Date
$script:CiPhaseEndTime = $null
$script:StageResults = New-Object System.Collections.Generic.List[object]
$script:Issues = New-Object System.Collections.Generic.List[object]
$script:Metrics = @{}

# * Console problem display caps -- keep the terminal readable for agents/humans.
$script:ProblemDisplayLimits = @{
    MaxIssuesPerStage     = 12
    MaxIssuesInSummary    = 30
    MaxMessageLength      = 220
    MaxLogFilesPerStage   = 2
    MaxExcerptLines       = 35
    MaxExcerptChars       = 5000
    MaxExcerptLineLength  = 200
}

function Resolve-RepoRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ($Path.StartsWith($ScriptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Path.Substring($ScriptRoot.Length).TrimStart("\").Replace("\", "/")
    }

    return $Path.Replace("\", "/")
}

function Invoke-GitTextCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $rawOutput = & git -C $ScriptRoot @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    return @{
        exit_code = $exitCode
        text = (($rawOutput | Out-String)).Trim()
    }
}

function Resolve-AbsoluteGitHookPath {
    param(
        [AllowNull()]
        [string]$GitHookPath
    )

    if ([string]::IsNullOrWhiteSpace($GitHookPath)) {
        return $null
    }

    if ([System.IO.Path]::IsPathRooted($GitHookPath)) {
        return [System.IO.Path]::GetFullPath($GitHookPath)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $ScriptRoot $GitHookPath))
}

function Test-IsRepoManagedGitHookPath {
    param(
        [AllowNull()]
        [string]$GitHookPath
    )

    $resolvedHooksPath = Resolve-AbsoluteGitHookPath -GitHookPath $GitHookPath
    if ([string]::IsNullOrWhiteSpace($resolvedHooksPath)) {
        return $false
    }

    return $resolvedHooksPath.TrimEnd("\") -ieq $RepoHooksDir.TrimEnd("\")
}

function Test-IsDefaultGitHookPath {
    param(
        [AllowNull()]
        [string]$GitHookPath
    )

    $resolvedHooksPath = Resolve-AbsoluteGitHookPath -GitHookPath $GitHookPath
    if ([string]::IsNullOrWhiteSpace($resolvedHooksPath)) {
        return $false
    }

    $defaultGitHookDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptRoot ".git/hooks"))
    return $resolvedHooksPath.TrimEnd("\") -ieq $defaultGitHookDir.TrimEnd("\")
}

function Get-ConfiguredGitHookPath {
    $result = Invoke-GitTextCommand -Arguments @("config", "--local", "--get", "core.hooksPath")
    if ($result.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace($result.text)) {
        return $null
    }

    return $result.text
}

function Enable-RepoManagedGitHookPath {
    if (-not (Test-Path -Path $RepoPreCommitHookPath -PathType Leaf)) {
        return @{
            status = "missing"
            hooks_path = $null
            note = "Repo-managed pre-commit hook file is missing."
        }
    }

    if (-not (Test-Path -Path (Join-Path $ScriptRoot ".git"))) {
        return @{
            status = "unavailable"
            hooks_path = $null
            note = "Git metadata is unavailable; repo-managed hooks bootstrap is skipped."
        }
    }

    $configuredHooksPath = Get-ConfiguredGitHookPath
    if ([string]::IsNullOrWhiteSpace($configuredHooksPath)) {
        $setResult = Invoke-GitTextCommand -Arguments @("config", "--local", "core.hooksPath", ".githooks")
        if ($setResult.exit_code -eq 0) {
            return @{
                status = "configured"
                hooks_path = ".githooks"
                note = "Repo-managed git hooks enabled via core.hooksPath=.githooks."
            }
        }

        return @{
            status = "error"
            hooks_path = $null
            note = "Failed to configure core.hooksPath=.githooks automatically."
        }
    }

    if (Test-IsRepoManagedGitHookPath -GitHookPath $configuredHooksPath) {
        return @{
            status = "ready"
            hooks_path = $configuredHooksPath
            note = "Repo-managed git hooks are already enabled."
        }
    }

    if (Test-IsDefaultGitHookPath -GitHookPath $configuredHooksPath) {
        $setResult = Invoke-GitTextCommand -Arguments @("config", "--local", "core.hooksPath", ".githooks")
        if ($setResult.exit_code -eq 0) {
            return @{
                status = "configured"
                hooks_path = ".githooks"
                note = "Repo-managed git hooks replaced the default .git/hooks path."
            }
        }

        return @{
            status = "error"
            hooks_path = $configuredHooksPath
            note = "Failed to replace the default .git/hooks path with core.hooksPath=.githooks."
        }
    }

    return @{
        status = "custom"
        hooks_path = $configuredHooksPath
        note = ("Custom core.hooksPath is set to {0}; repo-managed pre-commit guard is not auto-installed." -f $configuredHooksPath)
    }
}

function Get-GitHeadState {
    $headResult = Invoke-GitTextCommand -Arguments @("rev-parse", "--verify", "HEAD")
    if ($headResult.exit_code -eq 0 -and -not [string]::IsNullOrWhiteSpace($headResult.text)) {
        $refResult = Invoke-GitTextCommand -Arguments @("symbolic-ref", "--quiet", "--short", "HEAD")
        $headRef = if ($refResult.exit_code -eq 0 -and -not [string]::IsNullOrWhiteSpace($refResult.text)) {
            $refResult.text
        } else {
            $null
        }
        return @{
            head_state = "commit"
            head_commit = $headResult.text
            head_ref = $headRef
        }
    }

    $refResult = Invoke-GitTextCommand -Arguments @("symbolic-ref", "--quiet", "--short", "HEAD")
    if ($refResult.exit_code -eq 0 -and -not [string]::IsNullOrWhiteSpace($refResult.text)) {
        return @{
            head_state = "unborn"
            head_commit = $null
            head_ref = $refResult.text
        }
    }

    return @{
        head_state = "unknown"
        head_commit = $null
        head_ref = $null
    }
}

function Get-CurrentCiProfileName {
    if ($Fast) {
        return "fast"
    }

    return "full"
}

$script:GitHooksBootstrap = Enable-RepoManagedGitHookPath
switch ($script:GitHooksBootstrap.status) {
    "configured" {
        Write-Host ("[hooks] {0}" -f $script:GitHooksBootstrap.note) -ForegroundColor DarkGray
    }
    "custom" {
        Write-Host ("[hooks] {0}" -f $script:GitHooksBootstrap.note) -ForegroundColor Yellow
    }
    "error" {
        Write-Host ("[hooks] {0}" -f $script:GitHooksBootstrap.note) -ForegroundColor Yellow
    }
}

function ConvertTo-PlainData {
    param([AllowNull()]$Value)

    if ($null -eq $Value) {
        return $null
    }

    if ($Value -is [System.Collections.IDictionary]) {
        $result = @{}
        foreach ($key in $Value.Keys) {
            $result[$key] = ConvertTo-PlainData -Value $Value[$key]
        }
        return $result
    }

    # * Must run before IEnumerable: PSCustomObject enumerates property values and loses keys.
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $result = @{}
        foreach ($property in $Value.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-PlainData -Value $property.Value
        }
        return $result
    }

    if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $items = New-Object System.Collections.Generic.List[object]
        foreach ($item in $Value) {
            [void]$items.Add((ConvertTo-PlainData -Value $item))
        }
        Write-Output -InputObject ($items.ToArray()) -NoEnumerate
        return
    }

    return $Value
}

function ConvertFrom-JsonCompat {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Json
    )

    $parsed = $Json | ConvertFrom-Json
    return ConvertTo-PlainData -Value $parsed
}

function Get-OverallStatus {
    $hasFail = $false
    $hasWarn = $false
    foreach ($stage in $script:StageResults) {
        $status = Resolve-StageResultProperty -Stage $stage -PropertyName "status"
        if ($status -eq "fail") {
            $hasFail = $true
        } elseif ($status -eq "warn") {
            $hasWarn = $true
        }
    }
    if ($hasFail) {
        return "fail"
    }
    if ($hasWarn) {
        return "warn"
    }
    return "ok"
}

function Add-StageResult {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Result
    )

    $script:StageResults.Add($Result) | Out-Null
    $issues = Resolve-StageResultProperty -Stage $Result -PropertyName "issues"
    if ($null -ne $issues) {
        foreach ($issue in @($issues)) {
            $script:Issues.Add($issue) | Out-Null
        }
    }
    $metrics = Resolve-StageResultProperty -Stage $Result -PropertyName "metrics"
    if ($null -ne $metrics -and ($metrics -is [System.Collections.IDictionary])) {
        foreach ($key in @($metrics.Keys)) {
            $script:Metrics[[string]$key] = $metrics[$key]
        }
    }

    $status = [string](Resolve-StageResultProperty -Stage $Result -PropertyName "status")
    if ($status -in @("fail", "warn")) {
        Write-StageProblem -Stage $Result -Mode Immediate
    }
}

function New-StageResult {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Note = "",
        [int]$DurationMs = 0,
        [hashtable]$Details = @{},
        [object[]]$Issues = @()
    )

    return @{
        name = $Name
        status = $Status
        note = $Note
        duration_ms = $DurationMs
        details = $Details
        issues = @($Issues)
    }
}

function Add-BuildLogSection {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [Parameter(Mandatory = $true)][string]$ToolName,
        [Parameter(Mandatory = $true)][string]$Body
    )

    Add-Content -Path $BuildLogFile -Value ("`n=== {0}/{1} ===" -f $StageName, $ToolName)
    Add-Content -Path $BuildLogFile -Value $Body
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [Parameter(Mandatory = $true)][string]$ToolName,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    $startedAt = Get-Date
    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    $text = (($output | Out-String)).Trim()
    $durationMs = [int]((Get-Date) - $startedAt).TotalMilliseconds
    $body = @(
        ("command: {0} {1}" -f $FilePath, ($Arguments -join " ")),
        ("exit_code: {0}" -f $exitCode),
        ("duration_ms: {0}" -f $durationMs),
        "",
        $text
    ) -join [Environment]::NewLine
    Add-BuildLogSection -StageName $StageName -ToolName $ToolName -Body $body
    $logRel = Resolve-RepoRelativePath -Path $BuildLogFile
    return @{
        exit_code = [int]$exitCode
        output = $text
        duration_ms = $durationMs
        log_path = $logRel
    }
}

function Get-PythonCommandPath {
    $venvPython = Join-Path $ScriptRoot ".venv/Scripts/python.exe"
    if (Test-Path -Path $venvPython -PathType Leaf) {
        return $venvPython
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        return $pythonCommand.Source
    }

    throw "Python executable was not found. Run ``uv sync`` or create .venv first."
}

function Get-UvCommandPath {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uvCommand) {
        return $uvCommand.Source
    }
    throw "uv executable was not found on PATH."
}

function Get-FileHashValue {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$RelativePaths
    )

    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($relativePath in ($RelativePaths | Sort-Object -Unique)) {
            $fullPath = Join-Path $ScriptRoot $relativePath
            if (-not (Test-Path -Path $fullPath -PathType Leaf)) {
                continue
            }

            $relativeBytes = [System.Text.Encoding]::UTF8.GetBytes($relativePath.Replace("\", "/"))
            $contentBytes = [System.IO.File]::ReadAllBytes($fullPath)
            [void]$hasher.TransformBlock($relativeBytes, 0, $relativeBytes.Length, $relativeBytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
            [void]$hasher.TransformBlock($contentBytes, 0, $contentBytes.Length, $contentBytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
        }
        [void]$hasher.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return ([System.BitConverter]::ToString($hasher.Hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Get-TextHashValue {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Values
    )

    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($value in $Values) {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($value)
            [void]$hasher.TransformBlock($bytes, 0, $bytes.Length, $bytes, 0)
            [void]$hasher.TransformBlock([byte[]](0), 0, 1, [byte[]](0), 0)
        }
        [void]$hasher.TransformFinalBlock([byte[]]::new(0), 0, 0)
        return ([System.BitConverter]::ToString($hasher.Hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Get-StageCacheKey {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StageName
    )

    if ($StageName -eq "self-check") {
        $fileHash = Get-FileHashValue -RelativePaths @(
            "run.ps1",
            "build.ps1",
            "build.py",
            "PSScriptAnalyzerSettings.psd1",
            ".gitattributes",
            ".vscode/settings.json",
            "AGENTS.md",
            "docs/ci.md"
        )
        $psScriptAnalyzerModule = Get-Module -ListAvailable -Name PSScriptAnalyzer | Sort-Object Version -Descending | Select-Object -First 1
        $analyzerStamp = if ($null -ne $psScriptAnalyzerModule) {
            "PSScriptAnalyzer:{0}" -f $psScriptAnalyzerModule.Version.ToString()
        } else {
            "PSScriptAnalyzer:missing"
        }
        return Get-TextHashValue -Values @(
            $fileHash,
            ("PowerShell:{0}" -f $PSVersionTable.PSVersion.ToString()),
            $analyzerStamp
        )
    }

    $pythonPath = Get-PythonCommandPath
    $hashArgs = @(
        "--stage", $StageName,
        "--root", $ScriptRoot,
        "--cache-dir", $CacheDir,
        "--log-dir", $LogsBuildDir,
        "--hash-only"
    )
    if ($StageName -eq "line-endings") {
        $hashArgs += "--eol-fix"
    }
    $jsonOutput = & $pythonPath $BuildPyPath @hashArgs
    if (-not $jsonOutput) {
        throw ("build.py did not return a cache key for stage `{0}`." -f $StageName)
    }

    $payload = ConvertFrom-JsonCompat -Json ($jsonOutput -join "`n")
    return [string]$payload.cache_key
}

function Test-StageCache {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [Parameter(Mandatory = $true)][string]$CacheKey
    )

    if ($NoCache) {
        return $false
    }

    $hashPath = Join-Path $CacheDir ("{0}.sha256" -f $StageName)
    $trustPath = Join-Path $CacheDir ("{0}.trusted" -f $StageName)
    if (-not (Test-Path -Path $hashPath -PathType Leaf) -or -not (Test-Path -Path $trustPath -PathType Leaf)) {
        return $false
    }

    $storedHash = (Get-Content -Path $hashPath -Raw).Trim()
    return $storedHash -eq $CacheKey
}

function Write-StageCache {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [Parameter(Mandatory = $true)][string]$CacheKey
    )

    $hashPath = Join-Path $CacheDir ("{0}.sha256" -f $StageName)
    $trustPath = Join-Path $CacheDir ("{0}.trusted" -f $StageName)
    Set-Content -Path $hashPath -Value $CacheKey -Encoding utf8
    New-Item -ItemType File -Path $trustPath -Force | Out-Null
}

function Invoke-PythonStage {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [switch]$Critical,
        [switch]$AllowCache
    )

    Write-Host ("[{0}] running" -f $StageName) -ForegroundColor Cyan

    $cacheKey = ""
    if ($AllowCache) {
        $cacheKey = Get-StageCacheKey -StageName $StageName
        if (Test-StageCache -StageName $StageName -CacheKey $cacheKey) {
            Add-StageResult (New-StageResult -Name $StageName -Status "cached" -Note "Cache hit.")
            return
        }
    }

    $pythonPath = Get-PythonCommandPath
    $runArgs = @(
        "--stage", $StageName,
        "--root", $ScriptRoot,
        "--cache-dir", $CacheDir,
        "--log-dir", $LogsBuildDir,
        "--build-log", $BuildLogRelPath
    )
    # * Normalize text files to LF before fmt/ruff (matches .gitattributes eol=lf).
    if ($StageName -eq "line-endings") {
        $runArgs += "--eol-fix"
    }
    $jsonOutput = & $pythonPath $BuildPyPath @runArgs
    if (-not $jsonOutput) {
        throw ("build.py returned no output for stage `{0}`." -f $StageName)
    }

    $payload = ConvertFrom-JsonCompat -Json ($jsonOutput -join "`n")
    if ($AllowCache -and $payload.status -in @("ok", "warn")) {
        Write-StageCache -StageName $StageName -CacheKey $cacheKey
    }

    Add-StageResult $payload
    if ($Critical -and $payload.status -eq "fail") {
        throw ("Stage `{0}` failed: {1}" -f $StageName, $payload.note)
    }
}

function Get-CodebaseMemoryCommandPath {
    $command = Get-Command codebase-memory-mcp -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    if ($localAppData) {
        $fallbackPath = Join-Path $localAppData "Programs/codebase-memory-mcp/codebase-memory-mcp.exe"
        if (Test-Path -Path $fallbackPath -PathType Leaf) {
            return $fallbackPath
        }
    }

    return ""
}

function Invoke-CodebaseMemoryStage {
    $stageName = "codebase-memory"
    Write-Host ("[{0}] running" -f $stageName) -ForegroundColor Cyan

    $startedAt = Get-Date
    $commandPath = Get-CodebaseMemoryCommandPath
    if (-not $commandPath) {
        $note = "codebase-memory-mcp is not installed or is not available on PATH; code graph index was not refreshed."
        Add-BuildLogSection -StageName $stageName -ToolName "codebase-memory-mcp" -Body $note
        Add-StageResult (New-StageResult -Name $stageName -Status "warn" -Note $note -DurationMs ([int]((Get-Date) - $startedAt).TotalMilliseconds))
        return
    }

    $payloadJson = @{
        repo_path = $ScriptRoot.Replace("\", "/")
        mode = "full"
        persistence = $false
    } | ConvertTo-Json -Compress
    $result = Invoke-LoggedCommand -StageName $stageName -ToolName "codebase-memory-mcp" -FilePath $commandPath -Arguments @(
        "cli",
        "index_repository",
        $payloadJson
    )

    $details = @{
        command = Resolve-RepoRelativePath -Path $commandPath
        log_path = $result.log_path
        mode = "full"
        persistence = $false
    }
    if ($result.exit_code -ne 0) {
        $note = "codebase-memory-mcp index refresh failed; CI continues because the graph is an auxiliary navigation cache."
        Add-StageResult (New-StageResult -Name $stageName -Status "warn" -Note $note -DurationMs $result.duration_ms -Details $details)
        return
    }

    Add-StageResult (New-StageResult -Name $stageName -Status "ok" -Note "codebase-memory-mcp full index refreshed." -DurationMs $result.duration_ms -Details $details)
}

function Invoke-SelfCheckStage {
    Write-Host "[self-check] running" -ForegroundColor Cyan

    $psScriptAnalyzerModule = Get-Module -ListAvailable -Name PSScriptAnalyzer | Sort-Object Version -Descending | Select-Object -First 1
    $cacheKey = Get-StageCacheKey -StageName "self-check"
    if (($null -ne $psScriptAnalyzerModule) -and (Test-StageCache -StageName "self-check" -CacheKey $cacheKey)) {
        Add-StageResult (New-StageResult -Name "self-check" -Status "cached" -Note "Cache hit.")
        return
    }

    $startedAt = Get-Date
    $issues = New-Object System.Collections.Generic.List[hashtable]
    $notes = New-Object System.Collections.Generic.List[string]
    $status = "ok"
    $missingPSScriptAnalyzer = $false

    foreach ($requiredPath in @(
        $RunScriptPath,
        (Join-Path $ScriptRoot "build.ps1"),
        $BuildPyPath,
        $AnalyzerSettingsPath,
        $RepoPreCommitHookPath,
        (Join-Path $ScriptRoot ".gitattributes"),
        $AgentsPath,
        $CiDocPath
    )) {
        if (-not (Test-Path -Path $requiredPath -PathType Leaf)) {
            $issues.Add(@{
                language = "ci"
                tool = "self-check"
                rule = "missing_file"
                count = 1
                message = ("Required CI file is missing: {0}" -f (Resolve-RepoRelativePath -Path $requiredPath))
            })
        }
    }

    foreach ($scriptPath in @($RunScriptPath, (Join-Path $ScriptRoot "build.ps1"))) {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $scriptPath,
            [ref]$tokens,
            [ref]$parseErrors
        )
        if ($parseErrors.Count -gt 0) {
            foreach ($parseError in $parseErrors) {
                $issues.Add(@{
                    language = "powershell"
                    tool = "parser"
                    rule = "parse_error"
                    count = 1
                    message = ("{0}: {1}" -f (Resolve-RepoRelativePath -Path $scriptPath), $parseError.Message)
                })
            }
        }
    }

    if (Test-Path -Path $VSCodeSettingsPath -PathType Leaf) {
        $vscodeSettings = ConvertFrom-JsonCompat -Json (Get-Content -Path $VSCodeSettingsPath -Raw)
        if ($vscodeSettings["powershell.scriptAnalysis.enable"] -ne $true) {
            $issues.Add(@{
                language = "ci"
                tool = "vscode"
                rule = "missing_script_analysis_enable"
                count = 1
                message = "VS Code PowerShell script analysis is not enabled."
            })
        }
        if ($vscodeSettings["powershell.scriptAnalysis.settingsPath"] -ne '${workspaceFolder}/PSScriptAnalyzerSettings.psd1') {
            $issues.Add(@{
                language = "ci"
                tool = "vscode"
                rule = "missing_settings_path"
                count = 1
                message = "VS Code PowerShell analyzer settings path is not wired to PSScriptAnalyzerSettings.psd1."
            })
        }
    } else {
        $notes.Add(".vscode/settings.json missing; PSScriptAnalyzer IDE wiring optional")
    }

    if (Test-Path -Path $AgentsPath -PathType Leaf) {
        $agentsContent = Get-Content -Path $AgentsPath -Raw
        if (-not $agentsContent.Contains("./run.ps1 -Fast -SkipLaunch")) {
            $issues.Add(@{
                language = "ci"
                tool = "agents"
                rule = "missing_final_verification"
                count = 1
                message = "AGENTS.md must contain ./run.ps1 -Fast -SkipLaunch as the final verification command."
            })
        }
    }

    $pythonPath = Get-PythonCommandPath
    $ruffFormatOutcome = Invoke-LoggedCommand -StageName "self-check" -ToolName "ruff-format-buildpy" -FilePath $pythonPath -Arguments @(
        "-m", "ruff", "format", "build.py"
    )
    if ($ruffFormatOutcome.exit_code -ne 0) {
        $issues.Add(@{
            language = "python"
            tool = "ruff"
            rule = "build_py_format"
            count = 1
            message = "build.py is not correctly formatted."
        })
    }

    $ruffLintOutcome = Invoke-LoggedCommand -StageName "self-check" -ToolName "ruff-buildpy" -FilePath $pythonPath -Arguments @(
        "-m", "ruff", "check", "--fix", "--unsafe-fixes", "build.py"
    )
    if ($ruffLintOutcome.exit_code -ne 0) {
        $issues.Add(@{
            language = "python"
            tool = "ruff"
            rule = "build_py_lint"
            count = 1
            message = "build.py did not pass ruff check."
        })
    }

    $compileOutcome = Invoke-LoggedCommand -StageName "self-check" -ToolName "compileall-buildpy" -FilePath $pythonPath -Arguments @(
        "-m", "compileall", "-q", "build.py"
    )
    if ($compileOutcome.exit_code -ne 0) {
        $issues.Add(@{
            language = "python"
            tool = "compileall"
            rule = "build_py_compile"
            count = 1
            message = "build.py failed Python compilation."
        })
    }

    if ($null -ne $psScriptAnalyzerModule) {
        Import-Module PSScriptAnalyzer -ErrorAction Stop
        $allAnalyzerResults = @()
        foreach ($scriptPath in @($RunScriptPath, (Join-Path $ScriptRoot "build.ps1"))) {
            $allAnalyzerResults += Invoke-ScriptAnalyzer -Path $scriptPath -Settings $AnalyzerSettingsPath
        }
        $analyzerLines = @()
        foreach ($analyzerResult in $allAnalyzerResults) {
            $analyzerLines += ("{0}:{1} [{2}] {3}" -f (Resolve-RepoRelativePath -Path $analyzerResult.ScriptPath), $analyzerResult.Line, $analyzerResult.RuleName, $analyzerResult.Message)
            $issues.Add(@{
                language = "powershell"
                tool = "PSScriptAnalyzer"
                rule = $analyzerResult.RuleName
                count = 1
                message = $analyzerResult.Message
            })
        }
        if ($analyzerLines.Count -eq 0) {
            $analyzerLines += "No PSScriptAnalyzer issues found."
        }
        Add-BuildLogSection -StageName "self-check" -ToolName "psscriptanalyzer" -Body ($analyzerLines -join [Environment]::NewLine)
        $notes.Add("PSScriptAnalyzer executed")
    } else {
        $missingPSScriptAnalyzer = $true
        $notes.Add("PSScriptAnalyzer is not installed; PowerShell static analysis is skipped")
    }

    if ($issues.Count -gt 0) {
        $status = "fail"
    } elseif ($missingPSScriptAnalyzer) {
        $status = "warn"
    }

    if ($notes.Count -eq 0) {
        $notes.Add("CI wrapper checks passed")
    }

    if ($null -ne $script:GitHooksBootstrap -and -not [string]::IsNullOrWhiteSpace($script:GitHooksBootstrap.note)) {
        $notes.Add($script:GitHooksBootstrap.note)
    }

    $result = New-StageResult `
        -Name "self-check" `
        -Status $status `
        -Note ($notes -join "; ") `
        -DurationMs ([int]((Get-Date) - $startedAt).TotalMilliseconds) `
        -Details @{ log_paths = @((Resolve-RepoRelativePath -Path $BuildLogFile)) } `
        -Issues @($issues.ToArray())

    if (($status -in @("ok", "warn")) -and ($null -ne $psScriptAnalyzerModule)) {
        Write-StageCache -StageName "self-check" -CacheKey $cacheKey
    }

    Add-StageResult $result
    if ($status -eq "fail") {
        throw ("Stage `self-check` failed.")
    }
}

function Invoke-LaunchStage {
    Write-Host "[launch] running" -ForegroundColor Cyan

    $uvPath = Get-UvCommandPath
    $arguments = @("run", "python", "-m", "rutherford", "--smoke")
    $logPath = $RuntimeLaunchLogFile
    $startedAt = Get-Date
    $commandText = "{0} {1}" -f $uvPath, ($arguments -join " ")

    Set-Content -Path $logPath -Value @(
        $commandText.Trim(),
        "exit_code: pending",
        ""
    ) -Encoding utf8

    $exitCode = -1
    try {
        & $uvPath @arguments
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            $exitCode = -1
        } else {
            $exitCode = [int]$exitCode
        }
    } catch [System.Management.Automation.PipelineStoppedException] {
        Write-BuildInterruptMessage
        $exitCode = 130
    }

    $durationMs = [int]((Get-Date) - $startedAt).TotalMilliseconds
    Add-Content -Path $logPath -Value ("exit_code: {0}" -f $exitCode)
    $launchLogPaths = @((Resolve-RepoRelativePath -Path $RuntimeLaunchLogFile))

    if ($exitCode -eq 0) {
        Add-StageResult (New-StageResult `
            -Name "launch" `
            -Status "ok" `
            -Note "MCP smoke check passed (python -m rutherford --smoke)." `
            -DurationMs $durationMs `
            -Details @{ log_paths = $launchLogPaths })
        return
    }

    if ($exitCode -eq 130) {
        Add-StageResult (New-StageResult `
            -Name "launch" `
            -Status "warn" `
            -Note "Smoke check interrupted by user." `
            -DurationMs $durationMs `
            -Details @{ log_paths = $launchLogPaths })
        return
    }

    Add-StageResult (New-StageResult `
        -Name "launch" `
        -Status "fail" `
        -Note ("Smoke check exited with code {0}." -f $exitCode) `
        -DurationMs $durationMs `
        -Details @{ log_paths = $launchLogPaths })
    throw ("Stage `launch` failed with exit code {0}." -f $exitCode)
}

function Write-CiReport {
    $overallStatus = Get-OverallStatus
    $stageSnapshot = @($script:StageResults.ToArray())
    $issueSnapshot = @($script:Issues.ToArray())
    $metricsSnapshot = @{}
    foreach ($key in $script:Metrics.Keys) {
        $metricsSnapshot[$key] = $script:Metrics[$key]
    }

    $ciEnd = if ($null -ne $script:CiPhaseEndTime) { $script:CiPhaseEndTime } else { Get-Date }
    $ciDurationMs = [int](($ciEnd - $script:CiStartTime).TotalMilliseconds)
    $runtimeDurationMs = [Math]::Max(0, [int]((Get-Date) - $script:CiStartTime).TotalMilliseconds - $ciDurationMs)
    $gitMetadata = Get-GitHeadState
    $report = [ordered]@{
        schema_version = 1
        started_at_utc = $script:CiStartTime.ToUniversalTime().ToString("o")
        finished_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        duration_ms = [int]((Get-Date) - $script:CiStartTime).TotalMilliseconds
        ci_duration_ms = $ciDurationMs
        runtime_duration_ms = $runtimeDurationMs
        status = $overallStatus
        stages = $stageSnapshot
        issues = $issueSnapshot
        metrics = $metricsSnapshot
        ci = @{
            passed = ($overallStatus -ne "fail")
            profile = Get-CurrentCiProfileName
            skip_launch = [bool]$SkipLaunch
        }
        git = $gitMetadata
    }

    $json = $report | ConvertTo-Json -Depth 10
    Set-Content -Path $ReportPath -Value $json -Encoding utf8
}

function Write-EnforcerState {
    $reportJson = Get-Content -Path $ReportPath -Raw
    Set-Content -Path $EnforcerLastCheckPath -Value $reportJson -Encoding utf8

    $started = $script:CiStartTime.ToUniversalTime().ToString("o")
    $finished = (Get-Date).ToUniversalTime().ToString("o")
    Add-Content -Path $EnforcerStatsPath -Value ("--- Check started at {0} ---" -f $started)
    foreach ($issue in $script:Issues) {
        $lang = Resolve-StageResultProperty -Stage $issue -PropertyName "language"
        $tool = Resolve-StageResultProperty -Stage $issue -PropertyName "tool"
        $rule = Resolve-StageResultProperty -Stage $issue -PropertyName "rule"
        $message = Resolve-StageResultProperty -Stage $issue -PropertyName "message"
        $count = Resolve-StageResultProperty -Stage $issue -PropertyName "count"
        Add-Content -Path $EnforcerStatsPath -Value (
            "{0}: [{1}] {2} - {3} (x{4})" -f $lang, $tool, $rule, $message, $count
        )
    }
    Add-Content -Path $EnforcerStatsPath -Value (
        "--- Check finished at {0} (status={1}) ---" -f $finished, (Get-OverallStatus)
    )
    Add-Content -Path $EnforcerStatsPath -Value ""
}

function Get-StatusColor {
    param([Parameter(Mandatory = $true)][string]$Status)

    switch ($Status) {
        "ok" { return "Green" }
        "warn" { return "Yellow" }
        "fail" { return "Red" }
        "cached" { return "Cyan" }
        "skip" { return "DarkGray" }
        default { return "White" }
    }
}

function Resolve-StageResultProperty {
    param(
        [AllowNull()][object]$Stage,
        [Parameter(Mandatory = $true)][string]$PropertyName
    )

    if ($null -eq $Stage) {
        return $null
    }
    if ($Stage -is [System.Collections.IDictionary]) {
        return $Stage[$PropertyName]
    }
    return $Stage.$PropertyName
}

function Get-TruncatedDisplayText {
    param(
        [AllowNull()][string]$Text,
        [Parameter(Mandatory = $true)][int]$MaxLength
    )

    if ([string]::IsNullOrEmpty($Text)) {
        return ""
    }

    $normalized = ($Text -replace "\s+", " ").Trim()
    if ($normalized.Length -le $MaxLength) {
        return $normalized
    }

    if ($MaxLength -le 3) {
        return $normalized.Substring(0, $MaxLength)
    }

    return ($normalized.Substring(0, $MaxLength - 3) + "...")
}

function Get-StageDiagnosticLogFile {
    param(
        [Parameter(Mandatory = $true)][string]$StageName,
        [AllowNull()][object]$Details
    )

    $limits = $script:ProblemDisplayLimits
    $candidates = New-Object System.Collections.Generic.List[string]

    # * Prefer per-tool stage logs (build.py always writes them even when report points at build.log).
    if (Test-Path -Path $LogsBuildDir -PathType Container) {
        $pattern = "{0}-*.log" -f $StageName
        $stageLogs = @(Get-ChildItem -Path $LogsBuildDir -Filter $pattern -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
        foreach ($stageLog in $stageLogs) {
            if ($candidates.Count -ge [int]$limits.MaxLogFilesPerStage) {
                break
            }
            [void]$candidates.Add($stageLog.FullName)
        }
    }

    if ($candidates.Count -eq 0 -and ($null -ne $Details)) {
        $logPaths = $null
        if (($Details -is [System.Collections.IDictionary]) -and $Details.ContainsKey("log_paths")) {
            $logPaths = @($Details["log_paths"])
        } elseif ($Details.PSObject -and $Details.PSObject.Properties["log_paths"]) {
            $logPaths = @($Details.log_paths)
        }

        foreach ($logPath in @($logPaths)) {
            if ([string]::IsNullOrWhiteSpace([string]$logPath)) {
                continue
            }
            $fullPath = if ([System.IO.Path]::IsPathRooted([string]$logPath)) {
                [string]$logPath
            } else {
                Join-Path $ScriptRoot ([string]$logPath)
            }
            if ((Test-Path -Path $fullPath -PathType Leaf) -and -not $candidates.Contains($fullPath)) {
                [void]$candidates.Add($fullPath)
            }
            if ($candidates.Count -ge [int]$limits.MaxLogFilesPerStage) {
                break
            }
        }
    }

    if ($candidates.Count -eq 0 -and $StageName -eq "launch" -and (Test-Path -Path $RuntimeLaunchLogFile -PathType Leaf)) {
        [void]$candidates.Add($RuntimeLaunchLogFile)
    }

    return @($candidates.ToArray())
}

function Get-LogExcerptLine {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$StageName = ""
    )

    $limits = $script:ProblemDisplayLimits
    if (-not (Test-Path -Path $Path -PathType Leaf)) {
        return @()
    }

    $rawLines = @(Get-Content -Path $Path -Encoding utf8 -ErrorAction SilentlyContinue)
    if ($rawLines.Count -eq 0) {
        return @()
    }

    $filtered = New-Object System.Collections.Generic.List[string]
    foreach ($rawLine in $rawLines) {
        $line = [string]$rawLine
        $trimmed = $line.TrimEnd()
        # * Drop pytest progress dots and command metadata noise.
        if ($trimmed -match '^\.+\s*(\[\s*\d+%\])?\s*$') {
            continue
        }
        if ($trimmed -match '^(command|duration_ms):') {
            continue
        }
        if ([string]::IsNullOrWhiteSpace($trimmed)) {
            if ($filtered.Count -eq 0) {
                continue
            }
            if ($filtered[$filtered.Count - 1] -eq "") {
                continue
            }
            [void]$filtered.Add("")
            continue
        }
        [void]$filtered.Add($trimmed)
    }

    if ($filtered.Count -eq 0) {
        return @()
    }

    $lines = @($filtered.ToArray())
    $startIndex = -1

    # * For the unified build.log, jump to the last section for this stage when possible.
    if ((-not [string]::IsNullOrWhiteSpace($StageName)) -and ([System.IO.Path]::GetFileName($Path) -ieq "build.log")) {
        $sectionPrefix = ("=== {0}/" -f $StageName)
        for ($i = $lines.Count - 1; $i -ge 0; $i--) {
            if ($lines[$i].StartsWith($sectionPrefix)) {
                $startIndex = $i
                break
            }
        }
    }

    if ($startIndex -lt 0) {
        $preferredPatterns = @(
            "short test summary info",
            "per-file coverage below",
            "Required test coverage of",
            "^=+\s*FAILURES",
            "Traceback \(most recent call last\)",
            "^FAILED ",
            "^ERROR ",
            "would reformat",
            "Found \d+ error",
            "\[PS[A-Za-z]+\]",
            ": error:"
        )
        foreach ($pattern in $preferredPatterns) {
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -match $pattern) {
                    $startIndex = $i
                    break
                }
            }
            if ($startIndex -ge 0) {
                break
            }
        }
    }

    if ($startIndex -lt 0) {
        $take = [Math]::Min([int]$limits.MaxExcerptLines, $lines.Count)
        $startIndex = $lines.Count - $take
    }

    $excerpt = New-Object System.Collections.Generic.List[string]
    $charCount = 0
    for ($i = $startIndex; $i -lt $lines.Count; $i++) {
        if ($excerpt.Count -ge [int]$limits.MaxExcerptLines) {
            [void]$excerpt.Add(("... ({0} more line(s) truncated)" -f ($lines.Count - $i)))
            break
        }

        $displayLine = Get-TruncatedDisplayText -Text $lines[$i] -MaxLength ([int]$limits.MaxExcerptLineLength)
        $nextCount = $charCount + $displayLine.Length + 1
        if (($excerpt.Count -gt 0) -and ($nextCount -gt [int]$limits.MaxExcerptChars)) {
            [void]$excerpt.Add(("... ({0} more line(s) truncated by size)" -f ($lines.Count - $i)))
            break
        }

        [void]$excerpt.Add($displayLine)
        $charCount = $nextCount
    }

    return @($excerpt.ToArray())
}

function Write-StageProblem {
    param(
        [Parameter(Mandatory = $true)][object]$Stage,
        [ValidateSet("Immediate", "Summary")]
        [string]$Mode = "Immediate"
    )

    $status = [string](Resolve-StageResultProperty -Stage $Stage -PropertyName "status")
    if ($status -notin @("fail", "warn")) {
        return
    }

    $limits = $script:ProblemDisplayLimits
    $stageName = [string](Resolve-StageResultProperty -Stage $Stage -PropertyName "name")
    $color = Get-StatusColor -Status $status
    $issues = @((Resolve-StageResultProperty -Stage $Stage -PropertyName "issues"))
    $details = Resolve-StageResultProperty -Stage $Stage -PropertyName "details"

    if ($Mode -eq "Immediate") {
        Write-Host ""
        Write-Host ("--- {0} problems ({1}) ---" -f $stageName, $status.ToUpper()) -ForegroundColor $color
    } else {
        Write-Host ("  [{0}] {1}" -f $status.ToUpper(), $stageName) -ForegroundColor $color
    }

    $issueShown = 0
    foreach ($issue in $issues) {
        if ($null -eq $issue) {
            continue
        }
        if ($issueShown -ge [int]$limits.MaxIssuesPerStage) {
            $remaining = @($issues | Where-Object { $null -ne $_ }).Count - $issueShown
            if ($remaining -gt 0) {
                Write-Host ("  ... and {0} more issue(s)" -f $remaining) -ForegroundColor DarkGray
            }
            break
        }

        $tool = [string](Resolve-StageResultProperty -Stage $issue -PropertyName "tool")
        $rule = [string](Resolve-StageResultProperty -Stage $issue -PropertyName "rule")
        $message = Get-TruncatedDisplayText `
            -Text ([string](Resolve-StageResultProperty -Stage $issue -PropertyName "message")) `
            -MaxLength ([int]$limits.MaxMessageLength)
        $count = Resolve-StageResultProperty -Stage $issue -PropertyName "count"
        $countSuffix = ""
        if ($null -ne $count -and [int]$count -gt 1) {
            $countSuffix = (" x{0}" -f [int]$count)
        }

        $label = if ($tool -and $rule) {
            "{0}/{1}" -f $tool, $rule
        } elseif ($tool) {
            $tool
        } elseif ($rule) {
            $rule
        } else {
            "issue"
        }

        Write-Host ("  - [{0}] {1}{2}" -f $label, $message, $countSuffix) -ForegroundColor $color
        $issueShown++
    }

    if ($issueShown -eq 0) {
        $note = Get-TruncatedDisplayText `
            -Text ([string](Resolve-StageResultProperty -Stage $Stage -PropertyName "note")) `
            -MaxLength ([int]$limits.MaxMessageLength)
        if ($note) {
            Write-Host ("  - {0}" -f $note) -ForegroundColor $color
        }
    }

    # * Immediate mode shows diagnostic excerpts; summary mode only points at logs.
    $logFiles = @(Get-StageDiagnosticLogFile -StageName $stageName -Details $details)
    if ($Mode -eq "Immediate") {
        foreach ($logFile in $logFiles) {
            $isUnifiedBuildLog = ([System.IO.Path]::GetFileName($logFile) -ieq "build.log")
            # * Structured issues already carry the signal for self-check; skip noisy unified-log dump.
            if ($isUnifiedBuildLog -and ($issueShown -gt 0)) {
                Write-Host ("  log: {0}" -f (Resolve-RepoRelativePath -Path $logFile)) -ForegroundColor DarkGray
                continue
            }

            $excerpt = @(Get-LogExcerptLine -Path $logFile -StageName $stageName)
            if ($excerpt.Count -eq 0) {
                continue
            }
            Write-Host ("  excerpt ({0}):" -f (Resolve-RepoRelativePath -Path $logFile)) -ForegroundColor DarkGray
            foreach ($line in $excerpt) {
                Write-Host ("    {0}" -f $line) -ForegroundColor $color
            }
        }
    } elseif ($logFiles.Count -gt 0) {
        $relLogs = @($logFiles | ForEach-Object { Resolve-RepoRelativePath -Path $_ })
        Write-Host ("    logs: {0}" -f ($relLogs -join ", ")) -ForegroundColor DarkGray
    }
}

function Write-ProblemSummary {
    $problemStages = @($script:StageResults | Where-Object {
            $status = [string](Resolve-StageResultProperty -Stage $_ -PropertyName "status")
            $status -in @("fail", "warn")
        })

    if ($problemStages.Count -eq 0) {
        return
    }

    Write-Host ""
    Write-Host "PROBLEMS:" -ForegroundColor Yellow
    $issueBudget = [int]$script:ProblemDisplayLimits.MaxIssuesInSummary
    $issuesPrinted = 0
    $stagesShown = 0

    foreach ($stage in $problemStages) {
        if ($issuesPrinted -ge $issueBudget) {
            $omitted = $problemStages.Count - $stagesShown
            if ($omitted -gt 0) {
                Write-Host ("  ... additional problem stage(s) omitted ({0})" -f $omitted) -ForegroundColor DarkGray
            }
            break
        }

        Write-StageProblem -Stage $stage -Mode Summary
        $stagesShown++
        $stageIssues = @((Resolve-StageResultProperty -Stage $stage -PropertyName "issues") | Where-Object { $null -ne $_ })
        $issuesPrinted += [Math]::Max(1, $stageIssues.Count)
    }
}

function Write-CompactSummary {
    Write-Host ""
    Write-Host ("OVERALL: {0}" -f (Get-OverallStatus).ToUpper()) -ForegroundColor (Get-StatusColor -Status (Get-OverallStatus))
    Write-Host ("Profile: {0}" -f (Get-CurrentCiProfileName)) -ForegroundColor DarkGray
    Write-Host ""

    foreach ($stage in $script:StageResults) {
        $stStatus = [string](Resolve-StageResultProperty -Stage $stage -PropertyName "status")
        $stName = Resolve-StageResultProperty -Stage $stage -PropertyName "name"
        $color = Get-StatusColor -Status $stStatus
        $note = ""
        $stNote = Resolve-StageResultProperty -Stage $stage -PropertyName "note"
        if ($stNote) {
            $note = (" - {0}" -f (Get-TruncatedDisplayText -Text ([string]$stNote) -MaxLength 160))
        }
        Write-Host ("[{0}] {1}{2}" -f $stStatus.ToUpper(), $stName, $note) -ForegroundColor $color
    }

    Write-ProblemSummary

    Write-Host ""
    Write-Host ("Report: {0}" -f (Resolve-RepoRelativePath -Path $ReportPath)) -ForegroundColor DarkGray
    Write-Host ("Enforcer: {0}, {1}" -f (Resolve-RepoRelativePath -Path $EnforcerLastCheckPath), (Resolve-RepoRelativePath -Path $EnforcerStatsPath)) -ForegroundColor DarkGray
    Write-Host ("Total (wall clock): {0}s" -f ([int]((Get-Date) - $script:CiStartTime).TotalSeconds)) -ForegroundColor DarkGray
}

try {
    Invoke-SelfCheckStage
    Invoke-PythonStage -StageName "line-endings" -Critical -AllowCache
    Invoke-PythonStage -StageName "agents-coverage" -AllowCache
    Invoke-PythonStage -StageName "fmt" -Critical -AllowCache
    Invoke-PythonStage -StageName "lint" -Critical -AllowCache
    Invoke-PythonStage -StageName "line-limits" -AllowCache
    Invoke-PythonStage -StageName "license-check" -Critical -AllowCache
    Invoke-PythonStage -StageName "compile" -Critical -AllowCache

    if ($Fast) {
        Invoke-PythonStage -StageName "test" -Critical -AllowCache
        Add-StageResult (New-StageResult -Name "coverage" -Status "skip" -Note "Skipped by -Fast profile.")
        Add-StageResult (New-StageResult -Name "security" -Status "skip" -Note "Skipped by -Fast profile.")
    } else {
        Add-StageResult (New-StageResult -Name "test" -Status "skip" -Note "Covered by the coverage stage in full profile.")
        Invoke-PythonStage -StageName "coverage" -Critical -AllowCache
        Invoke-PythonStage -StageName "security" -AllowCache
    }

    Invoke-CodebaseMemoryStage

    Add-StageResult (New-StageResult -Name "build" -Status "skip" -Note "Not applicable: no distributable artifact.")
    Add-StageResult (New-StageResult -Name "db-checks" -Status "skip" -Note "Not applicable: no database migrations.")

    $script:CiPhaseEndTime = Get-Date
    if ($SkipLaunch) {
        Add-StageResult (New-StageResult -Name "launch" -Status "skip" -Note "Skipped by -SkipLaunch flag.")
    } else {
        Invoke-LaunchStage
    }

    Add-StageResult (New-StageResult -Name "archive" -Status "skip" -Note "Not applicable: no distributable artifacts are produced.")
} catch {
    $ex = $_.Exception
    if ($ex -is [System.Management.Automation.PipelineStoppedException]) {
        Write-BuildInterruptMessage
    } else {
        Write-Host ("Pipeline stopped: {0}" -f $ex.Message) -ForegroundColor Red
    }
} finally {
    Write-CiReport
    Write-EnforcerState
    Write-CompactSummary
}

if ((Get-OverallStatus) -eq "fail") {
    exit 1
}

exit 0
