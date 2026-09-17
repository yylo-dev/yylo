# Bundled Pygments

`pygments-2.19.2-py3-none-any.whl` is the unmodified, pure-Python upstream wheel.
`pygments.json` records the PyPI download URL and SHA-256. The BSD-2-Clause license
is in `PYGMENTS-LICENSE` and inside the wheel. No transitive runtime dependencies.

The services build and ServiceInstaller copy this directory recursively. Python
loads the wheel with zipimport; users need no pip install, and rendering never
accesses the network. Only explicit built-in lexer classes are selected (no
plugin lookup). A missing or broken lexer degrades to plain text.

Maintainer upgrade procedure (separate from normal builds):

1. Review the Pygments release, license and Python compatibility.
2. Obtain the pure-Python wheel from that exact release's PyPI JSON metadata.
3. Verify its SHA-256 against the upstream metadata before replacing the wheel.
4. Update `pygments.json`, the wheel filename in `headless_presentation.py`, and
   this document; retain the upstream license.
5. Run `test_pi_service`, build, and the built-service packaging test. Confirm
   both source and dist can highlight Python/TypeScript with `python3 -S` (no
   globally installed packages). Verify raw/NO_COLOR behavior remains unchanged.

Normal builds copy the pinned bytes and require no dependency download.
