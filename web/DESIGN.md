# Desk app design notes

**Who and what.** The insurance desk of a small Delhi hospital, on a desk PC and on a phone at the counter,
plus the accountant and the owner. The job of every screen is the same: show what is due, and move one claim
one step closer to money in the bank.

**One bold element: the arc.** Every case shows a five-station line: Case, Claim pack, Query, Payer, Bank.
Passed stations are solid black, the current one is a light-blue ring, the rest are open. It is the product's
promise drawn on every screen; everything around it stays quiet.

**Colour.** Taken from the Aeternix brand (white, black, light blue). Status colours appear only where the
domain itself speaks in them: the claim check's green, amber and red, and clocks that are overdue.

| Token | Hex | Use |
| --- | --- | --- |
| paper | #FFFFFF | background |
| ink | #000000 | text, solid stations, primary buttons |
| sky | #D5E8F7 | current station, selected row |
| azure | #17599A | links, focus ring |
| rule | #D3D9DF | hairlines and borders |
| muted | #56616C | secondary text |

**Type.** Palatino (the Aeternix content face) for case numbers and page titles; the operating system's
sans-serif for everything the desk reads quickly; tabular figures for every amount and date.

**Rules.** Sentence case everywhere. No all-caps labels. Buttons say what happens ("Submit claim", "Confirm
match"). Errors say what went wrong and how to fix it. Empty screens say what to do next.
