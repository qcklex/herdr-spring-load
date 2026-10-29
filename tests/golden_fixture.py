"""Golden fixtures keep scratch paths as the placeholder "$TEMP". They are
substituted in the parsed data, never in the JSON text: a Windows path's
backslashes are escapes in JSON text ("Invalid \\escape")."""
import json
from pathlib import Path

def with_temp(value, temp):
    """`value` with "$TEMP" in every string replaced by `temp` (forward slashes)."""
    temp = Path(temp).as_posix()
    if isinstance(value, str):
        return value.replace('$TEMP', temp)
    if isinstance(value, list):
        return [with_temp(v, temp) for v in value]
    if isinstance(value, dict):
        return {with_temp(k, temp): with_temp(v, temp) for k, v in value.items()}
    return value

def load_golden(path, temp):
    return with_temp(json.loads(Path(path).read_text(encoding='utf-8')), temp)
