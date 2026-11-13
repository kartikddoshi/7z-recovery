"""What a format module returns when it recognises a file."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Found:
    kind: str                       # human name, e.g. "Rhino 3D model"
    ext: str                        # extension without the dot
    length: int                     # bytes this file occupies, counted from the probe position
    complete: bool = True           # False: cut off or unfinished
    note: str = ""
    info: dict = field(default_factory=dict)   # format-specific metadata; "mtime" (unix time) is used for the file date
    absorb_unknown: bool = False    # the file has no usable end: unidentified bytes after it belong to it
    patches: tuple = ()             # ((offset, bytes), ...) applied when the file is written
    discard_after: int = 0          # bytes after `length` that belong to this file but are not worth writing
                                    # (e.g. the fragment of a record left by a cut)
