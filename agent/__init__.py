"""GigBid agent package.

Console encoding is fixed here, at the one import every entry point shares.

Windows consoles default to cp1252, which cannot encode the typographic
characters a language model routinely emits - curly quotes, en dashes,
non-breaking hyphens. A complete, successful demo run crashed at the final
print because a model-written proposal contained U+2011. Every script and
evaluation runner in this repository prints model output, so each one carried
the same latent crash.

Reconfiguring stdout is the right fix rather than sanitising the text: the
whole point of the approval screen is to show the user exactly what would be
sent, and silently substituting characters would break that.
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    # `reconfigure` exists on TextIOWrapper from 3.7; a redirected or wrapped
    # stream may not have it, in which case there is nothing to fix.
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached, or not reconfigurable
            pass
