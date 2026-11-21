"""Verify exact and layout hashes for Regol-built GOLs with randomized header IDs."""
import hashlib


def crc32c(data):
    checksum = 0xffffffff
    for byte in data:
        checksum ^= byte
        for _ in range(8):
            checksum = (checksum >> 1) ^ (0x82f63b78 if checksum & 1 else 0)
    return checksum ^ 0xffffffff


def native_digests(path):
    """Hash the file and its layout, masking the randomized IDs in a valid header."""
    with path.open("rb") as stream:
        header = bytearray(stream.read(508))
        if len(header) != 508 or crc32c(header[:504]) != int.from_bytes(header[504:], "little"):
            raise ValueError("invalid native GOL header checksum")
        exact = hashlib.sha256(header)
        # See src/build/store.rs. Check the dependent CRC before masking it.
        for start, end in ((8, 16), (64, 80), (504, 508)):
            header[start:end] = bytes(end - start)
        layout = hashlib.sha256(header)
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            exact.update(chunk)
            layout.update(chunk)
    return exact.hexdigest(), layout.hexdigest()
