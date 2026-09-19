#!/usr/bin/env bash
# Validate the exact topology of main-to-prod pull requests.
set -euo pipefail

usage() {
  echo "usage: validate-pr-policy.sh EVENT HEAD_REF BASE_REF ACTOR BASE_SHA HEAD_SHA MERGE_SHA HEAD_REPOSITORY REPOSITORY CURRENT_PROD_SHA" >&2
}

fail() {
  echo "::error::$*" >&2
  exit 1
}

git_object() {
  git --no-replace-objects "$@"
}

require_commit() {
  local label="$1"
  local revision="$2"
  local object_type

  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    fail "$label is not a full lowercase commit SHA."
  fi
  if ! object_type="$(git_object cat-file -t "$revision" 2>/dev/null)"; then
    fail "$label commit is missing. Fetch complete promotion history."
  fi
  if [[ "$object_type" != "commit" ]]; then
    fail "$label does not identify a commit."
  fi
}

commit_parents() {
  local revision="$1"
  git_object show -s --format=%P "$revision" 2>/dev/null
}

commit_tree() {
  local revision="$1"
  git_object rev-parse --verify "${revision}^{tree}" 2>/dev/null
}

is_on_first_parent_chain() {
  local current="$1"
  local target="$2"
  local parent_line
  local -a parents

  while [[ "$current" != "$target" ]]; do
    if ! parent_line="$(commit_parents "$current")"; then
      return 1
    fi
    parents=()
    read -r -a parents <<<"$parent_line"
    if (( ${#parents[@]} == 0 )); then
      return 1
    fi
    current="${parents[0]}"
  done
}

validate_prod_promotion() {
  local head_ref="$1"
  local base_sha="$2"
  local head_sha="$3"
  local merge_sha="$4"
  local main_requirement="${5:-exact}"
  local shallow_state tag_type bootstrap origin_main_type origin_main
  local prior_candidate parent_line base_tree prior_tree merge_tree candidate_tree
  local -a parents

  # This check must run before bot exemptions. Prod accepts only the integration branch.
  if [[ "$head_ref" != "main" ]]; then
    fail "Pull requests to prod must use main as their source branch."
  fi

  if ! shallow_state="$(git_object rev-parse --is-shallow-repository 2>/dev/null)"; then
    fail "Promotion validation requires a Git repository."
  fi
  if [[ "$shallow_state" != "false" ]]; then
    fail "Promotion validation requires complete, non-shallow Git history."
  fi

  require_commit "prod base" "$base_sha"
  require_commit "main candidate" "$head_sha"
  require_commit "synthetic merge" "$merge_sha"

  if ! tag_type="$(git_object cat-file -t refs/tags/prod-bootstrap 2>/dev/null)"; then
    fail "The prod-bootstrap tag or its object is missing."
  fi
  if [[ "$tag_type" != "tag" ]]; then
    fail "The prod-bootstrap tag must be annotated."
  fi
  if ! bootstrap="$(
    git_object rev-parse --verify 'refs/tags/prod-bootstrap^{commit}' 2>/dev/null
  )"; then
    fail "The prod-bootstrap tag does not resolve to a commit."
  fi
  require_commit "production bootstrap" "$bootstrap"

  if ! origin_main_type="$(
    git_object cat-file -t refs/remotes/origin/main 2>/dev/null
  )"; then
    fail "The origin/main commit is missing."
  fi
  if [[ "$origin_main_type" != "commit" ]]; then
    fail "The origin/main ref does not identify a commit."
  fi
  if ! origin_main="$(
    git_object rev-parse --verify 'refs/remotes/origin/main^{commit}' 2>/dev/null
  )"; then
    fail "The origin/main ref is malformed."
  fi
  if [[ "$main_requirement" == "exact" && "$origin_main" != "$head_sha" ]]; then
    fail "The pull request head SHA does not equal origin/main."
  fi
  if [[ "$main_requirement" == "descendant" ]] &&
     ! is_on_first_parent_chain "$origin_main" "$head_sha"; then
    fail "The production candidate is not in the current origin/main first-parent history."
  fi
  if [[ "$main_requirement" != "exact" && "$main_requirement" != "descendant" ]]; then
    fail "The main branch requirement is not valid."
  fi

  if [[ "$base_sha" == "$bootstrap" ]]; then
    # The bootstrap commit can itself have any number of parents.
    prior_candidate="$bootstrap"
  else
    if ! is_on_first_parent_chain "$base_sha" "$bootstrap"; then
      fail "The prod-bootstrap commit is not on the prod base first-parent chain."
    fi

    if ! parent_line="$(commit_parents "$base_sha")"; then
      fail "The prod base parents are unavailable."
    fi
    parents=()
    read -r -a parents <<<"$parent_line"
    if (( ${#parents[@]} != 2 )); then
      fail "A promoted prod base must have exactly two parents."
    fi
    prior_candidate="${parents[1]}"

    if ! base_tree="$(commit_tree "$base_sha")" ||
       ! prior_tree="$(commit_tree "$prior_candidate")"; then
      fail "The prior promotion tree is unavailable."
    fi
    if [[ "$base_tree" != "$prior_tree" ]]; then
      fail "The prior prod promotion tree differs from its main candidate tree."
    fi
  fi

  if ! git_object merge-base --is-ancestor "$prior_candidate" "$head_sha"; then
    fail "The main candidate does not descend from the prior approved candidate."
  fi

  if ! parent_line="$(commit_parents "$merge_sha")"; then
    fail "The synthetic merge parents are unavailable."
  fi
  parents=()
  read -r -a parents <<<"$parent_line"
  if (( ${#parents[@]} != 2 )) ||
     [[ "${parents[0]}" != "$base_sha" ]] ||
     [[ "${parents[1]}" != "$head_sha" ]]; then
    fail "The synthetic merge must have exactly the prod base and main candidate as parents."
  fi

  if ! merge_tree="$(commit_tree "$merge_sha")" ||
     ! candidate_tree="$(commit_tree "$head_sha")"; then
    fail "The synthetic merge or candidate tree is unavailable."
  fi
  if [[ "$merge_tree" != "$candidate_tree" ]]; then
    fail "The synthetic merge tree differs from the main candidate tree."
  fi

  echo "Production promotion topology is valid."
}

validate_prod_push() {
  local prior_prod="$1"
  local pushed_prod="$2"
  local current_prod="$3"
  local parent_line candidate
  local -a parents

  require_commit "prior prod" "$prior_prod"
  require_commit "pushed prod" "$pushed_prod"
  require_commit "current prod" "$current_prod"
  if ! is_on_first_parent_chain "$current_prod" "$pushed_prod"; then
    fail "The pushed prod commit is not in the current prod first-parent history."
  fi
  if ! parent_line="$(commit_parents "$pushed_prod")"; then
    fail "The pushed prod parents are unavailable."
  fi
  parents=()
  read -r -a parents <<<"$parent_line"
  if (( ${#parents[@]} != 2 )) || [[ "${parents[0]}" != "$prior_prod" ]]; then
    fail "The pushed prod commit must have the prior prod commit as its first parent."
  fi
  candidate="${parents[1]}"
  validate_prod_promotion "main" "$prior_prod" "$candidate" "$pushed_prod" "descendant"
}

if (( $# != 10 )); then
  usage
  exit 2
fi

event_name="$1"
head_ref="$2"
base_ref="$3"
base_sha="$5"
head_sha="$6"
merge_sha="$7"
head_repository="$8"
repository="$9"
current_prod_sha="${10}"

if [[ "$event_name" == "push" ]]; then
  if [[ "$head_ref" != "prod" || "$base_ref" != "prod" ]]; then
    echo "This push does not update prod. The promotion validation does not run."
    exit 0
  fi
  validate_prod_push "$base_sha" "$head_sha" "$current_prod_sha"
  exit 0
fi

if [[ "$event_name" != "pull_request" ]]; then
  echo "This event cannot run promotion validation."
  exit 0
fi

if [[ "$base_ref" != "prod" ]]; then
  echo "This pull request does not target prod. The promotion validation does not run."
  exit 0
fi

if [[ "$head_repository" != "$repository" ]]; then
  fail "Pull requests to prod must use the main branch in this repository."
fi

validate_prod_promotion "$head_ref" "$base_sha" "$head_sha" "$merge_sha"
