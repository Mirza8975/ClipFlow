#!/usr/bin/env bash
# ==============================================================================
# ClipFlow Local Terminal Smoke-Test Toolkit (Linux / macOS)
# ==============================================================================
# Verifies container lifecycle, persistent storage, health-check, API connectivity,
# optional MP4 video processing, and clip downloads.
# ==============================================================================

set -o pipefail

# Defaults
PORT="8765"
CONTAINER_NAME="clipflow-smoke-test"
VOLUME_NAME="clipflow-smoke-data"
IMAGE="ghcr.io/mirza8975/clipflow:latest"
VIDEO_PATH=""
KEEP_CONTAINER=false
OUTPUT_DIR="./smoke_test_downloads"

# Terminal Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PASSED=0
FAILED=0
TOTAL=0

usage() {
    cat << EOF
ClipFlow Local Terminal Smoke-Test Toolkit

Usage: $(basename "$0") [OPTIONS]

Options:
  -p, --port <port>          Application port (default: 8765)
  -c, --container <name>     Docker container name (default: clipflow-smoke-test)
  -v, --volume <volume>      Docker persistent volume name (default: clipflow-smoke-data)
  -i, --image <tag>          Docker image tag (default: ghcr.io/mirza8975/clipflow:latest)
  -f, --video <path>         Optional path to MP4 video file for full upload/clip test
  -o, --output <dir>         Output directory for downloaded clips (default: ./smoke_test_downloads)
  -k, --keep                 Keep container running after test completion
  -h, --help                 Display this help message

Examples:
  # Basic startup, health, API, and persistence test:
  ./tools/smoke_test.sh

  # Full end-to-end test with local MP4 upload and clip download:
  ./tools/smoke_test.sh --video /path/to/sample.mp4

EOF
    exit 0
}

# Parse Command Line Arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        -c|--container)
            CONTAINER_NAME="$2"
            shift 2
            ;;
        -v|--volume)
            VOLUME_NAME="$2"
            shift 2
            ;;
        -i|--image)
            IMAGE="$2"
            shift 2
            ;;
        -f|--video)
            VIDEO_PATH="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -k|--keep)
            KEEP_CONTAINER=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

BASE_URL="http://127.0.0.1:${PORT}"

pass_test() {
    TOTAL=$((TOTAL + 1))
    PASSED=$((PASSED + 1))
    echo -e "  [${GREEN}PASS${NC}] $1"
}

fail_test() {
    TOTAL=$((TOTAL + 1))
    FAILED=$((FAILED + 1))
    echo -e "  [${RED}FAIL${NC}] $1"
    if [[ -n "$2" ]]; then
        echo -e "         ${YELLOW}Details: $2${NC}"
    fi
}

info() {
    echo -e "${CYAN}==>${NC} $1"
}

cleanup() {
    if [ "$KEEP_CONTAINER" = false ]; then
        if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            info "Stopping smoke-test container '${CONTAINER_NAME}' (preserving volume '${VOLUME_NAME}')..."
            docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
            docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        fi
    else
        info "Keeping container '${CONTAINER_NAME}' running on ${BASE_URL} as requested."
    fi
}

trap cleanup EXIT

echo "======================================================================"
echo "          CLIPFLOW TERMINAL SMOKE-TEST TOOLKIT (LINUX/BASH)           "
echo "======================================================================"
echo " Target Image     : ${IMAGE}"
echo " Container Name   : ${CONTAINER_NAME}"
echo " Persistent Volume: ${VOLUME_NAME}"
echo " Port Binding     : ${PORT}"
echo " Video Upload     : ${VIDEO_PATH:-None (Skipping upload test)}"
echo "======================================================================"
echo ""

# ------------------------------------------------------------------------------
# TEST 1: Check Docker availability
# ------------------------------------------------------------------------------
info "Testing Docker daemon availability..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    pass_test "Docker CLI and daemon are running"
else
    fail_test "Docker daemon is not running or docker CLI is not installed"
    echo -e "${RED}Error: Cannot proceed without a working Docker installation.${NC}"
    exit 1
fi

# ------------------------------------------------------------------------------
# TEST 2: Pull / Verify Docker Image
# ------------------------------------------------------------------------------
info "Pulling or verifying container image: ${IMAGE}..."
if docker pull "${IMAGE}"; then
    pass_test "Successfully pulled container image '${IMAGE}'"
else
    fail_test "Failed to pull container image '${IMAGE}'"
    exit 1
fi

# ------------------------------------------------------------------------------
# TEST 3: Create Persistent Volume & Start Container
# ------------------------------------------------------------------------------
info "Setting up Docker persistent volume and container..."
if docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
    echo "  Volume '${VOLUME_NAME}' already exists (preserving data)."
