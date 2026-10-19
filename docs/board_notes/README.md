# docs/board_notes — one file per board item

Every board note lives in its own file, named for the item it explains:
`item-090.md`, `gate-item-004.md`, `decision-due-2026-10-31.md`. The
`00-preamble.md` file carries the header that explains what these notes are
and how an entry is keyed.

**Why.** These notes used to be one file, `docs/BOARD_NOTES.md`. Every branch
that touched any item touched that one file, so every branch conflicted with
every other branch and they had to be landed one at a time by hand. Split per
item, two branches working on two different items touch two different files
and cannot conflict at all.

**Reading them is unchanged.** `scripts/status_board.py::load_board_notes`
accepts this directory and reads its `*.md` files in name order, concatenated,
through exactly the parser the single file used. A note is still keyed by
`## item N` / `## gate item N` / `## decision due YYYY-MM-DD` — by number and
section, never by title.

**Writing one.** Create or edit the file for that item only. Do not add a note
for one item to another item's file. The merge driver
(`scripts/git_merge_driver_docs.sh`) routes every file here to the `notes`
resolver, so a genuine same-file collision still merges block by block.

**Retiring an item.** Unchanged, except for where the heading lives: retitle
the `## item N` heading *inside that item's own file* to
`## item N — RETIRED <date>, <reason>`. Not "CLOSED", not left untouched. Then
delete the file once nothing on the board points at it (2026-10-19): history
lives in git and `docs/INCIDENT_HISTORY.md`, and a retired note left here is
read as live doctrine by the next reader.

**Per-item budget.** `docs/WORK.md` has a whole-file cap AND a per-item byte budget (the cap divided by a chosen allowance for open items — see `tests/test_status_board.py::test_every_work_md_item_stays_within_its_per_item_budget`); an item over it moves its prose into its file here and leaves a title plus a `detail: docs/board_notes/item-NNN.md` pointer behind.
