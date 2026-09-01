# Gate 0 command ledger

[`rebrand-command-ledger.csv`](rebrand-command-ledger.csv) classifies the 74
public-installer rows accepted at baseline `c45fc56`. The paired private ledger
contains 76 rows and the daemon ledger 677, producing the fixed 827-row Gate 0
boundary. This is classification of that accepted boundary, not a new headline
measurement.

Each row records its baseline file and line, a content hash, its assignment to
an identity or frozen contract, and its disposition at the Release A
implementation head. The CSV uses `{legacy-command}` and `{legacy-brand}`
placeholders so the evidence file does not create fresh ratchet entries.
`row_id` and `line_sha256` make every original row independently verifiable at
the pinned commit.

Disposition values are `removed-or-reworded`,
`converted-to-compatibility-alias`, `converted-to-permanent-floor`, and
`still-bare`. Release A adds the paired console entry but intentionally leaves
the established runbook default in place; Release C owns the default flip.
