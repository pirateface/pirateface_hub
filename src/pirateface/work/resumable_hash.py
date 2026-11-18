"""SHA-256 checkpoint state at complete 64-byte boundaries.

This wrapper is specific to resumablesha256 1.0's C structure. It
stores only the chaining words and processed length, never the C data buffer.
"""

import struct

try:
    import resumablesha256
except ImportError:
    resumablesha256 = None

_CONTEXT = struct.Struct("@64sI4xQ8I")
_WORDS = struct.Struct(">Q8I")
_INITIAL = (0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19)


class ResumableSHA256:
    def __init__(self):
        if resumablesha256 is None:
            raise RuntimeError("resumable SHA-256 is not installed")
        self._hash = resumablesha256.sha256()
        state = self._hash.__getstate__()
        if len(state) != _CONTEXT.size:
            raise RuntimeError("unsupported resumable SHA-256 context layout")
        _, buffered, bits, *words = _CONTEXT.unpack(state)
        if buffered != 0 or bits != 0 or tuple(words) != _INITIAL:
            raise RuntimeError("unsupported resumable SHA-256 context layout")
        self._size = 0

    def update(self, data):
        self._hash.update(bytes(data))
        self._size += len(data)

    def hexdigest(self):
        return self._hash.hexdigest()

    def checkpoint(self):
        if self._size % 64:
            raise ValueError("SHA-256 checkpoint requires a 64-byte boundary")
        _, buffered, bits, *words = _CONTEXT.unpack(self._hash.__getstate__())
        if buffered != 0 or bits != self._size * 8:
            raise ValueError("invalid resumable SHA-256 state")
        return _WORDS.pack(bits, *words)

    @classmethod
    def restore(cls, state, expected_size):
        if len(state) != _WORDS.size or expected_size < 0 or expected_size % 64:
            raise ValueError("invalid SHA-256 checkpoint")
        bits, *words = _WORDS.unpack(state)
        if bits != expected_size * 8:
            raise ValueError("SHA-256 checkpoint length does not match source offset")
        result = cls()
        result._hash.__setstate__(_CONTEXT.pack(bytes(64), 0, bits, *words))
        result._size = expected_size
        return result
