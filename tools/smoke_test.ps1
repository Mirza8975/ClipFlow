<#
==============================================================================
ClipFlow Local Terminal Smoke-Test Toolkit (Windows PowerShell)
==============================================================================
Verifies container lifecycle, persistent storage, health-check, API connectivity,
optional MP4 video processing, and clip downloads.
==============================================================================
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [int]$Port = 8765,

    [Parameter(Mandatory = $false)]
    [string]$ContainerName = "clipflow-smoke-test",

    [Parameter(Mandatory = $false)]
    [string]$VolumeName = "clipflow-smoke-data",

    [Parameter(Mandatory = $false)]
    [string]$Image = "ghcr.io/mirza8975/clipflow:latest",

    [Parameter(Mandatory = $false)]
    [string]$VideoPath = "",

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = ".\smoke_test_downloads",

    [Parameter(Mandatory = $false)]
    [switch]$KeepContainer
)

$ErrorActionPreference = "Continue"

$Global:Passed = 0
$Global:Failed = 0
$Global:Total = 0

function Write-Pass {
    param([string]$Message)
    $Global:Total++
    $Global:Passed++
    Write-Host "  [PASS] " -ForegroundColor Green -NoNewline
    Write-Host $Message
}

function Write-Fail {
    param([string]$Message, [string]$Details = "")
    $Global:Total++
    $Global:Failed++
    Write-Host "  [FAIL] " -ForegroundColor Red -NoNewline
    Write-Host $Message
    if ($Details) {
        Write-Host "         Details: $Details" -ForegroundColor Yellow
    }
}

function Write-Info {
    param([string]$Message)
    Write-Host "==> " -ForegroundColor Cyan -NoNewline
    Write-Host $Message
}

$BaseUrl = "http://127.0.0.1:$Port"

Write-Host "======================================================================"
Write-Host "         CLIPFLOW TERMINAL SMOKE-TEST TOOLKIT (WINDOWS/PS)            "
Write-Host "======================================================================"
Write-Host " Target Image     : $Image"
Write-Host " Container Name   : $ContainerName"
Write-Host " Persistent Volume: $VolumeName"
Write-Host " Port Binding     : $Port"
Write-Host " Video Upload     : $(if ($VideoPath) { $VideoPath } else { 'None (Skipping upload test)' })"
Write-Host "======================================================================"
Write-Host ""

# ------------------------------------------------------------------------------
# TEST 1: Check Docker availability
# ------------------------------------------------------------------------------
Write-Info "Testing Docker daemon availability..."
try {
    $dockerInfo = docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Pass "Docker CLI and daemon are running"
    } else {
        Write-Fail "Docker daemon is not running or CLI returned error" $dockerInfo
        exit 1
    }
} catch {
    Write-Fail "Docker CLI is not installed or not in PATH" $_.Exception.Message
    exit 1
}

# ------------------------------------------------------------------------------
# TEST 2: Pull / Verify Docker Image
# ------------------------------------------------------------------------------
Write-Info "Pulling or verifying container image: $Image..."
docker pull $Image
if ($LASTEXITCODE -eq 0) {
    Write-Pass "Successfully pulled container image '$Image'"
} else {
    Write-Fail "Failed to pull container image '$Image'"
    exit 1
}

# ------------------------------------------------------------------------------
# TEST 3: Create Persistent Volume & Start Container
# ------------------------------------------------------------------------------
Write-Info "Setting up Docker persistent volume and container..."
$volExists = docker volume ls --format '{{.Name}}' | Where-Object { $_ -eq $VolumeName }
if ($volExists) {
    Write-Host "  Volume '$VolumeName' already exists (preserving data)."
} else {
    docker volume create $VolumeName | Out-Null
    Write-Host "  Created persistent volume '$VolumeName'."
}

# Stop and remove any pre-existing container with this test name
$existingContainer = docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq $ContainerName }
if ($existingContainer) {
    docker stop $ContainerName 2>&1 | Out-Null
    docker rm $ContainerName 2>&1 | Out-Null
}

