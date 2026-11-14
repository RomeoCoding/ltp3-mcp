# Backlog

Cosmetic layout issues seen when the editor's output was opened in the GUI (2026-11-14). The files are functionally correct.
- `place()` doesn't treat the `<Summ>` Summary Report box (default at 410,555 in Artix-7) as an obstacle. New parts and wires overlap the summary text in the dual-converter and LDO files.
- In `artix7_dual_converter.ltp3`, FPGA moved from x=760 to x=800. Its free-standing `<Symb> Label`s (1.0V CORE etc.) didn't move, so they overlap the terminal labels. Either the generator shouldn't move existing parts, or `move_part` should offer to move nearby labels as well.
- Long part names overflow the title box (e.g. "LTC3621 BUCK (1.5V)"). Consider warning when a name is longer than the box width allows.
- The split-output edit changed LTC3621 to 1.5 V while the FPGA input label still reads "1.8V AUX". A semantic check could flag a load rail label that disagrees with the driving Vout.
