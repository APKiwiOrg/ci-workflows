#!/bin/sh
# check-prose.sh - the prose-semicolon ban (global CLAUDE.md writing style, restated in this repo's
# AGENTS.md) in .md files: a hard rule on what you ADD, and a RATCHET on what is already written.
#
# Semicolons in CODE are fine, so the checker strips fenced code blocks (``` / ~~~), indented code
# blocks, inline code spans, YAML front matter, and HTML comments before looking, and only ever reads .md.
#
# Why a baseline and not a mass rewrite: the rule postdates the docs, so the fleet carries a four-figure
# pile of pre-existing hits (measured for #35: 33 in the template, 73 Hardpoint, 169 SpaceGame, 400
# Nullwake, 457 Ruinborne, 1634 KhaozEngine). Whole-tree mode used to report all of them and exit 1
# everywhere, which is worse than not checking at all: a guard that is red in every repo on every run
# teaches everyone to scroll past it, and then it also fails to flag the one line that IS new.
#
# Sweeping them instead was considered and rejected, because of WHERE they are. They are overwhelmingly
# historical rather than current: per-version adoption-ledger entries, dated design records, released
# player-changelog builds, and third-party asset CREDITS files. Those are the three cases worth naming,
# since each fails a different way under a mass rewrite:
#   - A RELEASED changelog entry describes a build that shipped. Editing its wording later is
#     revisionism against a record players already read.
#   - A DATED design record or per-version ledger entry is a note about what was decided then. Rewriting
#     it to today's house style quietly restates history in a document whose whole value is that it was
#     written at the time.
#   - THIRD-PARTY ATTRIBUTION text is not ours to reword at all. Reflowing a licence or credit line to
#     dodge a linter risks misstating the terms, which is a real problem in exchange for a cosmetic one.
# On top of that, none of it is going to be re-read. The fix would carry all the risk and reach no
# reader. So: freeze the count each file already has, block every new one, and let real cleanup happen
# incrementally in the files someone is editing anyway.
#
# NEXT READER: this is not an oversight waiting to be tidied. Do NOT "finish the job" by sweeping the
# changelog, the ledger, or a CREDITS file to drive the numbers to zero. Lowering a count is welcome
# when you were editing that text for its own sake, and --update then follows you down.
#
# Two rules on the tree, plus an escape hatch for the files where the rules are a category error:
#   1. A file recorded in .prose-baseline must not exceed its recorded count. It may fall freely (no
#      baseline edit needed). It may never rise.
#   2. A file NOT in the baseline must have none at all.
#
# The escape hatch: a file whose text is NOT OURS TO REWRITE (a vendored licence, a CREDITS file
# quoting an upstream notice verbatim) is not a ratchet candidate in the first place. Its count moves
# when the UPSTREAM text changes, not when our writing does, so freezing it just fails the next honest
# re-sync of that file and pressures whoever hits it into editing text they should be copying. THE TEST
# IS AUTHORSHIP, NOT SUBJECT: could we correct this sentence ourselves and still be telling the truth?
# Mark the path with an "exempt <path>" line in .prose-baseline instead of a number, with the reason on
# a "#" comment line above it: no baseline, no zero rule, no diagnostic for that path in whole-tree
# mode. Always a deliberate hand-edit (see write_baseline_header). --init and --update never write one.
#
# Modes (the local hook and the whole-tree sweep share one scanner so they cannot drift on what counts):
#   (default)   staged mode - checks only ADDED lines in the currently staged diff, and does NOT consult
#               the baseline at all. What .githooks/pre-commit runs at commit time: pre-existing
#               semicolons elsewhere in a touched file are ignored, only what you are adding is checked.
#               This is the leg that actually prevents new violations, so the ratchet deliberately does
#               not soften it. There is no PROSE_OK-style override either (pre-commit prints its own
#               --no-verify hint, and that is the only way past, same as the dash ban).
#   --tree      whole-tree mode - the ratchet above, over every tracked .md file as it stands. A manual
#               sweep and the pre-push/CI-shaped leg, so a commit that bypassed the local hook still
#               gets caught. No baseline in the repo means the ratchet is not adopted yet and this
#               no-ops, so propagating the template layer cannot redden a repo that has not run --init.
#   --preview   print what --init would freeze, writing nothing. Works with or without an existing
#               baseline, so it also answers "what would this repo freeze if it adopted the ratchet".
#   --init      write .prose-baseline from the current tree. Adoption only, refuses to overwrite.
#   --update    ratchet DOWN: lower entries whose file now has fewer, drop entries that reached zero or
#               whose file is gone. Never raises an entry and never adds one, so it cannot be used to
#               bless a new violation. Blessing one is a hand-edit of the baseline, which is a
#               deliberate act with a reviewable diff. Policy: once you have cleaned prose out of a
#               listed file, run --update in the SAME branch so the baseline follows the new low-water
#               mark instead of sitting stale (over-generous to the next regression). Carries every
#               "exempt <path>" line through unchanged, along with its preceding "#" reason comment:
#               exemption is a hand-edit the ratchet never touches, and --update rewrites the whole
#               file, so without this it would silently drop every exemption.
#
# Accuracy, since a ratchet bakes the scanner's mistakes into a committed file: of 1135 tree hits
# measured for #35, 5 were false positives, 4 of them one unbackticked `net8.0;net10.0` in
# docs/SERVER-STATUS.md replicated per repo. A false positive frozen in the baseline costs nothing (the
# number only has to not grow) and is dropped by --update the day the line is backticked.
set -eu

