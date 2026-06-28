#!/usr/bin/env bash
#SBATCH --job-name=pi-smoke
#SBATCH --output=pi-smoke-%j.out
#SBATCH --error=pi-smoke-%j.err
#SBATCH --time=00:30:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
# Optional local settings: copy this template and fill outside the repo.
# #SBATCH --partition=<partition>
# #SBATCH --account=<account>

set -euo pipefail

: "${HEPH_PI_PROVIDER:?Set HEPH_PI_PROVIDER to an operator-local Pi provider alias}"
: "${HEPH_PI_MODEL:?Set HEPH_PI_MODEL to an operator-local Pi model alias}"
: "${HEPH_PI_SMOKE_LOG_DIR:=pi-smoke-logs}"

python3 scripts/pi_smoke.py --cwd "${SLURM_SUBMIT_DIR:-$PWD}" --log-dir "$HEPH_PI_SMOKE_LOG_DIR"