else
    docker volume create "${VOLUME_NAME}" >/dev/null 2>&1
    echo "  Created persistent volume '${VOLUME_NAME}'."
fi

# Remove any old container with the exact same name
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# Start the container with local mode (CLIPFLOW_ACCOUNTS=0)
if docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${PORT}:8765" \
    -v "${VOLUME_NAME}:/data" \
    -e CLIPFLOW_PORT=8765 \
    -e CLIPFLOW_HOST=0.0.0.0 \
    -e CLIPFLOW_ACCOUNTS=0 \
    -e CLIPFLOW_TRUST_PROXY=1 \
    "${IMAGE}" >/dev/null 2>&1; then
    pass_test "Started container '${CONTAINER_NAME}' on port ${PORT}"
else
    fail_test "Failed to start Docker container '${CONTAINER_NAME}'"
    exit 1
fi

# ------------------------------------------------------------------------------
# TEST 4: Health-Check Endpoint (GET /healthz)
# ------------------------------------------------------------------------------
info "Waiting for container initialization and verifying GET /healthz..."
HEALTHY=false
for i in {1..60}; do
    RESPONSE=$(curl -s -m 3 "${BASE_URL}/healthz" 2>/dev/null || true)
    if [[ "$RESPONSE" == *"\"ok\":true"* || "$RESPONSE" == *"\"ok\": true"* ]]; then
        HEALTHY=true
        break
    fi
    sleep 1
done

if [ "$HEALTHY" = true ]; then
    pass_test "Endpoint GET /healthz returned {'ok': true}"
else
    fail_test "GET /healthz did not respond with {'ok': true} within 60s" "$RESPONSE"
    echo -e "${YELLOW}Container logs:${NC}"
    docker logs --tail 30 "${CONTAINER_NAME}"
    exit 1
fi

# ------------------------------------------------------------------------------
# TEST 5: Verify API Status Endpoint (GET /api/status)
# ------------------------------------------------------------------------------
info "Verifying API status endpoint (GET /api/status)..."
STATUS_JSON=$(curl -s -m 5 "${BASE_URL}/api/status" 2>/dev/null || true)
if [[ "$STATUS_JSON" == *"\"jobs\":"* || "$STATUS_JSON" == *"\"load\":"* ]]; then
    pass_test "Endpoint GET /api/status returned valid operational JSON"
else
    fail_test "GET /api/status failed or returned invalid response" "$STATUS_JSON"
fi

# ------------------------------------------------------------------------------
# TEST 6: Verify Jobs Listing (GET /api/jobs)
# ------------------------------------------------------------------------------
info "Verifying jobs list endpoint (GET /api/jobs)..."
JOBS_LIST=$(curl -s -m 5 "${BASE_URL}/api/jobs" 2>/dev/null || true)
if [[ "$JOBS_LIST" =~ ^\[.*\]$ ]]; then
    pass_test "Endpoint GET /api/jobs returned valid JSON array"
else
    fail_test "GET /api/jobs failed to return JSON array" "$JOBS_LIST"
fi

