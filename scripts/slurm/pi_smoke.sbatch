#!/usr/bin/env bash
#SBATCH --job-name=pi-smoke
#SBATCH --output=pi-smoke-%j.out
#SBATCH --error=pi-smoke-%j.err
#SBATCH --time=00:30:00
#SBATCH --nodes=1
#SBATCH --ntasks=1

set -euo pipefail

: "${HEPH_PI_MODEL:?Set HEPH_PI_MODEL to an operator-local Pi model alias}"

python3 scripts/pi_smoke.py --cwd "${SLURM_SUBMIT_DIR:-$PWD}"
