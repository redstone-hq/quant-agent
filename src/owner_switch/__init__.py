"""The owner's switch: the one door that records Stop, Freeze or Start from his phone.

Owner ruling 2026-10-09: dashboard buttons Stop (desk fully off, positions and
broker stops kept), Freeze (no new buys, exits work), Start (clears both). The
desk already obeys those intents (src/owner_intents.py, src/owner_flags.py);
this service only RECORDS one of them through the existing writer. It reads
nothing else, touches no broker and runs no desk work. The dashboard API
(`src/api/`) stays read-only; this is the separate door src/owner_intents.py
asks for. See docs/OWNER_SWITCH.md.
"""
