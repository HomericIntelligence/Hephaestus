#!/usr/bin/env bash
#SBATCH --job-name=pi-smoke
#SBATCH --output=/dev/null
#SBATCH --error=/dev/null
#SBATCH --export=NONE
#SBATCH --time=00:30:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
# Optional local settings: copy this template and fill outside the repo.
# #SBATCH --partition=<partition>
# #SBATCH --account=<account>

set -euo pipefail

: "${1:?Pass the operator-owned Pi alias config path}"
: "${2:?Pass the private Pi smoke log directory}"

pi_args=(
    --pi-alias-config "$1"
    --cwd "${SLURM_SUBMIT_DIR:-$PWD}"
    --log-dir "$2"
)
if [[ -n "${3:-}" ]]; then
    pi_args+=(--pi-dir "$3")
fi
python3 scripts/pi_smoke.py "${pi_args[@]}"
