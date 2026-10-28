#!/usr/bin/env bash
# Sourced by install.sh after its trusted install directory exists.
# die is provided by the caller. Proof is returned only on explicit --print
# into the installer's command substitution, with Docker logging disabled.
stage_setup_token_file() {
  local token_file="$1" temporary
  if [ -L "$token_file" ] || { [ -e "$token_file" ] && [ ! -f "$token_file" ]; }; then
    die "Host setup token must be a regular file" 2
  fi
  if [ ! -f "$token_file" ]; then
    temporary=$(mktemp "${token_file}.XXXXXX") || die "Cannot stage host setup token" 2
    if ! (if declare -F random_hex >/dev/null; then random_hex 32; else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi) > "$temporary"; then
      rm -f "$temporary"
      die "Cannot generate host setup token" 2
    fi
    # Claim the final name without overwriting an existing token. The temporary
    # file starts mode 0600, so proof is never published with broad permissions.
    if ! ln "$temporary" "$token_file" 2>/dev/null; then
      # Some operator mounts cannot create hard links. mv -n keeps an existing
      # name intact; recheck the result before reading it in either case.
      if [ ! -e "$token_file" ] && [ ! -L "$token_file" ]; then
        mv -n "$temporary" "$token_file" || {
          rm -f "$temporary"
          die "Cannot publish host setup token file" 2
        }
      fi
      if [ ! -f "$token_file" ] || [ -L "$token_file" ]; then
        rm -f "$temporary"
        die "Cannot claim host setup token file" 2
      fi
    fi
    rm -f "$temporary"
  fi
}

resolve_setup_auth_image() (
  set -o pipefail
  # --images includes dependencies even with a service filter. Select the
  # image field of the named service from Compose's canonical YAML instead.
  docker compose "$@" config platform-auth-api | awk '
    /^  platform-auth-api:$/ { active=1; next }
    active && /^  [^ ]/ { active=0 }
    active && /^    image: / {
      sub(/^    image: /, "")
      sub(/^[\047\"]/, "")
      sub(/[\047\"]$/, "")
      print
      found++
    }
    END { if (found != 1) exit 1 }
  '
)

prepare_setup_token_for_auth() {
  local token_file="$1" image="$2" helper_dir capability token_source helper_source
  helper_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  capability=$(docker image inspect --format '{{ index .Config.Labels "ai.caura.setup-proof" }}' "$image") || return 1
  if [ "$capability" != file-v1 ]; then
    echo "Selected auth image does not support mounted host proof (ai.caura.setup-proof=file-v1). Release a compatible image before using this bundle." >&2
    return 1
  fi
  if [ -L "$token_file" ] || [ ! -f "$token_file" ]; then
    echo "Host setup token must be a regular file." >&2
    return 1
  fi
  # --mount is CSV: quote the entire source field and double embedded quotes.
  token_source=${token_file//\"/\"\"}
  helper_source=${helper_dir//\"/\"\"}
  docker run --rm --pull=never --network none --read-only --log-driver none --user 0:0 --entrypoint python \
    --mount "type=bind,\"src=$token_source\",dst=/run/caura/setup-token" \
    --mount "type=bind,\"src=$helper_source/setup_token.py\",dst=/run/caura/setup_token.py,readonly" \
    "$image" /run/caura/setup_token.py "${3:-}"
}