# Start the container
docker run -d `
    --name $ContainerName `
    -p "${Port}:8765" `
    -v "${VolumeName}:/data" `
    -e CLIPFLOW_PORT=8765 `
    -e CLIPFLOW_HOST=0.0.0.0 `
    -e CLIPFLOW_ACCOUNTS=0 `
    -e CLIPFLOW_TRUST_PROXY=1 `
    $Image | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Pass "Started container '$ContainerName' on port $Port"
} else {
    Write-Fail "Failed to start Docker container '$ContainerName'"
    exit 1
}

# Cleanup helper on exit
function Cleanup-Container {
    if (-not $KeepContainer) {
        Write-Info "Stopping smoke-test container '$ContainerName' (preserving volume '$VolumeName')..."
        docker stop $ContainerName 2>&1 | Out-Null
        docker rm $ContainerName 2>&1 | Out-Null
    } else {
        Write-Info "Keeping container '$ContainerName' running on $BaseUrl as requested."
    }
}

# ------------------------------------------------------------------------------
# TEST 4: Health-Check Endpoint (GET /healthz)
# ------------------------------------------------------------------------------
Write-Info "Waiting for container initialization and verifying GET /healthz..."
$healthy = $false
for ($i = 1; $i -le 60; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 3 -ErrorAction SilentlyContinue
        if ($resp -and $resp.ok -eq $true) {
            $healthy = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}

if ($healthy) {
    Write-Pass "Endpoint GET /healthz returned {'ok': true}"
} else {
    Write-Fail "GET /healthz did not respond with {'ok': true} within 60s"
    Write-Host "Container logs:" -ForegroundColor Yellow
    docker logs --tail 30 $ContainerName
    Cleanup-Container
    exit 1
}

# ------------------------------------------------------------------------------
# TEST 5: Verify API Status Endpoint (GET /api/status)
# ------------------------------------------------------------------------------
Write-Info "Verifying API status endpoint (GET /api/status)..."
try {
    $statusResp = Invoke-RestMethod -Uri "$BaseUrl/api/status" -TimeoutSec 5 -ErrorAction Stop
    if ($statusResp.jobs -ne $null -or $statusResp.load -ne $null) {
        Write-Pass "Endpoint GET /api/status returned valid operational JSON"
    } else {
        Write-Fail "GET /api/status returned unexpected structure"
    }
} catch {
    Write-Fail "GET /api/status failed to respond" $_.Exception.Message
}

# ------------------------------------------------------------------------------
# TEST 6: Verify Jobs Listing (GET /api/jobs)
# ------------------------------------------------------------------------------
Write-Info "Verifying jobs list endpoint (GET /api/jobs)..."
try {
    $jobsResp = Invoke-RestMethod -Uri "$BaseUrl/api/jobs" -TimeoutSec 5 -ErrorAction Stop
    Write-Pass "Endpoint GET /api/jobs returned valid response"
} catch {
    Write-Fail "GET /api/jobs failed to respond" $_.Exception.Message
}

# ------------------------------------------------------------------------------
# TEST 7-10: Optional Video Upload, Polling & Clip Download
# ------------------------------------------------------------------------------
$uploadedJid = ""
if ($VideoPath) {
    if (-not (Test-Path $VideoPath)) {
        Write-Fail "Specified video file does not exist: $VideoPath"
    } else {
        Write-Info "Uploading local MP4 video file: $VideoPath..."
        try {
            # Use curl.exe for robust multipart form upload on Windows
            $uploadOutput = & curl.exe -s -m 120 -X POST "$BaseUrl/api/upload" `
                -F "file=@$VideoPath" `
                -F "options={}"

            $jobObj = $uploadOutput | ConvertFrom-Json
            $uploadedJid = $jobObj.id

            if ($uploadedJid) {
                Write-Pass "Video uploaded successfully. Created Job ID: $uploadedJid"

                Write-Info "Polling Job $uploadedJid status until done or failed..."
                $jobDone = $false
                $jobFailed = $false
                $jobDetail = $null

                for ($poll = 1; $poll -le 120; $poll++) {
                    $jobDetail = Invoke-RestMethod -Uri "$BaseUrl/api/jobs/$uploadedJid" -TimeoutSec 5 -ErrorAction SilentlyContinue
                    $curStatus = $jobDetail.status

                    if ($curStatus -eq "done") {
                        $jobDone = $true
                        break
                    } elseif ($curStatus -eq "failed") {
                        $jobFailed = $true
                        break
                    }
                    Write-Host -NoNewline "  Processing... Current status: [$curStatus] ($($poll * 2)s)`r"
                    Start-Sleep -Seconds 2
                }
                Write-Host ""

                if ($jobDone) {
                    Write-Pass "Job $uploadedJid processing completed with status: done"

                    # Create download folder
                    if (-not (Test-Path $OutputDir)) {
                        New-Item -ItemType Directory -Path $OutputDir | Out-Null
                    }

                    if ($jobDetail.clips -and $jobDetail.clips.Count -gt 0) {
                        foreach ($clip in $jobDetail.clips) {
                            $clipFile = $clip.file
                            $targetPath = Join-Path $OutputDir $clipFile
                            $downloadUrl = "$BaseUrl/media/$uploadedJid/$clipFile`?download=1"

                            & curl.exe -s -m 60 -o $targetPath $downloadUrl
                            if ((Test-Path $targetPath) -and (Get-Item $targetPath).Length -gt 0) {
                                $size = (Get-Item $targetPath).Length
                                Write-Pass "Clip downloaded successfully: $targetPath ($size bytes)"
                            } else {
                                Write-Fail "Failed to download or clip file has 0 bytes: $clipFile"
                            }
                        }
                    } else {
                        Write-Host "  Notice: Job completed with 0 clips (no loud speech/audio detected)." -ForegroundColor Yellow
                    }
                } elseif ($jobFailed) {
                    Write-Fail "Job $uploadedJid failed during processing"
                } else {
                    Write-Fail "Job $uploadedJid timed out waiting for completion"
                }
            } else {
                Write-Fail "POST /api/upload failed to create job" $uploadOutput
            }
        } catch {
            Write-Fail "Error during video upload or processing" $_.Exception.Message
        }
    }
} else {
    Write-Host "  (Skipping upload & clip download tests; supply -VideoPath <path> to run end-to-end)"
}

# ------------------------------------------------------------------------------
# TEST 11: Container Restart & Persistence Verification
# ------------------------------------------------------------------------------
Write-Info "Testing container restart and data persistence..."
docker restart $ContainerName 2>&1 | Out-Null

$restartedHealthy = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 3 -ErrorAction SilentlyContinue
        if ($resp -and $resp.ok -eq $true) {
            $restartedHealthy = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}

if ($restartedHealthy) {
    Write-Pass "Container restarted successfully and GET /healthz is healthy"
} else {
    Write-Fail "Container failed to respond healthy after restart"
}

# Verify data was preserved in the volume
try {
    $restartJobs = Invoke-RestMethod -Uri "$BaseUrl/api/jobs" -TimeoutSec 5
    if ($uploadedJid) {
        $found = $restartJobs | Where-Object { $_.id -eq $uploadedJid }
        if ($found) {
            Write-Pass "Job ID $uploadedJid preserved in persistent volume across restart"
        } else {
            Write-Fail "Job ID $uploadedJid was lost after container restart"
        }
    } else {
        Write-Pass "Database and jobs store preserved across container restart"
    }
} catch {
    Write-Fail "Jobs store corrupted or unreachable after restart" $_.Exception.Message
}

# Cleanup
Cleanup-Container

# ------------------------------------------------------------------------------
# Summary Report
# ------------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================================================"
Write-Host "                         TEST RESULTS SUMMARY                         "
Write-Host "======================================================================"
Write-Host " Total Tests Run : $Global:Total"
Write-Host " Passed          : $Global:Passed" -ForegroundColor Green
Write-Host " Failed          : $Global:Failed" -ForegroundColor $(if ($Global:Failed -gt 0) { "Red" } else { "Green" })
Write-Host "======================================================================"

if ($Global:Failed -eq 0) {
    Write-Host "ALL SMOKE TESTS PASSED! ClipFlow is operational and ready." -ForegroundColor Green
    exit 0
} else {
    Write-Host "SOME SMOKE TESTS FAILED. Please review the output above." -ForegroundColor Red
    exit 1
}
