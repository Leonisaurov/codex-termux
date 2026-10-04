#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"


def fail(message: str) -> None:
    raise ValueError(f"android-bundle-validator: {message}")


def parse_graph(data: bytes) -> tuple[bytes, int]:
    if len(data) < 8 + OFFSETS_SIZE + len(TRAILER):
        fail("standalone binary is too short")
    if data[:4] != b"\x7fELF":
        fail("standalone output is not ELF")
    total = struct.unpack_from("<Q", data, len(data) - 8)[0]
    if total != len(data):
        fail("standalone total_byte_count does not match file size")
    trailer_start = len(data) - 8 - len(TRAILER)
    offsets_start = trailer_start - OFFSETS_SIZE
    if data[trailer_start:trailer_start + len(TRAILER)] != TRAILER:
        fail("module graph trailer is missing or misplaced")
    byte_count = struct.unpack_from("<Q", data, offsets_start)[0]
    graph_size = byte_count + OFFSETS_SIZE + len(TRAILER)
    runtime_size = len(data) - 8 - graph_size
    if runtime_size <= 0:
        fail("standalone module graph has no runtime prefix")
    graph = data[runtime_size:len(data) - 8]
    if len(graph) != graph_size:
        fail("module graph size is inconsistent")
    graph_offsets_start = len(graph) - len(TRAILER) - OFFSETS_SIZE
    graph_byte_count = struct.unpack_from("<Q", graph, graph_offsets_start)[0]
    module_offset, module_length = struct.unpack_from("<II", graph, graph_offsets_start + 8)
    argv_offset, argv_length = struct.unpack_from("<II", graph, graph_offsets_start + 20)
    if graph_byte_count != graph_offsets_start:
        fail(f"offsets byte_count {graph_byte_count} does not match {graph_offsets_start}")
    if module_offset > graph_byte_count or module_length > graph_byte_count - module_offset:
        fail("module list points outside the graph")
    if argv_offset > graph_byte_count or argv_length > graph_byte_count - argv_offset:
        fail("compile argv points outside the graph")
    return graph, module_offset


def declared(segment: str, identifier: str) -> bool:
    return re.search(rf"\b(?:var|let|const|function|class)\s+{re.escape(identifier)}\b", segment) is not None


def vulnerabilities(graph: bytes, product: str) -> list[str]:
    string_end = struct.unpack_from("<I", graph, len(graph) - len(TRAILER) - OFFSETS_SIZE + 8)[0]
    strings = graph[:string_end]
    found: list[str] = []
    for raw_segment in strings.split(b"\0"):
        if b"undici" not in raw_segment or b"import" not in raw_segment:
            continue
        segment = raw_segment.decode("latin1")
        imports = re.finditer(rf"\bimport\s+({IDENTIFIER})\s+from\s*[\"']undici[\"']\s*;?", segment)
        exports = list(re.finditer(rf"\b{IDENTIFIER}\(\s*({IDENTIFIER})\s*,\s*\{{\s*default\s*:\s*\(\)\s*=>\s*({IDENTIFIER})\s*\}}\s*\)", segment))
        for imported in imports:
            binding = imported.group(1)
            after_import = segment[imported.start():]
            for exported in exports:
                exported_object, exported_name = exported.groups()
                if not re.search(rf"\b{re.escape(exported_name)}\s*=\s*{re.escape(binding)}\b", after_import):
                    continue
                object_pattern = re.escape(exported_object)
                if product == "opencode":
                    call_pattern = rf"__reExport\(\s*{object_pattern}\s*,\s*({IDENTIFIER})\s*\)"
                else:
                    call_pattern = rf"\b{IDENTIFIER}\(\s*{object_pattern}\s*,\s*({IDENTIFIER})\s*\)"
                candidates = []
                for call in re.finditer(call_pattern, after_import):
                    candidate = call.group(1)
                    if candidate == binding or declared(after_import, candidate):
                        continue
                    candidates.append(candidate)
                if len(candidates) > 1:
                    fail(f"ambiguous undici re-export for {binding}")
                if candidates:
                    found.append(f"{binding}->{candidates[0]}")
    return found


def validate(path: Path, product: str) -> None:
    data = path.read_bytes()
    graph, _ = parse_graph(data)
    remaining = vulnerabilities(graph, product)
    if remaining:
        fail(f"module graph contains vulnerable {product} undici re-export(s): {', '.join(remaining)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("product", choices=("opencode", "kilo"))
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    try:
        validate(args.binary, args.product)
    except (OSError, ValueError, struct.error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"android-bundle-validator: valid {args.product} {args.binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
