"""Parsing JSON back out of model output.

Every backend occasionally wraps its JSON in markdown fences or pads it
with a sentence of commentary despite being told not to. Both the
soul-generation path (autoprof/create_prof.py) and the decomposition path
(autoprof/decompose.py) need the same salvage logic, so it lives here
rather than being duplicated with two slightly different sets of bugs.
"""

import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def extract_json_object(text: str) -> dict:
    """Parse a JSON object out of raw model output.

    Tries, in order: the text as-is, the text with a wrapping markdown
    fence stripped, and finally the outermost {...} span found anywhere in
    the text (which rescues the common "Here is the JSON:\n{...}" shape).
    Raises json.JSONDecodeError if none of those parse.
    """
    text = text.strip()

    fence_match = _FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        # Re-raise from the original text so the error message quotes what
        # the model actually said, not a truncated fragment of it.
        return json.loads(text)
    return json.loads(text[start : end + 1])
