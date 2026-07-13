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

function Show-Help {
    Write-Host "Local CI Runner (Rutherford / Agent Enforcer 2)" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Usage: .\run.ps1 [options]"
    Write-Host ""
    Write-Host "Profiles:"
    Write-Host "  -Fast         Skip coverage and security stages only."
    Write-Host ""
    Write-Host "Execution:"
    Write-Host "  -SkipLaunch   Skip the smoke launch stage (uv run python -m rutherford --smoke)."
    Write-Host ""
    Write-Host "Cache control:"
    Write-Host "  -NoCache      Ignore existing stage cache and recompute results."
    Write-Host "  -ForceAll     Re-run all implement stages for this invocation."
    Write-Host "  -Clean        Remove .ci_cache before the run starts."
    Write-Host ""
    Write-Host "Other:"
    Write-Host "  -Help         Show this help message."
}

$knownParamNames = @(
    "Fast",
    "SkipLaunch",
    "NoCache",
    "ForceAll",
    "Clean",
    "Help"
)

$exitCode = 0
try {
    if ($args.Count -gt 0) {
        Write-Host ("Error: Unknown parameter(s): {0}" -f ($args -join ", ")) -ForegroundColor Red
        Write-Host ("Valid parameters: -{0}" -f ($knownParamNames -join ", -")) -ForegroundColor Yellow
        $exitCode = 1
    } elseif ($Help) {
        Show-Help
        $exitCode = 0
    } else {
        $buildScript = Join-Path $PSScriptRoot "build.ps1"
        if (-not (Test-Path -Path $buildScript -PathType Leaf)) {
            Write-Host ("Error: build.ps1 was not found at {0}" -f $buildScript) -ForegroundColor Red
            $exitCode = 1
        } else {
            $forwardedParameters = @{}
            foreach ($entry in $PSBoundParameters.GetEnumerator()) {
                if ($entry.Key -ne "Help") {
                    $forwardedParameters[$entry.Key] = $entry.Value
                }
            }

            if ($ForceAll) {
                $forwardedParameters["NoCache"] = $true
            }

            & $buildScript @forwardedParameters
            if ($null -eq $LASTEXITCODE) {
                $exitCode = 0
            } else {
                $exitCode = [int]$LASTEXITCODE
            }
        }
    }
} finally {
}

exit $exitCode