BASELINE=.prose-baseline
mode=${1:-staged}

# stdin = markdown, stdout = "<lineno><TAB><line>" for each prose line carrying a semicolon.
scan() {
  awk '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm == 1 { if ($0 == "---" || $0 == "...") fm = 0; next }

    {
      line = $0
      t = line
      sub(/^[ \t]+/, "", t)
      m = ""
      if (substr(t, 1, 3) == "```") m = "```"
      else if (substr(t, 1, 3) == "~~~") m = "~~~"

      if (infence) { if (m == marker) infence = 0; next }
      if (m != "") { infence = 1; marker = m; span = 0; next }
    }

    # a code span cannot contain a blank line, so a paragraph break always closes one (this bounds
    # the damage a stray unpaired backtick can do to one paragraph instead of the rest of the file)
    /^[ \t]*$/ { span = 0; next }

    # indented code block (4+ spaces)
    /^(    |\t)/ { next }

    {
      line = $0
      gsub(/<!--.*-->/, " ", line)
      # tail of an inline code span opened on an earlier line: drop everything up to its close
      if (span) {
        p = index(line, "`")
        if (p == 0) next
        line = substr(line, p + 1)
        span = 0
      }
      while (match(line, /`[^`]*`/)) line = substr(line, 1, RSTART - 1) " " substr(line, RSTART + RLENGTH)
      # an unclosed backtick opens a span that continues on the next line: drop the tail, do not guess
      p = index(line, "`")
      if (p > 0) { line = substr(line, 1, p - 1); span = 1 }
      if (index(line, ";") > 0) printf "%d\t%s\n", NR, $0
    }
  '
}

report() {
  echo "check-prose: prose semicolon found in .md (banned in shipped text: split the sentence, or use a comma, colon, or parentheses):" >&2
  cat >&2
  return 1
}

# Echo the recorded count for a path, or nothing when it is not baselined. Tolerates paths with spaces.
baseline_for() {
  [ -f "$BASELINE" ] || return 0
  awk -v want="$1" '
    /^[[:space:]]*#/ { next }
    $1 ~ /^[0-9]+$/ {
      p = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", p)
      if (p == want) { print $1; exit }
    }
  ' "$BASELINE"
}

# True (exit 0) when <path> has an "exempt" line in the baseline: no baseline check, no zero rule, no
# diagnostic for that path at all in whole-tree mode. An exempt entry wins over a numeric entry for the
# same path regardless of which appears first, since this scans the whole file for a match rather than
# stopping at the first entry seen (numeric entries keep first-wins among themselves, in baseline_for
# above). Same tolerances as baseline_for: leading whitespace allowed, path is the rest of the line
# verbatim so it may contain spaces. The "NF >= 2" guard rejects a bare "exempt" line with no path at
# all: without it, sub() has nothing to remove, p is left as the literal string "exempt", and the line
# would silently exempt any path actually named "exempt".
is_exempt() {
  [ -f "$BASELINE" ] || return 1
  hit=$(awk -v want="$1" '
    /^[[:space:]]*#/ { next }
    $1 == "exempt" && NF >= 2 {
      p = $0
      sub(/^[[:space:]]*exempt[[:space:]]+/, "", p)
      if (p == want) { print "1"; exit }
    }
  ' "$BASELINE")
  [ -n "$hit" ]
}

# Echo the standard "<path>: <n> prose semicolons, ..." sentence for one file: over its baseline (when
# $3 is non-empty) or unbaselined and non-zero (when $3 is empty). Parameter names are deliberately
# distinct from any caller's own f/n/limit/path: POSIX sh functions do not scope variables, so reusing
# those names here would silently clobber the caller's copy.
violation_line() {
  vpath=$1; vcount=$2; vlimit=$3
  if [ -n "$vlimit" ]; then
    printf '  %s: %s prose semicolons, baseline is %s (this file may improve, not regress)\n' "$vpath" "$vcount" "$vlimit"
  else
    printf '  %s: %s prose semicolons, and not in %s (an unlisted file must have none)\n' "$vpath" "$vcount" "$BASELINE"
  fi
}

# The explanation appended after any whole-tree violation listing. This is "the ratchet's own message":
# other callers surface it verbatim rather than writing their own.
print_ratchet_footer() {
  echo "" >&2
  echo "            This is a ratchet: the prose already written is frozen, it just may not get worse." >&2
  echo "            Split the sentence, or use a comma, colon, or parentheses. Semicolons in code are" >&2
  echo "            fine and are stripped before this check looks, so backtick the offender if it is code." >&2
  echo "            Do NOT drive a baseline to zero by rewriting a released changelog entry, a dated" >&2
  echo "            design record, or a third-party CREDITS file. See the header of this script for why." >&2
}

# The tracked .md files. Deliberately the same set as staged mode sees (no vendor/generated exclusion
# list): text we did not write is handled by an "exempt" line, which is explicit and reviewable, rather
# than by a path pattern that would also silently stop checking anything new added under it.
list_docs() {
  # Note the explicit if/then rather than "[ -f "$f" ] && continue": under set -e an AND-list whose left
  # side fails takes the whole list's status, which would kill this subshell on the first file present.
  git ls-files -z '*.md' 2>/dev/null | tr '\0' '\n' | while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ ! -f "$f" ]; then continue; fi
    printf '%s\n' "$f"
  done
}

# Every tracked .md file carrying at least one prose semicolon, as "<count> <path>", worst first. The
# single definition of "what gets frozen", shared by --init and --preview so a preview cannot disagree
# with what --init then writes.
with_hits() {
  list_docs | while IFS= read -r f; do
    n=$(scan < "$f" | wc -l | tr -d ' ')
    if [ "$n" -gt 0 ]; then printf '%s %s\n' "$n" "$f"; fi
  done | sort -rn
}

write_baseline_header() {
  cat <<'EOF'
# .prose-baseline - frozen prose-semicolon counts for .md files written before the ban.
#
# Format: "<count> <path>", worst first. Managed by scripts/check-prose.sh:
#   scripts/check-prose.sh --preview  what --init would freeze, writes nothing
#   scripts/check-prose.sh --init     create this file (adoption only)
#   scripts/check-prose.sh --update   ratchet down after cleaning some out
#
# A listed file may drop its count freely without editing this file. It may not raise it. A file NOT
# listed here must have none at all. Ratchet an entry down with --update once real work has landed.
# Adding an entry or raising a number is a deliberate hand-edit, on purpose: it should show up in
# review as "we are blessing new prose semicolons in shipped text".
#
# These counts are HISTORY, not a cleanup queue. They sit in released changelog builds, dated design
# records, per-version ledger entries, and third-party attribution files. Rewriting that text to drive
# a number down is revisionism at best and a misstated licence at worst. Read the header of
# scripts/check-prose.sh before you decide to sweep any of it.
#
# A path can instead be marked "exempt <path>" in place of a number, for a file whose text is not ours
# to rewrite (a vendored licence, a CREDITS file quoting an upstream notice verbatim). Its count moves
# when the upstream text changes rather than when our writing does, so a frozen number would just fail
# the next honest re-sync. Put the reason on a "#" comment line right above the exempt line, since the
# line itself is the path verbatim (paths may contain spaces) and cannot also carry a trailing comment:
#
#   # upstream licence text, copied verbatim, not ours to reword
#   exempt assets/thirdparty/CREDITS.md
#
# Exemption is always a deliberate hand-edit: --init and --update never write an exempt line, only a
# human adds one, and --update carries every exempt line (and its comment) through unchanged when it
# rewrites this file. An exempt entry wins over a numeric entry for the same path, whichever appears
# first, because exemption is the more explicit statement.
#
# Staged mode (the pre-commit hook) never reads this file. What you ADD must be clean regardless of
# what the file already carries.
#
# This file is per-repo and is NOT part of the verbatim template layer.

EOF
}

case "$mode" in
  --tree)
    # No baseline at all means this repo has not adopted the ratchet yet, so pass. This is what keeps
    # propagating the template layer from reddening every game the moment it lands: adoption is "run
    # --init and commit the result", a separate deliberate step. Same contract as check-file-size.sh.
    if [ ! -f "$BASELINE" ]; then
      echo "check-prose: no $BASELINE in this repo, prose ratchet not adopted yet (skipping)." >&2
      echo "            Adopt it with: scripts/check-prose.sh --init && git add $BASELINE" >&2
      exit 0
    fi

    violations=$(mktemp)
    improved=$(mktemp)
    trap 'rm -f "$violations" "$improved"' EXIT

    list_docs | while IFS= read -r f; do
      if is_exempt "$f"; then continue; fi
      hits=$(scan < "$f")
      if [ -z "$hits" ]; then n=0; else n=$(printf '%s\n' "$hits" | wc -l | tr -d ' '); fi
      limit=$(baseline_for "$f")
      if [ -n "$limit" ]; then
        if [ "$n" -gt "$limit" ]; then
          violation_line "$f" "$n" "$limit" >> "$violations"
          printf '%s\n' "$hits" | awk -F'\t' -v f="$f" '{ printf "    %s:%s: %s\n", f, $1, substr($0, index($0, "\t") + 1) }' >> "$violations"
        elif [ "$n" -lt "$limit" ]; then
          printf '  %s: %s prose semicolons, baseline is %s\n' "$f" "$n" "$limit" >> "$improved"
        fi
      elif [ "$n" -gt 0 ]; then
        violation_line "$f" "$n" "" >> "$violations"
        printf '%s\n' "$hits" | awk -F'\t' -v f="$f" '{ printf "    %s:%s: %s\n", f, $1, substr($0, index($0, "\t") + 1) }' >> "$violations"
      fi
    done

    if [ -s "$violations" ]; then
      echo "check-prose: .md files carry more prose semicolons than $BASELINE allows:" >&2
      cat "$violations" >&2
      print_ratchet_footer
      exit 1
    fi

    # Informational only, so nothing is ever blocked for the good outcome. Tells you the ratchet has
    # slack to take up.
    if [ -s "$improved" ]; then
      echo "check-prose: these files now carry fewer prose semicolons than their baseline:" >&2
      cat "$improved" >&2
      echo "            Run 'scripts/check-prose.sh --update' to ratchet the baseline down." >&2
    fi
    ;;

  staged)
    # Unchanged by the ratchet, and deliberately so: this is the leg that stops the pile growing. It
    # never reads $BASELINE, so a baselined file still cannot ADD a semicolon in a new line.
    fail=0
    files=$(git diff --cached --name-only --diff-filter=ACMR -- '*.md' 2>/dev/null || true)
    [ -n "$files" ] || exit 0
    out=$(
      printf '%s\n' "$files" | while IFS= read -r f; do
        [ -n "$f" ] || continue
        added=$(git diff --cached --unified=0 -- "$f" 2>/dev/null | awk '
          /^@@ / {
            n = $3
            sub(/^\+/, "", n)
            c = n
            len = 1
            if (index(n, ",")) { split(n, a, ","); c = a[1]; len = a[2] }
            for (i = 0; i < len; i++) print c + i
          }')
        [ -n "$added" ] || continue
        git show ":$f" 2>/dev/null | scan | awk -F'\t' -v add=" $(printf '%s' "$added" | tr '\n' ' ') " -v f="$f" '
          { if (index(add, " " $1 " ")) printf "  %s:%s: %s\n", f, $1, substr($0, index($0, "\t") + 1) }'
      done
    )
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | report || fail=1
    fi
    exit $fail
    ;;

  --init)
    if [ -f "$BASELINE" ]; then
      echo "check-prose: $BASELINE already exists. --init is for adoption only." >&2
      echo "            To ratchet it down after cleaning a file, use --update." >&2
      exit 2
    fi
    tmp=$(mktemp)
    write_baseline_header > "$tmp"
    with_hits >> "$tmp"
    mv "$tmp" "$BASELINE"
    n=$(grep -cE '^[0-9]+ ' "$BASELINE" || true)
    total=$(awk '/^[0-9]+ /{ s += $1 } END { print s + 0 }' "$BASELINE")
    echo "check-prose: wrote $BASELINE with $n file(s) carrying $total prose semicolon(s)."
    ;;

  --update)
    if [ ! -f "$BASELINE" ]; then
      echo "check-prose: no $BASELINE to update. Create one with --init." >&2
      exit 2
    fi
    tmp=$(mktemp)
    write_baseline_header > "$tmp"
    # Walk the EXISTING entries only. A file cleaned to zero or deleted drops out, one that improved
    # but still has hits gets its lower number. Nothing is ever added or raised.
    grep -E '^[0-9]+ ' "$BASELINE" 2>/dev/null | while IFS= read -r entry; do
      recorded=${entry%% *}
      path=${entry#* }
      [ -f "$path" ] || continue
      n=$(scan < "$path" | wc -l | tr -d ' ')
      [ "$n" -gt 0 ] || continue
      if [ "$n" -lt "$recorded" ]; then
        printf '%s %s\n' "$n" "$path"
      else
        printf '%s %s\n' "$recorded" "$path"
      fi
    done | sort -rn >> "$tmp"
    # Exempt lines are a hand-edit the ratchet never touches: carry every "exempt <path>" line through
    # unchanged, along with any "#" comment line(s) immediately above it (the reason, by convention).
    # This rewrites the whole file like the numeric pass above, so without this step --update would
    # silently drop every exemption on its next run. A run of "#" lines not immediately followed by an
    # exempt line (an unrelated comment) is discarded, and a blank line clears the buffer.
    #
    # The LEADING comment block is the header, and it must be skipped rather than buffered.
    # write_baseline_header has already regenerated it into $tmp above, so buffering it here would emit
    # a SECOND copy whenever the first entry in the file is an exempt line, which is exactly where a
    # human puts the first one. The header ends at the first blank line (write_baseline_header emits
    # one for this purpose) or at the first entry, whichever comes first. Same guard, and the same past
    # bug, as check-file-size.sh --update.
    awk '
      !past_header && /^[[:space:]]*$/ { past_header = 1; next }
      !past_header && /^[[:space:]]*#/ { next }
      { past_header = 1 }
      /^[[:space:]]*#/ { buf = buf $0 "\n"; next }
      /^[[:space:]]*exempt[[:space:]]+/ { printf "%s%s\n", buf, $0; buf = ""; next }
      { buf = "" }
    ' "$BASELINE" >> "$tmp"
    mv "$tmp" "$BASELINE"
    n=$(grep -cE '^[0-9]+ ' "$BASELINE" || true)
    x=$(grep -cE '^[[:space:]]*exempt[[:space:]]+' "$BASELINE" || true)
    total=$(awk '/^[0-9]+ /{ s += $1 } END { print s + 0 }' "$BASELINE")
    echo "check-prose: ratcheted $BASELINE down to $n file(s) carrying $total prose semicolon(s), $x exempt."
    ;;

  --preview)
    with_hits
    ;;

  *)
    echo "check-prose: unknown mode '$mode' (expected: staged, --tree, --preview, --init, --update)" >&2
    exit 2
    ;;
esac
