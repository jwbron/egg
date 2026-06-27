"""Console entry point for the sandbox container: ``python3 -m entrypoint``.

The Dockerfile ENTRYPOINT invokes the package via ``python3 -m entrypoint``
(the source file became a sub-package in #3312, slice 9). ``main()`` lives in
the package barrel; this module just dispatches to it.
"""

from __future__ import annotations

from entrypoint import main

if __name__ == "__main__":
    main()
