"""Private, run-local transport for explicitly bound task return values."""

import json
from collections import Counter
from collections.abc import Mapping

from .model import ValidationError

MAX_RESULT_BYTES = 1 << 20
MAX_RETAINED_BYTES = 8 << 20
MAX_DEPTH = 64


def _encode(value: object, name: str) -> bytes:
    """Strict compact JSON, stopping even repeated/aliased subtrees at the cap."""
    data = bytearray()
    parents = set()

    def invalid(message):
        return ValidationError([f"bound result of task {name!r}: {message}"])

    def append(chunk):
        if len(data) + len(chunk) > MAX_RESULT_BYTES:
            raise invalid("exceeds the per-result byte limit")
        data.extend(chunk)

    def write(item, depth):
        kind = type(item)
        if depth > MAX_DEPTH:
            raise invalid("nesting too deep")
        if kind in (list, dict):
            if id(item) in parents:
                raise invalid("cyclic result")
            parents.add(id(item))
            append(b'[' if kind is list else b'{')
            for i, key in enumerate(item):
                if i:
                    append(b',')
                if kind is dict:
                    if type(key) is not str:
                        raise invalid("object keys must be strings")
                    write(key, depth + 1)
                    append(b':')
                    write(item[key], depth + 1)
                else:
                    write(key, depth + 1)
            append(b']' if kind is list else b'}')
            parents.remove(id(item))
        elif item is None or kind in (str, bool, int, float):
            if kind is str and len(item) > MAX_RESULT_BYTES - len(data):
                raise invalid("exceeds the per-result byte limit")
            try:
                token = json.dumps(item, allow_nan=False).encode('ascii')
            except ValueError:
                raise invalid("number is not representable as finite JSON") from None
            append(token)
        else:
            raise invalid("expected a JSON value")

    write(value, 0)
    return bytes(data)


class RunResults:
    def __init__(self, inputs: Mapping[str, Mapping[str, str]]):
        self.inputs = inputs
        self.remaining = Counter(p for bindings in inputs.values() for p in set(bindings.values()))
        self.values: dict[str, bytes] = {}
        self.retained = 0

    def arguments(self, consumer: str) -> dict:
        return {key: json.loads(self.values[p]) for key, p in self.inputs.get(consumer, {}).items()}

    def release_inputs(self, name: str) -> None:
        for p in set(self.inputs.get(name, {}).values()):
            self.remaining[p] -= 1
            if not self.remaining[p]:
                self.retained -= len(self.values.pop(p, b''))

    def complete(self, name: str, result: object) -> None:
        # Validate before committing either the release or publication.
        encoded = _encode(result, name) if self.remaining[name] else None
        self.commit_encoded(name, encoded)

    def commit_encoded(self, name: str, encoded: bytes | None) -> None:
        """Publish already-validated bytes (None means no bound result)."""
        if encoded is not None:
            freed = sum(len(self.values[p]) for p in set(self.inputs.get(name, {}).values())
                        if self.remaining[p] == 1)
            if self.retained - freed + len(encoded) > MAX_RETAINED_BYTES:
                raise ValidationError([f"bound result of task {name!r}: run exceeds retained byte limit"])
        self.release_inputs(name)
        if encoded is not None:
            self.values[name] = encoded
            self.retained += len(encoded)
