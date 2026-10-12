from __future__ import annotations

from dataclasses import dataclass


def encode_varint(value: int) -> bytes:
    chunks = []
    while value > 0x7F:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    chunks.append(value & 0x7F)
    return bytes(chunks)


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if index >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, index
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _skip(buf: bytes, index: int, wire: int) -> int:
    if wire == 0:
        _, index = _read_varint(buf, index)
        return index
    if wire == 1:
        return index + 8
    if wire == 2:
        length, index = _read_varint(buf, index)
        return index + length
    if wire == 5:
        return index + 4
    # wire 3/4 = deprecated groups; skip until end-group (4) or bail
    if wire == 3:
        while index < len(buf):
            key, index = _read_varint(buf, index)
            inner_wire = key & 7
            if inner_wire == 4:
                return index
            index = _skip(buf, index, inner_wire)
        raise ValueError("truncated group")
    if wire == 4:
        return index
    raise ValueError(f"unsupported wire type {wire}")


def _parse_item(buf: bytes) -> tuple[float, float] | None:
    index = 0
    price = qty = None
    while index < len(buf):
        key, index = _read_varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire != 2:
            index = _skip(buf, index, wire)
            continue
        length, index = _read_varint(buf, index)
        chunk = buf[index : index + length]
        index += length
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if field == 1:
            price = float(text)
        elif field == 2:
            qty = float(text)
    if price is None or qty is None:
        return None
    return price, qty


def _parse_depth(
    buf: bytes,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], int | None]:
    """Parse PublicLimitDepthsV3Api: asks=1, bids=2, eventType=3, version=4 (string)."""
    asks: list[tuple[float, float]] = []
    bids: list[tuple[float, float]] = []
    version: int | None = None
    index = 0
    while index < len(buf):
        key, index = _read_varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire != 2:
            index = _skip(buf, index, wire)
            continue
        length, index = _read_varint(buf, index)
        item = buf[index : index + length]
        index += length
        # Only fields 1/2 are repeated price levels; 3/4 are strings — do not parse as items.
        if field == 1:
            parsed = _parse_item(item)
            if parsed is not None:
                asks.append(parsed)
        elif field == 2:
            parsed = _parse_item(item)
            if parsed is not None:
                bids.append(parsed)
        elif field == 4:
            try:
                version = int(item.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                version = None
    return bids, asks, version


@dataclass(frozen=True, slots=True)
class MexcDepth:
    symbol: str | None
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    version: int | None = None
    # PushDataV3ApiWrapper sendTime (field 6) or createTime (field 5), epoch ms
    send_time_ms: int | None = None


def parse_mexc_limit_depth_full(buf: bytes) -> MexcDepth:
    """Decode PushDataV3ApiWrapper binary for publicLimitDepths (field 303)."""
    index = 0
    symbol: str | None = None
    channel: str | None = None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    version: int | None = None
    create_time: int | None = None
    send_time: int | None = None
    while index < len(buf):
        key, index = _read_varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire == 0 and field in (5, 6):
            value, index = _read_varint(buf, index)
            if field == 5:
                create_time = value
            else:
                send_time = value
            continue
        if wire != 2:
            index = _skip(buf, index, wire)
            continue
        length, index = _read_varint(buf, index)
        chunk = buf[index : index + length]
        index += length
        if field == 1:
            channel = chunk.decode("utf-8", errors="ignore")
        elif field == 3:
            symbol = chunk.decode("utf-8", errors="ignore")
        elif field == 303:
            bids, asks, version = _parse_depth(chunk)
    if not symbol and channel and "@" in channel:
        parts = channel.split("@")
        if len(parts) >= 3:
            symbol = parts[-2]
    return MexcDepth(symbol, bids, asks, version, send_time or create_time)


def parse_mexc_limit_depth(buf: bytes) -> tuple[str | None, list[tuple[float, float]], list[tuple[float, float]]]:
    depth = parse_mexc_limit_depth_full(buf)
    return depth.symbol, depth.bids, depth.asks
