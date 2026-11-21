Recovers file contents from `.7z` archives that 7-Zip refuses to open ("Cannot open the file as [7z] archive", "Headers Error") because the end header is lost, truncated or zeroed.

**Install:** download the `.whl` file below and run `pip install salvage7z-*.whl`, or run `pip install git+https://github.com/kartikddoshi/7z-recovery.git`. Python 3.9 or newer, no dependencies.

**Start with** `salvage7z info damaged.7z` (read-only), then `salvage7z recover`. See the [README](https://github.com/kartikddoshi/7z-recovery#readme) for details and limitations.
