import math
import struct
import time
from dataclasses import dataclass


BLOCK_SIZE = 512
TOTAL_BLOCKS = 256

SUPERBLOCK_BLOCK = 0
BITMAP_BLOCK = 1
META_START = 2
META_BLOCK_COUNT = 32
DATA_START = META_START + META_BLOCK_COUNT

TYPE_FILE = 1
TYPE_DIR = 2

DIR_ENTRY_EMPTY = 0
DIR_ENTRY_USED = 1
DIR_ENTRY_DELETED = 2

ENTRY_TYPE_FILE = 1
ENTRY_TYPE_DIR = 2

END_OF_CHAIN = 65535
MAX_NAME_BYTES = 24


@dataclass
class FileMeta:
    links: int
    size_bytes: int
    first_data_block: int
    last_data_block: int
    ctime: int
    mtime: int


@dataclass
class DirMeta:
    links: int
    parent_meta_block: int
    table_block: int
    num_entries: int
    flags: int
    ctime: int
    mtime: int


class FSImageBuilder:
    def __init__(self, total_blocks: int = TOTAL_BLOCKS):
        self.total_blocks = total_blocks
        self.disk = bytearray(total_blocks * BLOCK_SIZE)
        self.bitmap = bytearray(BLOCK_SIZE)

        self.file_metas: dict[int, FileMeta] = {}
        self.dir_metas: dict[int, DirMeta] = {}
        self.tables: dict[int, list[dict]] = {}
        self.data_blocks: dict[int, bytes] = {}

        self._mark_allocated(SUPERBLOCK_BLOCK)
        self._mark_allocated(BITMAP_BLOCK)

    def _mark_allocated(self, block_num: int) -> None:
        byte_index = block_num // 8
        bit_index = block_num % 8
        bit_value = 2 ** bit_index
        if not self._is_allocated(block_num):
            self.bitmap[byte_index] += bit_value

    def _is_allocated(self, block_num: int) -> bool:
        byte_index = block_num // 8
        bit_index = block_num % 8
        bit_value = 2 ** bit_index
        return (self.bitmap[byte_index] // bit_value) % 2 == 1

    def _alloc_block(self, start: int, end_exclusive: int) -> int:
        for b in range(start, end_exclusive):
            if not self._is_allocated(b):
                self._mark_allocated(b)
                return b
        raise RuntimeError("No free block found in requested region")

    def alloc_meta_block(self) -> int:
        return self._alloc_block(META_START, META_START + META_BLOCK_COUNT)

    def alloc_data_block(self) -> int:
        return self._alloc_block(DATA_START, self.total_blocks)

    def _write_block(self, block_num: int, data: bytes) -> None:
        if len(data) != BLOCK_SIZE:
            raise ValueError(f"Block {block_num} must be exactly {BLOCK_SIZE} bytes")
        start = block_num * BLOCK_SIZE
        self.disk[start:start + BLOCK_SIZE] = data

    @staticmethod
    def _hash_name(name: str) -> int:
        h = 0
        for byte in name.lower().encode("ascii"):
            h = (h * 31 + byte) % 65536
        return h

    def _init_table(self, table_block: int) -> None:
        self.tables[table_block] = [
            {
                "state": DIR_ENTRY_EMPTY,
                "entry_type": 0,
                "name_hash": 0,
                "target_meta_block": 0,
                "name_len": 0,
                "name": "",
            }
            for _ in range(16)
        ]

    def _insert_dir_entry(self, dir_meta_block: int, entry_type: int, target_meta_block: int, name: str) -> None:
        if len(name.encode("ascii")) > MAX_NAME_BYTES:
            raise ValueError(f"Name '{name}' exceeds {MAX_NAME_BYTES} bytes")

        dir_meta = self.dir_metas[dir_meta_block]
        slots = self.tables[dir_meta.table_block]
        name_hash = self._hash_name(name)
        start = name_hash % 16
        first_tombstone = None

        for i in range(16):
            idx = (start + i) % 16
            slot = slots[idx]

            if slot["state"] == DIR_ENTRY_USED:
                if slot["name"] == name:
                    raise ValueError(f"Duplicate name '{name}' in directory block {dir_meta_block}")
                continue

            if slot["state"] == DIR_ENTRY_DELETED and first_tombstone is None:
                first_tombstone = idx
                continue

            if slot["state"] == DIR_ENTRY_EMPTY:
                target_idx = first_tombstone if first_tombstone is not None else idx
                slots[target_idx] = {
                    "state": DIR_ENTRY_USED,
                    "entry_type": entry_type,
                    "name_hash": name_hash,
                    "target_meta_block": target_meta_block,
                    "name_len": len(name.encode("ascii")),
                    "name": name,
                }
                dir_meta.num_entries += 1
                dir_meta.mtime = int(time.time())
                return

        if first_tombstone is not None:
            slots[first_tombstone] = {
                "state": DIR_ENTRY_USED,
                "entry_type": entry_type,
                "name_hash": name_hash,
                "target_meta_block": target_meta_block,
                "name_len": len(name.encode("ascii")),
                "name": name,
            }
            dir_meta.num_entries += 1
            dir_meta.mtime = int(time.time())
            return

        raise RuntimeError(f"Directory table full for metadata block {dir_meta_block}")

    def create_dir(self, parent_meta_block: int, name) -> int:
        now = int(time.time())
        dir_meta_block = self.alloc_meta_block()
        table_block = self.alloc_data_block()

        self.dir_metas[dir_meta_block] = DirMeta(
            links=2,
            parent_meta_block=parent_meta_block,
            table_block=table_block,
            num_entries=0,
            flags=0,
            ctime=now,
            mtime=now,
        )
        self._init_table(table_block)

        if name is not None:
            self._insert_dir_entry(parent_meta_block, ENTRY_TYPE_DIR, dir_meta_block, name)

        return dir_meta_block

    def _store_file_data_chain(self, content: bytes) -> tuple[int, int]:
        blocks_needed = math.ceil(len(content) / 510)
        block_numbers = [self.alloc_data_block() for _ in range(blocks_needed)]

        for i, block_num in enumerate(block_numbers):
            payload = content[i * 510:(i + 1) * 510]
            payload = payload + (b"\x00" * (510 - len(payload)))
            next_block = block_numbers[i + 1] if i + 1 < len(block_numbers) else END_OF_CHAIN
            data = struct.pack("<H", next_block) + payload
            self.data_blocks[block_num] = data

        return block_numbers[0], block_numbers[-1]

    def create_file(self, parent_meta_block: int, name: str, content: bytes) -> int:
        now = int(time.time())
        meta_block = self.alloc_meta_block()

        if content:
            first_data_block, last_data_block = self._store_file_data_chain(content)
        else:
            first_data_block = END_OF_CHAIN
            last_data_block = END_OF_CHAIN

        self.file_metas[meta_block] = FileMeta(
            links=1,
            size_bytes=len(content),
            first_data_block=first_data_block,
            last_data_block=last_data_block,
            ctime=now,
            mtime=now,
        )
        self._insert_dir_entry(parent_meta_block, ENTRY_TYPE_FILE, meta_block, name)
        return meta_block

    def _serialize_superblock(self, root_meta_block: int) -> bytes:
        superblock = struct.pack(
            "<8s8H2I",
            b"JIFSv1\x00\x00",
            BLOCK_SIZE,
            self.total_blocks,
            BITMAP_BLOCK,
            META_START,
            META_BLOCK_COUNT,
            DATA_START,
            root_meta_block,
            0,
            1,
            0,
        )
        return superblock + (b"\x00" * (BLOCK_SIZE - len(superblock)))

    @staticmethod
    def _serialize_file_meta(meta: FileMeta) -> bytes:
        raw = struct.pack(
            "<HHIHHII",
            TYPE_FILE,
            meta.links,
            meta.size_bytes,
            meta.first_data_block,
            meta.last_data_block,
            meta.ctime,
            meta.mtime,
        )
        return raw + (b"\x00" * (BLOCK_SIZE - len(raw)))

    @staticmethod
    def _serialize_dir_meta(meta: DirMeta) -> bytes:
        raw = struct.pack(
            "<HHHHHHII",
            TYPE_DIR,
            meta.links,
            meta.parent_meta_block,
            meta.table_block,
            meta.num_entries,
            meta.flags,
            meta.ctime,
            meta.mtime,
        )
        return raw + (b"\x00" * (BLOCK_SIZE - len(raw)))

    @staticmethod
    def _serialize_table(entries: list[dict]) -> bytes:
        if len(entries) != 16:
            raise ValueError("Directory table must contain exactly 16 entries")

        out = bytearray()
        for entry in entries:
            name_bytes = entry["name"].encode("ascii")
            if len(name_bytes) > MAX_NAME_BYTES:
                raise ValueError("Directory entry name too long")
            name_bytes = name_bytes + (b"\x00" * (MAX_NAME_BYTES - len(name_bytes)))
            out.extend(
                struct.pack(
                    "<BBHHBB24s",
                    entry["state"],
                    entry["entry_type"],
                    entry["name_hash"],
                    entry["target_meta_block"],
                    entry["name_len"],
                    0,
                    name_bytes,
                )
            )
        return bytes(out)

    def flush_to_disk(self, disk_path: str, root_meta_block: int) -> None:
        self._write_block(SUPERBLOCK_BLOCK, self._serialize_superblock(root_meta_block))
        self._write_block(BITMAP_BLOCK, bytes(self.bitmap))

        for block_num, meta in self.file_metas.items():
            self._write_block(block_num, self._serialize_file_meta(meta))

        for block_num, meta in self.dir_metas.items():
            self._write_block(block_num, self._serialize_dir_meta(meta))

        for block_num, entries in self.tables.items():
            self._write_block(block_num, self._serialize_table(entries))

        for block_num, raw in self.data_blocks.items():
            self._write_block(block_num, raw)

        with open(disk_path, "wb") as f:
            f.write(self.disk)

    def used_blocks(self) -> list[int]:
        return [b for b in range(self.total_blocks) if self._is_allocated(b)]


def build_sample_image(disk_path: str = "disk.img") -> None:
    fs = FSImageBuilder(TOTAL_BLOCKS)

    root_meta = fs.create_dir(parent_meta_block=0, name=None)
    # Root directory parent points to itself per spec invariant.
    fs.dir_metas[root_meta].parent_meta_block = root_meta

    docs_meta = fs.create_dir(root_meta, "docs")
    home_meta = fs.create_dir(root_meta, "home")

    fs.create_file(
        root_meta,
        "hello.txt",
        b"Hello from JIFS v1 sample disk image.\n",
    )

    fs.create_file(
        docs_meta,
        "notes.txt",
        (
            b"These notes are intentionally larger than 510 bytes to demonstrate "
            b"linked data blocks in the sample disk image. " * 9
        ),
    )

    fs.create_file(
        home_meta,
        "todo.txt",
        b"- complete implementation\n- run final demo \n- get an A!!!\n",
    )

    fs.flush_to_disk(disk_path, root_meta)

    used = fs.used_blocks()
    print(f"Wrote sample filesystem image to: {disk_path}")
    print(f"Block size: {BLOCK_SIZE}, total blocks: {TOTAL_BLOCKS}")
    print(f"Used blocks ({len(used)}): {used}")


if __name__ == "__main__":
    build_sample_image("disk.img")
