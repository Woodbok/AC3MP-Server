# Credits and legal notes

## Distribution / legal notes

This is an interoperability project for owners of the PC game. Only server-side code is distributed here, plus the
game settings document the server must send (the default embedded in the v1.00 executable, in `cxb/embedded/`) and
small protocol replies the game expects. No game executables or game data files are distributed; anyone running the
game needs their own legitimately owned copy, started through Ubisoft Connect as always. Nothing here bypasses DRM or
any ownership check, and the server does not emulate Ubisoft accounts. `patcher/ac3mp_patch.py` edits a player's own
copy only to stop it sending their Ubisoft password and, optionally, to name the server.

Not affiliated with or endorsed by Ubisoft. Assassin's Creed is a trademark of Ubisoft Entertainment.

## Credits / prior art

- [acb-rdv](https://github.com/michal-kapala/acb-rdv) (michal-kapala, MIT) - private server for Assassin's Creed
  Brotherhood, the same RendezVous SDK family. Its PRUDP packet layout is byte-compatible with AC3's, and its service
  suite was the first reference for many protocols here. It credits the original Ghost Recon Online backend
  implementation to Warranty Voider (GROBackendWV).
- [CXBTool](https://github.com/ACBMP/CXBTool) (ACBMP, GPLv3) - reverse-engineered the `.cxb` game settings container
  (credited there to Kamzik123). `cxb/cxb_builder.py` and `cxb/cxb_reader.py` are a Python port of its logic, which is
  why this repository is GPLv3.
- [LZO](https://www.oberhumer.com/opensource/lzo/) (Markus F.X.J. Oberhumer, GPLv2+) - `cxb/lzo2a.py` is a
  Python re-expression of its LZO2A decompressor.
- [Pretendo Network](https://pretendo.network/) - open-source NEX (Nintendo's fork of Quazal RDV) reimplementation;
  useful for the generic PRUDP transport layer.
- [kinnay/NintendoClients](https://github.com/kinnay/NintendoClients) wiki - community documentation of the PRUDP
  transport layer.