# ------------------------------------------------------------------------------
# TEST 7-10: Optional Video Upload, Polling & Clip Download
# ------------------------------------------------------------------------------
UPLOADED_JID=""
if [[ -n "$VIDEO_PATH" ]]; then
    if [[ ! -f "$VIDEO_PATH" ]]; then
        fail_test "Specified video file does not exist: $VIDEO_PATH"
    else
        info "Uploading local MP4 video file: ${VIDEO_PATH}..."
        UPLOAD_RESP=$(curl -s -m 120 -X POST "${BASE_URL}/api/upload" \
            -F "file=@${VIDEO_PATH}" \
            -F 'options={}' 2>/dev/null || true)
        
        # Extract Job ID using python3 or sed
        UPLOADED_JID=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('id', ''))" "$UPLOAD_RESP" 2>/dev/null || \
                       echo "$UPLOAD_RESP" | grep -o '"id":"[^"]*' | cut -d'"' -f4)

        if [[ -n "$UPLOADED_JID" ]]; then
            pass_test "Video uploaded successfully. Created Job ID: ${UPLOADED_JID}"
            
            info "Polling Job ${UPLOADED_JID} status until done or failed..."
            JOB_DONE=false
            JOB_FAILED=false
            for poll in {1..120}; do
                JOB_INFO=$(curl -s -m 5 "${BASE_URL}/api/jobs/${UPLOADED_JID}" 2>/dev/null || true)
                CURRENT_STATUS=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('status', ''))" "$JOB_INFO" 2>/dev/null || \
                                 echo "$JOB_INFO" | grep -o '"status":"[^"]*' | cut -d'"' -f4)
                
                if [[ "$CURRENT_STATUS" == "done" ]]; then
                    JOB_DONE=true
                    break
                elif [[ "$CURRENT_STATUS" == "failed" ]]; then
                    JOB_FAILED=true
                    break
                fi
                echo -ne "  Processing... Current status: [${CURRENT_STATUS:-queued}] (${poll}s)\r"
                sleep 2
            done
            echo ""

            if [ "$JOB_DONE" = true ]; then
                pass_test "Job ${UPLOADED_JID} processing completed with status: done"

                # Download clips
                mkdir -p "${OUTPUT_DIR}"
                CLIP_FILES=$(python3 -c "import sys, json; print(' '.join(c.get('file', '') for c in json.loads(sys.argv[1]).get('clips', [])))" "$JOB_INFO" 2>/dev/null || true)
                
                if [[ -n "$CLIP_FILES" ]]; then
                    DOWNLOAD_SUCCESS=true
                    for CLIP_FILE in $CLIP_FILES; do
                        TARGET_FILE="${OUTPUT_DIR}/${CLIP_FILE}"
                        curl -s -m 60 -o "$TARGET_FILE" "${BASE_URL}/media/${UPLOADED_JID}/${CLIP_FILE}?download=1"
                        
                        if [[ -f "$TARGET_FILE" && $(wc -c <"$TARGET_FILE") -gt 0 ]]; then
                            pass_test "Clip downloaded successfully: ${TARGET_FILE} ($(wc -c <"$TARGET_FILE") bytes)"
                        else
                            fail_test "Failed to download clip: ${CLIP_FILE}"
                            DOWNLOAD_SUCCESS=false
                        fi
                    done
                else
                    echo -e "  ${YELLOW}Notice: Job finished but generated 0 clips (video may have contained no loud speech/audio).${NC}"
                fi
            elif [ "$JOB_FAILED" = true ]; then
                fail_test "Job ${UPLOADED_JID} failed during processing"
            else
                fail_test "Job ${UPLOADED_JID} timed out waiting for completion"
            fi
        else
            fail_test "POST /api/upload failed to create job" "$UPLOAD_RESP"
        fi
    fi
else
    echo "  (Skipping upload & clip download tests; supply --video <path> to run end-to-end)"
fi

# ------------------------------------------------------------------------------
# TEST 11: Container Restart & Persistence Verification
# ------------------------------------------------------------------------------
info "Testing container restart and data persistence..."
docker restart "${CONTAINER_NAME}" >/dev/null 2>&1

# Wait for healthz after restart
RESTARTED_HEALTHY=false
for i in {1..30}; do
    RESPONSE=$(curl -s -m 3 "${BASE_URL}/healthz" 2>/dev/null || true)
    if [[ "$RESPONSE" == *"\"ok\":true"* || "$RESPONSE" == *"\"ok\": true"* ]]; then
        RESTARTED_HEALTHY=true
        break
    fi
    sleep 1
done

if [ "$RESTARTED_HEALTHY" = true ]; then
    pass_test "Container restarted successfully and GET /healthz is healthy"
else
    fail_test "Container failed to respond healthy after restart"
fi

# Verify data was preserved in the volume
RESTART_JOBS=$(curl -s -m 5 "${BASE_URL}/api/jobs" 2>/dev/null || true)
if [[ -n "$UPLOADED_JID" ]]; then
    if [[ "$RESTART_JOBS" == *"$UPLOADED_JID"* ]]; then
        pass_test "Job ID ${UPLOADED_JID} preserved in persistent volume across restart"
    else
        fail_test "Job ID ${UPLOADED_JID} was lost after container restart"
    fi
else
    if [[ "$RESTART_JOBS" =~ ^\[.*\]$ ]]; then
        pass_test "Database and jobs store preserved across container restart"
    else
        fail_test "Jobs store corrupted after container restart"
    fi
fi

# ------------------------------------------------------------------------------
# Summary Report
# ------------------------------------------------------------------------------
echo ""
echo "======================================================================"
echo "                         TEST RESULTS SUMMARY                         "
echo "======================================================================"
echo -e " Total Tests Run : ${TOTAL}"
echo -e " Passed          : ${GREEN}${PASSED}${NC}"
echo -e " Failed          : ${RED}${FAILED}${NC}"
echo "======================================================================"

if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}ALL SMOKE TESTS PASSED! ClipFlow is operational and ready.${NC}"
    exit 0
else
    echo -e "${RED}SOME SMOKE TESTS FAILED. Please review the output above.${NC}"
    exit 1
fi
