#!/usr/bin/env bash
# Compare what onprem.caura.ai actually serves against this repository.
#
# WHY THIS EXISTS. Every other gate here measures the repository TREE. The
# ratchet counts lines in tracked files; the sentinel checks strings in tracked
# files; the parity check hashes a vendored copy against its canonical. All
# three can be green, and the gated legacy-name count can reach zero, while
# customers curl a copy of install.sh from last month. Nothing goes red, because
# nothing was looking at the channel.
#
# For a served file the check is an HTTP GET and nothing substitutes for one.
#
# WHAT IT NEEDS: nothing. Both sides are public — a public bucket in front of a
# public repo — so unlike the publisher, which needs a GCS write credential and
# therefore lives in a private repo, this can run here with no secret at all.
#
# HOW IT DECIDES. It does not diff text. It hashes the served bytes and walks
# this repo's history for the commit whose version of that file matches
# EXACTLY, then counts how many later commits touched the same path. So the
# output is "the channel is serving the state of commit X, which is N commits
# behind", which is actionable, rather than "these files differ", which is not.
#
# A served copy that matches NO commit is reported separately and is the more
# serious state: it means the channel is serving something that never came from
# this repository. That was literally true before 2026-08-25, when the object
# being served came from the private fork half.
#
# SCHEDULED, NOT A PR GATE, deliberately. Between two publishes the repository
# is SUPPOSED to be ahead of the channel — that is what an unpublished commit
# is. Failing a PR for it would make every PR red until the next release and
# teach everyone to ignore the check. On a schedule, red means "the channel has
# been behind for a day", which is the thing worth knowing.

set -euo pipefail

BASE="${SERVED_BASE:-https://onprem.caura.ai}"
REF="${COMPARE_REF:-origin/main}"

# The plain-file root objects. bundle.tar.gz is the third and is handled
# separately below: it is an archive, so it is unpacked and each member is
# compared to its own path in the repo.
ROOT_OBJECTS=(install.sh upgrade.sh)

# Loud and attributable rather than mysterious. Without this, an unresolvable
# ref makes every artefact report "does not exist at <ref>", which reads as a
# channel problem when it is a checkout problem.
if ! git rev-parse --verify "$REF" >/dev/null 2>&1; then
  echo "::error::COMPARE_REF '${REF}' does not resolve. This needs full history and the remote ref present — actions/checkout with fetch-depth: 0."
  exit 2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stale=0
unmatched=0
unreachable=0
refused=0
current=0
report=""

# Hash a blob out of history without checking it out. Prints nothing and
# returns 1 when the path did not exist at that commit.
_blob_sha() {
  local commit="$1" path="$2" blob
  blob=$(git rev-parse "${commit}:${path}" 2>/dev/null) || return 1
  git cat-file blob "$blob" | shasum -a 256 | cut -d' ' -f1
}

# The heart of it: which commit's version of $path do these bytes match?
# Walks newest-first and stops at the first match, so the answer is the most
# recent commit that could have produced them.
_match_commit() {
  local path="$1" want="$2" commit
  for commit in $(git rev-list "$REF" -- "$path"); do
    if [ "$(_blob_sha "$commit" "$path" || true)" = "$want" ]; then
      printf '%s' "$commit"
      return 0
    fi
  done
  return 1
}

_compare() {
  local label="$1" served_file="$2" path="$3"
  local served_sha head_sha commit behind subject

  served_sha=$(shasum -a 256 "$served_file" | cut -d' ' -f1)

  if ! head_sha=$(_blob_sha "$REF" "$path"); then
    report+=$(printf '\n  %-42s served, but %s does not exist at %s' "$label" "$path" "$REF")
    unmatched=$((unmatched + 1))
    return
  fi

  if [ "$served_sha" = "$head_sha" ]; then
    report+=$(printf '\n  %-42s current' "$label")
    current=$((current + 1))
    return
  fi

  if ! commit=$(_match_commit "$path" "$served_sha"); then
    report+=$(printf '\n  %-42s MATCHES NO COMMIT in %s -- served bytes did not come from this repo' "$label" "$REF")
    unmatched=$((unmatched + 1))
    return
  fi

  behind=$(git rev-list --count "${commit}..${REF}" -- "$path")
  subject=$(git log -1 --format='%s' "$commit" | cut -c1-48)
  report+=$(printf '\n  %-42s STALE  %s behind, serving %s (%s)' \
    "$label" "$behind commit(s)" "${commit:0:8}" "$subject")
  stale=$((stale + 1))
}

echo "Comparing ${BASE} against ${REF}"
echo

# ── the root objects ────────────────────────────────────────────────────────
for obj in "${ROOT_OBJECTS[@]}"; do
  if ! curl -fsSL --max-time 60 "${BASE}/${obj}" -o "${work}/${obj}"; then
    report+=$(printf '\n  %-42s UNREACHABLE at %s/%s' "$obj" "$BASE" "$obj")
    unreachable=$((unreachable + 1))
    continue
  fi
  _compare "$obj" "${work}/${obj}" "$obj"
done

# ── the bundle ──────────────────────────────────────────────────────────────
# Compared member by member rather than as an archive, because a tarball's
# bytes depend on mtimes and ownership: two builds of identical content do not
# hash the same, so the archive itself can never be compared to a commit. The
# members can.
if ! curl -fsSL --max-time 120 "${BASE}/bundle.tar.gz" -o "${work}/bundle.tar.gz"; then
  report+=$(printf '\n  %-42s UNREACHABLE at %s/bundle.tar.gz' "bundle.tar.gz" "$BASE")
  unreachable=$((unreachable + 1))
