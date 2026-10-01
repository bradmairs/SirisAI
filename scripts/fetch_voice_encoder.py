"""Fetch the pretrained speaker encoder used for voiceprints
(app/voice/speaker.py) and convert it to a plain numpy .npz -- no PyTorch
needed, at build time or at run time.

The weights are Resemblyzer's (Apache-2.0,
https://github.com/resemble-ai/Resemblyzer): a 3-layer LSTM trained for
speaker verification, shipped inside its PyPI wheel as `pretrained.pt` in
PyTorch's legacy (pre-1.6, non-zip) serialization format. This reads that
format directly with a restricted unpickler that only ever builds numpy
arrays -- it never imports or executes anything from the file beyond the
handful of tensor/storage names it expects.

    python scripts/fetch_voice_encoder.py [output.npz]

Default output: data/voice_encoder.npz (SIRISAI_SPEAKER_ID_MODEL_PATH).
The Dockerfile runs this during the image build.
"""

import hashlib
import io
import pickle
import struct
import sys
import urllib.request
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

WHEEL_URL = (
    "https://files.pythonhosted.org/packages/d9/b5/d897d5de5b2123a1ad5d32e8e007d83f76582f5c32c60992577263bd7c7e/"
    "Resemblyzer-0.1.4-py3-none-any.whl"
)
WHEEL_SHA256 = "8f12eb2f1a9982d32e8db7856de754709b59c93a77bcf0ff536584b619a9dd1f"

_DTYPES = {
    "FloatStorage": np.float32,
    "DoubleStorage": np.float64,
    "HalfStorage": np.float16,
    "LongStorage": np.int64,
    "IntStorage": np.int32,
    "ShortStorage": np.int16,
    "CharStorage": np.int8,
    "ByteStorage": np.uint8,
    "BoolStorage": np.bool_,
}


class _Storage:
    def __init__(self, dtype: Any) -> None:
        self.dtype = dtype
        self.data: np.ndarray | None = None


class _TensorRef:
    def __init__(self, storage: _Storage, offset: int, size: tuple[int, ...], stride: tuple[int, ...]) -> None:
        self.storage, self.offset, self.size, self.stride = storage, offset, tuple(size), tuple(stride)

    def array(self) -> np.ndarray:
        assert self.storage.data is not None
        flat = self.storage.data
        if not self.size:
            return flat[self.offset : self.offset + 1].reshape(())
        itemsize = flat.dtype.itemsize
        view = np.lib.stride_tricks.as_strided(
            flat[self.offset :], shape=self.size, strides=tuple(s * itemsize for s in self.stride)
        )
        return np.array(view)


def _rebuild_tensor(storage: _Storage, offset: int, size: Any, stride: Any, *args: Any) -> _TensorRef:
    return _TensorRef(storage, offset, size, stride)


class _StorageType:
    def __init__(self, name: str) -> None:
        self.name = name


class _RestrictedUnpickler(pickle.Unpickler):
    def __init__(self, file: Any, storages: dict[str, _Storage]) -> None:
        super().__init__(file, encoding="utf-8")
        self._storages = storages

    def find_class(self, module: str, name: str) -> Any:
        if module == "torch._utils" and name in ("_rebuild_tensor", "_rebuild_tensor_v2"):
            return _rebuild_tensor
        if module == "torch" and name in _DTYPES:
            return _StorageType(name)
        if module == "collections" and name == "OrderedDict":
            return OrderedDict
        raise pickle.UnpicklingError(f"refusing to load {module}.{name}")

    def persistent_load(self, pid: Any) -> Any:
        kind, storage_type, key, _location, _size = pid[:5]
        if kind != "storage" or not isinstance(storage_type, _StorageType):
            raise pickle.UnpicklingError(f"unexpected persistent id {pid!r}")
        if key not in self._storages:
            self._storages[key] = _Storage(_DTYPES[storage_type.name])
        return self._storages[key]


def load_legacy_torch(data: bytes) -> Any:
    """Parse PyTorch's legacy serialization: magic, protocol, sys info, the
    object pickle, the storage-key list, then each storage's raw bytes."""
    stream = io.BytesIO(data)
    for _ in range(3):
        pickle.load(stream)  # magic number, protocol version, sys info -- plain ints/dicts
    storages: dict[str, _Storage] = {}
    obj = _RestrictedUnpickler(stream, storages).load()
    keys = pickle.load(stream)
    for key in keys:
        storage = storages[key]
        (count,) = struct.unpack("<q", stream.read(8))
        itemsize = np.dtype(storage.dtype).itemsize
        storage.data = np.frombuffer(stream.read(count * itemsize), dtype=storage.dtype)
    return obj


def convert(pt_bytes: bytes) -> dict[str, np.ndarray]:
    checkpoint = load_legacy_torch(pt_bytes)
    state = checkpoint["model_state"] if "model_state" in checkpoint else checkpoint
    return {name: ref.array().astype(np.float32) for name, ref in state.items() if isinstance(ref, _TensorRef)}


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "data/voice_encoder.npz")
    with urllib.request.urlopen(WHEEL_URL, timeout=120) as response:  # noqa: S310 - fixed https URL
        wheel = response.read()
    digest = hashlib.sha256(wheel).hexdigest()
    if digest != WHEEL_SHA256:
        raise SystemExit(f"Checksum mismatch for the Resemblyzer wheel: {digest}")
    with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
        pt_bytes = zf.read("resemblyzer/pretrained.pt")
    weights = convert(pt_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez(output, **weights)
    print(f"Wrote {len(weights)} tensors to {output}")


if __name__ == "__main__":
    main()
