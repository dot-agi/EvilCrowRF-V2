# Offline ProtoPirate fixture

`kia_v0_synthetic.sub` contains one synthetic Kia V0 RAW frame for the SD-file
decoder path. It is generated test data; loading it with ProtoPirate's Load
File command does not transmit RF.

Expected decoded fields:

| Field | Value |
| --- | --- |
| Protocol | `Kia V0` |
| Data | `0000010ABCDEF306` |
| Bits | `61` |
| Serial | `0ABCDEF` (11259375) |
| Button | `3` |
| Counter | `1` |
| CRC | `06`, valid |

Generated with `generate_kia_v0_pulses(1, 0x0ABCDEF, 3)` from
`tools/generate_test_sub.py`. The final adjacent LOW durations are merged into
one `-2250` duration, preserving alternating HIGH/LOW pulses for the file loader.
The fixture contains 188 pulses and one frame at 433920000 Hz in its metadata.

The current `PPKiaV0` C++ decoder was compiled and fed these pulses on macOS;
it produced exactly one result matching all fields above. Uploading and loading
this file tests BLE, SD storage, file parsing, and decoding. It does not validate
the radio receive path or reception from a physical transmitter.