else
  # Read the member list BEFORE extracting, and refuse anything that cannot
  # correspond to a path in this repo. That is a traversal guard, but it is also
  # what makes the comparison below sound: a member named `../x` or `/etc/x` has
  # no repo path to be compared against, so it has to be reported rather than
  # silently skipped. Symlinks are refused for the same two reasons at once —
  # they are the other half of a tar-slip, and a symlink has no blob to hash.
  #
  # Not relying on tar's own defaults here in either direction. GNU tar strips
  # leading slashes and skips `..` members, bsdtar behaves differently, and the
  # runner's tar is an implementation detail this check should not depend on.
  # Two checks, and NEITHER parses a member name out of verbose output. An
  # earlier revision took the name as awk's $NF, which a member called
  # "foo bar/../../etc/passwd" walks straight through: whitespace splits the
  # name across fields, $NF is "passwd", and the traversal test sees nothing.
  # The guard was defeated by the shape of its own parser.
  #
  # (1) NAMES come from `tar -tzf`, which prints one raw member per line with
  #     no metadata columns, so a space in a name is just a space.
  # (2) TYPES come from `tar -tzvf` but only via $1, the mode string, which is
  #     the FIRST field and therefore cannot be displaced by anything in the
  #     name. The whole line is reported verbatim rather than reconstructed:
  #     refusing does not require knowing which member it was.
  #
  # (2) also generalises past symlinks. Anything that is not a regular file or
  # a directory is refused -- hard links, devices, fifos -- because the
  # question is not "is this the specific vector I thought of" but "can this
  # member correspond to a blob in a git tree", and only those two shapes can.
  bad_paths=$(tar -tzf "${work}/bundle.tar.gz" | awk '
    /^\// { print "absolute path: " $0; next }
    /(^|\/)\.\.(\/|$)/ { print "parent traversal: " $0 }
  ')
  bad_types=$(tar -tzvf "${work}/bundle.tar.gz" | awk '$1 !~ /^[-d]/ { print "not a file or directory: " $0 }')

  # A member name containing a newline would make the two listings disagree on
  # line count, which is the only way this pair can be desynchronised. Cheap to
  # notice, and it means neither listing has to be trusted to be well-formed.
  n_paths=$(tar -tzf "${work}/bundle.tar.gz" | wc -l | tr -d ' ')
  n_types=$(tar -tzvf "${work}/bundle.tar.gz" | wc -l | tr -d ' ')
  if [ "$n_paths" != "$n_types" ]; then
    bad_types="${bad_types}
member count disagrees between listings (${n_paths} vs ${n_types}) -- a name probably contains a newline"
  fi

  bad_members=$(printf '%s\n%s' "$bad_paths" "$bad_types" | sed '/^$/d')
  if [ -n "$bad_members" ]; then
    report+=$(printf '\n  %-42s REFUSED %s member(s), archive not extracted:\n%s' \
      "bundle.tar.gz" "$(printf '%s\n' "$bad_members" | wc -l | tr -d ' ')" \
      "$(printf '%s\n' "$bad_members" | sed 's/^/      /')")
    refused=$((refused + 1))
  else
    mkdir -p "${work}/bundle"
    # --no-same-owner: the served bundle's headers carry a workstation's uid and
    # group, which are meaningless here. Explicit rather than relying on tar
    # dropping them because the process is unprivileged.
    tar -xzf "${work}/bundle.tar.gz" -C "${work}/bundle" --no-same-owner
    while IFS= read -r member; do
      rel="${member#./}"
      _compare "bundle.tar.gz -> ${rel}" "${work}/bundle/${rel}" "$rel"
    done < <(cd "${work}/bundle" && find . -type f | sort)
  fi
fi

printf '%s\n\n' "$report"
printf 'current %d, stale %d, unaccounted %d, unreachable %d, refused %d\n' \
  "$current" "$stale" "$unmatched" "$unreachable" "$refused"

# Three distinct failures with three distinct remedies, so three messages
# rather than one counter. Collapsing them was the first thing the
# unreachable-channel dry run exposed: it printed "did not come from this
# repository" about a 404, which sends the reader looking for the wrong problem.
if [ "$refused" -gt 0 ]; then
  echo
  echo "::error::A served archive contains member(s) that cannot correspond to a path in this repository and was NOT extracted. An absolute path, a parent traversal or a symlink in bundle.tar.gz means the archive was not built by the documented recipe — find out what published it before trusting anything else about the channel."
fi

if [ "$unreachable" -gt 0 ]; then
  echo
  echo "::error::${unreachable} artefact(s) could not be fetched from ${BASE}. Either the channel is down or an object is missing from the bucket — check the URL before reading anything else here, because nothing below was measured."
fi

if [ "$unmatched" -gt 0 ]; then
  echo
  echo "::error::${unmatched} served artefact(s) could not be accounted for against ${REF}. A served copy that matches no commit did not come from this repository — check which repo published it before publishing over it."
fi

if [ "$stale" -gt 0 ]; then
  echo
  echo "::error::${stale} served artefact(s) are behind ${REF}. Customers are fetching an older copy than this repository holds. Publishing is a run of the on-prem release workflow in caura-ai/caura-enterprise; see its publish-installer job."
fi

if [ "$stale" -gt 0 ] || [ "$unmatched" -gt 0 ] || [ "$unreachable" -gt 0 ] || [ "$refused" -gt 0 ]; then
  exit 1
fi

echo
echo "The channel matches ${REF}."
