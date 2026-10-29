import os
import struct
import subprocess
import sys
import time

import Disk

DISK_FILE="disk.img"
BLOCK_SIZE=512
END_OF_CHAIN=65535

SUPERBLOCK_BLOCK=0
SUPERBLOCK_FORMAT="<8s8H2I"
DIR_ENTRY_FORMAT="<BBHHBB24s"
MAX_NAME_BYTES=24

TYPE_FILE=1
TYPE_DIR=2
ENTRY_TYPE_FILE=1
ENTRY_TYPE_DIR=2
DIR_ENTRY_EMPTY=0
DIR_ENTRY_USED=1
DIR_ENTRY_DELETED=2

META_TYPES={
  TYPE_FILE: "TYPE_FILE",
  TYPE_DIR: "TYPE_DIR"
}

DIR_ENTRY_STATES={
  "EMPTY": DIR_ENTRY_EMPTY,
  "USED": DIR_ENTRY_USED,
  "DELETED": DIR_ENTRY_DELETED
}

DIR_ENTRY_TYPES={
  ENTRY_TYPE_FILE: "TYPE_FILE",
  ENTRY_TYPE_DIR: "TYPE_DIR"
}

DISK=Disk.Disk(DISK_FILE, BLOCK_SIZE)


class metadata_block:
  def __init__(self):
    self.type=None
    self.links=0
    self.size=0
    self.first_data_block=END_OF_CHAIN
    self.last_data_block=END_OF_CHAIN
    self.parent_meta_block=0
    self.table_block=0
    self.num_entries=0
    self.flags=0
    self.ctime=0
    self.mtime=0


def read_superblock():
  raw=DISK.readBlock(SUPERBLOCK_BLOCK)
  unpacked=struct.unpack(SUPERBLOCK_FORMAT, raw[:struct.calcsize(SUPERBLOCK_FORMAT)])

  magic=unpacked[0].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
  return {
    "magic": magic,
    "block_size": unpacked[1],
    "total_blocks": unpacked[2],
    "bitmap_start": unpacked[3],
    "meta_start": unpacked[4],
    "meta_block_count": unpacked[5],
    "data_start": unpacked[6],
    "root_meta_block": unpacked[7],
    "flags": unpacked[8],
    "mount_count": unpacked[9],
    "checksum": unpacked[10]
  }


def is_block_allocated(block_num):
  byte_index=block_num//8
  bit_index=block_num%8
  bit_value=2**bit_index
  return (BITMAP[byte_index]//bit_value)%2 == 1


def hash_name(name):
  h=0
  for b in name.lower().encode("ascii"):
    h=(h*31+b)%65536
  return h


def read_metadata_block(block_num):
  raw=DISK.readBlock(block_num)
  meta_type=int.from_bytes(raw[0:2], byteorder="little")
  if meta_type not in META_TYPES:
    return None

  out=metadata_block()
  out.type=META_TYPES[meta_type]

  if out.type == "TYPE_FILE":
    _, out.links, out.size, out.first_data_block, out.last_data_block, out.ctime, out.mtime = struct.unpack(
      "<HHIHHII", raw[:20]
    )
  elif out.type == "TYPE_DIR":
    _, out.links, out.parent_meta_block, out.table_block, out.num_entries, out.flags, out.ctime, out.mtime = struct.unpack(
      "<HHHHHHII", raw[:20]
    )

  return out


def parse_dir_table(table_block):
  raw=DISK.readBlock(table_block)
  out=[]
  entry_size=struct.calcsize(DIR_ENTRY_FORMAT)

  for i in range(BLOCK_SIZE//entry_size):
    offset=i*entry_size
    state, entry_type, name_hash, target_meta_block, name_len, _, name_raw = struct.unpack_from(DIR_ENTRY_FORMAT, raw, offset)
    name=name_raw[:name_len].decode("ascii")
    out.append({
      "state": state,
      "entry_type": entry_type,
      "hash": name_hash,
      "target_meta_block": target_meta_block,
      "name_len": name_len,
      "name": name
    })

  return out


def list_dir_entries(dir_meta_block):
  dir_meta=read_metadata_block(dir_meta_block)
  if dir_meta is None or dir_meta.type != "TYPE_DIR":
    return []

  entries=[]
  for entry in parse_dir_table(dir_meta.table_block):
    if entry["state"] != DIR_ENTRY_STATES["USED"]:
      continue

    target_meta=read_metadata_block(entry["target_meta_block"])
    if target_meta is None:
      continue

    entry_type=DIR_ENTRY_TYPES.get(entry["entry_type"], "UNKNOWN")
    size=target_meta.size if entry_type == "TYPE_FILE" else target_meta.num_entries
    entries.append({
      "type": entry_type,
      "size": size,
      "name": entry["name"],
      "target_meta_block": entry["target_meta_block"]
    })
  return entries


def lookup_dir_entry(dir_meta_block, name):
  dir_meta=read_metadata_block(dir_meta_block)
  if dir_meta is None or dir_meta.type != "TYPE_DIR":
    return None

  entries=parse_dir_table(dir_meta.table_block)
  name_hash=hash_name(name)
  start=name_hash%len(entries)

  for i in range(len(entries)):
    idx=(start+i)%len(entries)
    entry=entries[idx]
    if entry["state"] == DIR_ENTRY_STATES["EMPTY"]:
      return None
    if entry["state"] != DIR_ENTRY_STATES["USED"]:
      continue
    if entry["hash"] == name_hash and entry["name"] == name:
      return entry
  return None


def resolve_path(curr_meta_block, path):
  if path == "" or path == ".":
    return curr_meta_block

  block=SUPERBLOCK["root_meta_block"] if path.startswith("/") else curr_meta_block
  parts=path.split("/")

  for part in parts:
    if part in ("", "."):
      continue
    curr_meta=read_metadata_block(block)
    if curr_meta is None or curr_meta.type != "TYPE_DIR":
      return None
    if part == "..":
      block=curr_meta.parent_meta_block
      continue
    entry=lookup_dir_entry(block, part)
    if entry is None:
      return None
    block=entry["target_meta_block"]

  return block


def read_file_bytes(file_meta):
  if file_meta.type != "TYPE_FILE":
    return None

  if file_meta.size == 0 or file_meta.first_data_block == END_OF_CHAIN:
    return b""

  data=bytearray()
  remaining=file_meta.size
  curr_block=file_meta.first_data_block
  visited=set()

  while curr_block != END_OF_CHAIN and remaining > 0:
    if curr_block in visited:
      print("corrupt file chain detected")
      break
    visited.add(curr_block)

    raw=DISK.readBlock(curr_block)
    next_block=int.from_bytes(raw[0:2], byteorder="little")
    to_take=min(remaining, BLOCK_SIZE-2)
    data.extend(raw[2:2+to_take])
    remaining-=to_take
    curr_block=next_block

  return bytes(data)


def mark_block_allocated(block_num):
  byte_index=block_num//8
  bit_index=block_num%8
  bit_value=2**bit_index
  if not is_block_allocated(block_num):
    BITMAP[byte_index]+=bit_value
  DISK.writeBlock(SUPERBLOCK["bitmap_start"], bytes(BITMAP))


def mark_block_free(block_num):
  byte_index=block_num//8
  bit_index=block_num%8
  bit_value=2**bit_index
  if is_block_allocated(block_num):
    BITMAP[byte_index]-=bit_value
  DISK.writeBlock(SUPERBLOCK["bitmap_start"], bytes(BITMAP))


def alloc_block(start, end_exclusive):
  for block_num in range(start, end_exclusive):
    if not is_block_allocated(block_num):
      mark_block_allocated(block_num)
      return block_num
  return None


def free_block(block_num):
  DISK.writeBlock(block_num, b"\x00"*BLOCK_SIZE)
  mark_block_free(block_num)


def write_metadata_block(block_num, meta):
  if meta.type == "TYPE_FILE":
    raw=struct.pack(
      "<HHIHHII",
      TYPE_FILE,
      meta.links,
      meta.size,
      meta.first_data_block,
      meta.last_data_block,
      meta.ctime,
      meta.mtime
    )
  elif meta.type == "TYPE_DIR":
    raw=struct.pack(
      "<HHHHHHII",
      TYPE_DIR,
      meta.links,
      meta.parent_meta_block,
      meta.table_block,
      meta.num_entries,
      meta.flags,
      meta.ctime,
      meta.mtime
    )
  else:
    return False

  DISK.writeBlock(block_num, raw + (b"\x00"*(BLOCK_SIZE-len(raw))))
  return True


def write_dir_table(table_block, entries):
  out=bytearray(BLOCK_SIZE)
  entry_size=struct.calcsize(DIR_ENTRY_FORMAT)

  for i, entry in enumerate(entries):
    name_bytes=entry["name"].encode("ascii")
    if len(name_bytes) > MAX_NAME_BYTES:
      return False
    name_padded=name_bytes + (b"\x00"*(MAX_NAME_BYTES-len(name_bytes)))
    struct.pack_into(
      DIR_ENTRY_FORMAT,
      out,
      i*entry_size,
      entry["state"],
      entry["entry_type"],
      entry["hash"],
      entry["target_meta_block"],
      entry["name_len"],
      0,
      name_padded
    )

  DISK.writeBlock(table_block, bytes(out))
  return True


def split_parent_and_name(target_path):
  if target_path is None:
    return None, None

  stripped=target_path.rstrip("/")
  if stripped == "":
    return None, None

  if "/" not in stripped:
    return ".", stripped

  parent, name=stripped.rsplit("/", 1)
  if parent == "":
    parent="/"
  return parent, name


def probe_dir_entry(entries, name_hash, name):
  start=name_hash%len(entries)
  first_tombstone=None

  for i in range(len(entries)):
    idx=(start+i)%len(entries)
    entry=entries[idx]

    if entry["state"] == DIR_ENTRY_STATES["USED"]:
      if entry["hash"] == name_hash and entry["name"] == name:
        return idx, None
      continue

    if entry["state"] == DIR_ENTRY_STATES["DELETED"] and first_tombstone is None:
      first_tombstone=idx
      continue

    if entry["state"] == DIR_ENTRY_STATES["EMPTY"]:
      return None, first_tombstone if first_tombstone is not None else idx

  if first_tombstone is not None:
    return None, first_tombstone
  return None, None


def free_data_chain(first_data_block):
  curr_block=first_data_block
  visited=set()

  while curr_block != END_OF_CHAIN:
    if curr_block in visited:
      print("corrupt file chain detected")
      return False
    visited.add(curr_block)
    raw=DISK.readBlock(curr_block)
    next_block=int.from_bytes(raw[0:2], byteorder="little")
    free_block(curr_block)
    curr_block=next_block
  return True


def mkdir(curr_meta_block, target_path):
  parent_path, name=split_parent_and_name(target_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid target directory")
    return

  name_bytes=name.encode("ascii")

  if len(name_bytes) > MAX_NAME_BYTES:
    print(f"name too long (max {MAX_NAME_BYTES} bytes)")
    return

  parent_block=resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid parent directory")
    return

  parent_meta=read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("parent is not a directory")
    return

  entries=parse_dir_table(parent_meta.table_block)
  name_hash=hash_name(name)
  existing_idx, target_idx=probe_dir_entry(entries, name_hash, name)

  if existing_idx is not None:
    print("target already exists")
    return
  if target_idx is None:
    print("directory full")
    return

  new_meta_block=alloc_block(SUPERBLOCK["meta_start"], SUPERBLOCK["meta_start"]+SUPERBLOCK["meta_block_count"])
  if new_meta_block is None:
    print("no free metadata blocks")
    return

  new_table_block=alloc_block(SUPERBLOCK["data_start"], SUPERBLOCK["total_blocks"])
  if new_table_block is None:
    DISK.writeBlock(new_meta_block, b"\x00"*BLOCK_SIZE)
    mark_block_free(new_meta_block)
    print("no free data blocks for directory table")
    return

  empty_entries=[{
    "state": DIR_ENTRY_STATES["EMPTY"],
    "entry_type": 0,
    "hash": 0,
    "target_meta_block": 0,
    "name_len": 0,
    "name": ""
  } for _ in range(BLOCK_SIZE//struct.calcsize(DIR_ENTRY_FORMAT))]

  write_dir_table(new_table_block, empty_entries)

  now=int(time.time())
  new_dir_meta=metadata_block()
  new_dir_meta.type="TYPE_DIR"
  new_dir_meta.links=2
  new_dir_meta.parent_meta_block=parent_block
  new_dir_meta.table_block=new_table_block
  new_dir_meta.num_entries=0
  new_dir_meta.flags=0
  new_dir_meta.ctime=now
  new_dir_meta.mtime=now

  write_metadata_block(new_meta_block, new_dir_meta)

  entries[target_idx]={
    "state": DIR_ENTRY_STATES["USED"],
    "entry_type": ENTRY_TYPE_DIR,
    "hash": name_hash,
    "target_meta_block": new_meta_block,
    "name_len": len(name_bytes),
    "name": name
  }
  write_dir_table(parent_meta.table_block, entries)

  parent_meta.num_entries+=1
  parent_meta.mtime=now
  write_metadata_block(parent_block, parent_meta)
  print("directory created")


def touch(curr_meta_block, target_path):
  parent_path, name=split_parent_and_name(target_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid target file")
    return

  name_bytes=name.encode("ascii")

  if len(name_bytes) > MAX_NAME_BYTES:
    print(f"name too long (max {MAX_NAME_BYTES} bytes)")
    return

  parent_block=resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid parent directory")
    return

  parent_meta=read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("parent is not a directory")
    return

  entries=parse_dir_table(parent_meta.table_block)
  name_hash=hash_name(name)
  existing_idx, insert_idx=probe_dir_entry(entries, name_hash, name)

  if existing_idx is not None:
    existing=entries[existing_idx]
    if existing["entry_type"] == ENTRY_TYPE_FILE:
      print("file already exists")
    else:
      print("target already exists")
    return

  if insert_idx is None:
    print("directory full")
    return

  new_meta_block=alloc_block(SUPERBLOCK["meta_start"], SUPERBLOCK["meta_start"]+SUPERBLOCK["meta_block_count"])
  if new_meta_block is None:
    print("no free metadata blocks")
    return

  now=int(time.time())
  file_meta=metadata_block()
  file_meta.type="TYPE_FILE"
  file_meta.links=1
  file_meta.size=0
  file_meta.first_data_block=END_OF_CHAIN
  file_meta.last_data_block=END_OF_CHAIN
  file_meta.ctime=now
  file_meta.mtime=now
  write_metadata_block(new_meta_block, file_meta)

  entries[insert_idx]={
    "state": DIR_ENTRY_STATES["USED"],
    "entry_type": ENTRY_TYPE_FILE,
    "hash": name_hash,
    "target_meta_block": new_meta_block,
    "name_len": len(name_bytes),
    "name": name
  }
  if not write_dir_table(parent_meta.table_block, entries):
    free_block(new_meta_block)
    print("failed to update directory table")
    return

  parent_meta.num_entries+=1
  parent_meta.mtime=now
  write_metadata_block(parent_block, parent_meta)
  print("file created")


def write(curr_meta_block, target_path, text):
  parent_path, name=split_parent_and_name(target_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid target file")
    return

  name_bytes=name.encode("ascii")

  if len(name_bytes) > MAX_NAME_BYTES:
    print(f"name too long (max {MAX_NAME_BYTES} bytes)")
    return

  payload=text.encode("ascii")

  parent_block=resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid parent directory")
    return

  parent_meta=read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("parent is not a directory")
    return

  entries=parse_dir_table(parent_meta.table_block)
  name_hash=hash_name(name)
  existing_idx, insert_idx=probe_dir_entry(entries, name_hash, name)

  now=int(time.time())
  created_new_file=False

  if existing_idx is not None:
    entry=entries[existing_idx]
    if entry["entry_type"] != ENTRY_TYPE_FILE:
      print("target is not a file")
      return
    file_meta_block=entry["target_meta_block"]
    file_meta=read_metadata_block(file_meta_block)
    if file_meta is None or file_meta.type != "TYPE_FILE":
      print("invalid file metadata")
      return
  else:
    if insert_idx is None:
      print("directory full")
      return

    file_meta_block=alloc_block(SUPERBLOCK["meta_start"], SUPERBLOCK["meta_start"]+SUPERBLOCK["meta_block_count"])
    if file_meta_block is None:
      print("no free metadata blocks")
      return

    file_meta=metadata_block()
    file_meta.type="TYPE_FILE"
    file_meta.links=1
    file_meta.size=0
    file_meta.first_data_block=END_OF_CHAIN
    file_meta.last_data_block=END_OF_CHAIN
    file_meta.ctime=now
    file_meta.mtime=now
    write_metadata_block(file_meta_block, file_meta)

    entries[insert_idx]={
      "state": DIR_ENTRY_STATES["USED"],
      "entry_type": ENTRY_TYPE_FILE,
      "hash": name_hash,
      "target_meta_block": file_meta_block,
      "name_len": len(name_bytes),
      "name": name
    }
    if not write_dir_table(parent_meta.table_block, entries):
      free_block(file_meta_block)
      print("failed to update directory table")
      return

    parent_meta.num_entries+=1
    parent_meta.mtime=now
    write_metadata_block(parent_block, parent_meta)
    created_new_file=True

  if file_meta.first_data_block != END_OF_CHAIN:
    free_data_chain(file_meta.first_data_block)

  block_count=(len(payload)+(BLOCK_SIZE-3))//(BLOCK_SIZE-2)
  data_blocks=[]
  for _ in range(block_count):
    new_block=alloc_block(SUPERBLOCK["data_start"], SUPERBLOCK["total_blocks"])
    if new_block is None:
      for b in data_blocks:
        free_block(b)
      file_meta.size=0
      file_meta.first_data_block=END_OF_CHAIN
      file_meta.last_data_block=END_OF_CHAIN
      file_meta.mtime=now
      write_metadata_block(file_meta_block, file_meta)
      print("no free data blocks")
      return
    data_blocks.append(new_block)

  for i, data_block in enumerate(data_blocks):
    next_block=data_blocks[i+1] if i+1 < len(data_blocks) else END_OF_CHAIN
    chunk=payload[i*(BLOCK_SIZE-2):(i+1)*(BLOCK_SIZE-2)]
    raw=struct.pack("<H", next_block) + chunk + (b"\x00"*((BLOCK_SIZE-2)-len(chunk)))
    DISK.writeBlock(data_block, raw)

  if data_blocks:
    file_meta.first_data_block=data_blocks[0]
    file_meta.last_data_block=data_blocks[-1]
  else:
    file_meta.first_data_block=END_OF_CHAIN
    file_meta.last_data_block=END_OF_CHAIN
  file_meta.size=len(payload)
  file_meta.mtime=now
  write_metadata_block(file_meta_block, file_meta)

  print("file created and written" if created_new_file else "file written")


def cp(curr_meta_block, source_path, dest_path):
  source_block=resolve_path(curr_meta_block, source_path)
  if source_block is None:
    print("invalid source file")
    return

  source_meta=read_metadata_block(source_block)
  if source_meta is None or source_meta.type != "TYPE_FILE":
    print("source is not a file")
    return

  payload=read_file_bytes(source_meta)
  if payload is None:
    print("failed to read source file")
    return

  parent_path, name=split_parent_and_name(dest_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid destination file")
    return

  name_bytes=name.encode("ascii")

  if len(name_bytes) > MAX_NAME_BYTES:
    print(f"name too long (max {MAX_NAME_BYTES} bytes)")
    return

  parent_block=resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid destination parent directory")
    return

  parent_meta=read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("destination parent is not a directory")
    return

  entries=parse_dir_table(parent_meta.table_block)
  name_hash=hash_name(name)
  existing_idx, insert_idx=probe_dir_entry(entries, name_hash, name)

  if existing_idx is not None:
    print("destination already exists")
    return
  if insert_idx is None:
    print("directory full")
    return

  new_meta_block=alloc_block(SUPERBLOCK["meta_start"], SUPERBLOCK["meta_start"]+SUPERBLOCK["meta_block_count"])
  if new_meta_block is None:
    print("no free metadata blocks")
    return

  now=int(time.time())
  new_file_meta=metadata_block()
  new_file_meta.type="TYPE_FILE"
  new_file_meta.links=1
  new_file_meta.size=0
  new_file_meta.first_data_block=END_OF_CHAIN
  new_file_meta.last_data_block=END_OF_CHAIN
  new_file_meta.ctime=now
  new_file_meta.mtime=now
  write_metadata_block(new_meta_block, new_file_meta)

  block_count=(len(payload)+(BLOCK_SIZE-3))//(BLOCK_SIZE-2)
  data_blocks=[]
  for _ in range(block_count):
    data_block=alloc_block(SUPERBLOCK["data_start"], SUPERBLOCK["total_blocks"])
    if data_block is None:
      for b in data_blocks:
        free_block(b)
      free_block(new_meta_block)
      print("no free data blocks")
      return
    data_blocks.append(data_block)

  for i, data_block in enumerate(data_blocks):
    next_block=data_blocks[i+1] if i+1 < len(data_blocks) else END_OF_CHAIN
    chunk=payload[i*(BLOCK_SIZE-2):(i+1)*(BLOCK_SIZE-2)]
    raw=struct.pack("<H", next_block) + chunk + (b"\x00"*((BLOCK_SIZE-2)-len(chunk)))
    DISK.writeBlock(data_block, raw)

  if data_blocks:
    new_file_meta.first_data_block=data_blocks[0]
    new_file_meta.last_data_block=data_blocks[-1]
  new_file_meta.size=len(payload)
  new_file_meta.mtime=now
  write_metadata_block(new_meta_block, new_file_meta)

  entries[insert_idx]={
    "state": DIR_ENTRY_STATES["USED"],
    "entry_type": ENTRY_TYPE_FILE,
    "hash": name_hash,
    "target_meta_block": new_meta_block,
    "name_len": len(name_bytes),
    "name": name
  }
  if not write_dir_table(parent_meta.table_block, entries):
    for b in data_blocks:
      free_block(b)
    free_block(new_meta_block)
    print("failed to update directory table")
    return

  parent_meta.num_entries+=1
  parent_meta.mtime=now
  write_metadata_block(parent_block, parent_meta)
  print("file copied")


def dir(curr_meta_block, target_path=None):
  block=curr_meta_block if target_path is None else resolve_path(curr_meta_block, target_path)
  if block is None:
    print("invalid path")
    return

  curr_meta=read_metadata_block(block)
  if curr_meta is None or curr_meta.type != "TYPE_DIR":
    print("target is not a directory")
    return

  print("Type\tSize\tName")
  for entry in list_dir_entries(block):
    row_type="file" if entry["type"] == DIR_ENTRY_TYPES[ENTRY_TYPE_FILE] else "dir"
    print(f"{row_type}\t{entry['size']}\t{entry['name']}")


def cd(curr_meta_block, target_path):
  block=resolve_path(curr_meta_block, target_path)
  if block is None:
    print("invalid target directory")
    return curr_meta_block

  target_meta=read_metadata_block(block)
  if target_meta is None or target_meta.type != "TYPE_DIR":
    print("target is not a directory")
    return curr_meta_block

  return block

def ln(curr_meta_block, source_path, dest_path):
  """
  Write a hard link from dest to source. If one file is written to, the other is written to as well.
  """

  source_block = resolve_path(curr_meta_block, source_path)
  if source_block is None:
    print("invalid source path")
    return

  source_meta = read_metadata_block(source_block)
  if source_meta is None or source_meta.type != "TYPE_FILE":
    print("source is not a file")
    return

  parent_path, name = split_parent_and_name(dest_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid destination path")
    return

  name_bytes = name.encode("ascii")
  if len(name_bytes) > MAX_NAME_BYTES:
    print(f"name too long (max {MAX_NAME_BYTES} bytes)")
    return

  parent_block = resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid destination parent directory")
    return

  parent_meta = read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("destination parent is not a directory")
    return

  entries = parse_dir_table(parent_meta.table_block)
  name_hash = hash_name(name)
  existing_idx, insert_idx = probe_dir_entry(entries, name_hash, name)

  if existing_idx is not None:
    print("destination already exists")
    return
  if insert_idx is None:
    print("directory full")
    return

  entries[insert_idx] = {
    "state": DIR_ENTRY_STATES["USED"],
    "entry_type": ENTRY_TYPE_FILE,
    "hash": name_hash,
    "target_meta_block": source_block,
    "name_len": len(name_bytes),
    "name": name,
  }
  if not write_dir_table(parent_meta.table_block, entries):
    print("failed to update directory table")
    return

  source_meta.links += 1
  write_metadata_block(source_block, source_meta)

  now = int(time.time())
  parent_meta.num_entries += 1
  parent_meta.mtime = now
  write_metadata_block(parent_block, parent_meta)
  print("hard link created")


def rm(curr_meta_block, target_path):
  parent_path, name = split_parent_and_name(target_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid target path")
    return

  parent_block = resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid parent directory")
    return

  parent_meta = read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("parent is not a directory")
    return

  entries = parse_dir_table(parent_meta.table_block)
  name_hash = hash_name(name)
  existing_idx, _ = probe_dir_entry(entries, name_hash, name)

  if existing_idx is None:
    print("file not found")
    return

  entry = entries[existing_idx]
  if entry["entry_type"] != ENTRY_TYPE_FILE:
    print("target is not a file")
    return

  file_meta_block = entry["target_meta_block"]
  file_meta = read_metadata_block(file_meta_block)
  if file_meta is None or file_meta.type != "TYPE_FILE":
    print("invalid file metadata")
    return

  entries[existing_idx]["state"] = DIR_ENTRY_STATES["DELETED"]
  write_dir_table(parent_meta.table_block, entries)

  now = int(time.time())
  parent_meta.num_entries -= 1
  parent_meta.mtime = now
  write_metadata_block(parent_block, parent_meta)

  file_meta.links -= 1
  if file_meta.links == 0:
    if file_meta.first_data_block != END_OF_CHAIN:
      free_data_chain(file_meta.first_data_block)
    free_block(file_meta_block)
  else:
    write_metadata_block(file_meta_block, file_meta)

  print("file removed")

def rmdir(curr_meta_block, target_path):
  parent_path, name = split_parent_and_name(target_path)
  if parent_path is None or name in ("", ".", ".."):
    print("invalid target path")
    return

  parent_block = resolve_path(curr_meta_block, parent_path)
  if parent_block is None:
    print("invalid parent directory")
    return

  parent_meta = read_metadata_block(parent_block)
  if parent_meta is None or parent_meta.type != "TYPE_DIR":
    print("parent is not a directory")
    return

  entries = parse_dir_table(parent_meta.table_block)
  name_hash = hash_name(name)
  existing_idx = probe_dir_entry(entries, name_hash, name)[0]

  if existing_idx is None:
    print("directory not found")
    return

  entry = entries[existing_idx]
  if entry["entry_type"] != ENTRY_TYPE_DIR:
    print("target is not a directory")
    return

  dir_meta_block = entry["target_meta_block"]

  if dir_meta_block == curr_meta_block:
    print("cannot remove current working directory")
    return
  if dir_meta_block == SUPERBLOCK["root_meta_block"]:
    print("cannot remove root directory")
    return

  dir_meta = read_metadata_block(dir_meta_block)
  if dir_meta is None or dir_meta.type != "TYPE_DIR":
    print("directory is not TYPE_DIR")
    return

  if dir_meta.num_entries != 0:
    print("directory not empty")
    return

  # make a tombstone
  entries[existing_idx]["state"] = DIR_ENTRY_STATES["DELETED"]
  write_dir_table(parent_meta.table_block, entries)

  now = int(time.time())
  parent_meta.num_entries -= 1
  parent_meta.mtime = now
  write_metadata_block(parent_block, parent_meta)

  free_block(dir_meta.table_block)
  free_block(dir_meta_block)
  print("directory removed")

def read(curr_meta_block, target_path):
  block=resolve_path(curr_meta_block, target_path)
  if block is None:
    print("invalid target filename")
    return

  target_meta=read_metadata_block(block)
  if target_meta is None or target_meta.type != "TYPE_FILE":
    print("target is not a file")
    return

  print(read_file_bytes(target_meta).decode("ascii", errors="replace"))


def stat(curr_meta_block, target_path):
  block=resolve_path(curr_meta_block, target_path)
  if block is None:
    print("invalid path")
    return

  target_meta=read_metadata_block(block)
  if target_meta is None:
    print("invalid metadata block")
    return

  print("metadata information:")
  print(f"meta block: {block}")
  print(f"type: {target_meta.type}")
  print(f"links: {target_meta.links}")
  print(f"ctime: {target_meta.ctime}")
  print(f"mtime: {target_meta.mtime}")
  if target_meta.type == "TYPE_FILE":
    print(f"size: {target_meta.size}")
    print(f"first data block: {target_meta.first_data_block}")
    print(f"last data block: {target_meta.last_data_block}")
  else:
    print(f"parent meta block: {target_meta.parent_meta_block}")
    print(f"table block: {target_meta.table_block}")
    print(f"num entries: {target_meta.num_entries}")


def pwd(curr_meta_block):
  if curr_meta_block == SUPERBLOCK["root_meta_block"]:
    print("/")
    return

  path_parts=[]
  block=curr_meta_block

  while block != SUPERBLOCK["root_meta_block"]:
    curr_meta=read_metadata_block(block)
    if curr_meta is None:
      break
    parent=curr_meta.parent_meta_block

    found_name=None
    for entry in list_dir_entries(parent):
      if entry["target_meta_block"] == block:
        found_name=entry["name"]
        break

    if found_name is None:
      break

    path_parts.append(found_name)
    block=parent

  path_parts.reverse()
  print("/" + "/".join(path_parts))


def help():
  print("dir(ls) <path>\t\tlist contents of current directory or [path]")
  print("cd <dir>\t\tchange directory to <dir> (.. supported)")
  print("read(cat) <file>\tread and print the contents of <file>")
  print("pwd\t\t\tprints working directory")
  print("stat <path>\t\tprints metadata information for <path>")
  print("mkdir <dir>\t\tcreate a new directory")
  print("touch <file>\t\tcreate a new file")
  print("write <file> <txt>\twrite text to file")
  print("rm <file>\t\tdelete a file")
  print("rmdir <dir>\t\tdelete a directory")
  print("cp <src> <dst>\t\tcopy a file")
  print("ln <src> <dst>\t\tcreate a hard link")
  print("help\t\t\tprints this helpful information")
  print("exit\t\t\texit program")


SUPERBLOCK=read_superblock()

BITMAP=bytearray(DISK.readBlock(SUPERBLOCK["bitmap_start"]))
allocated_count=sum(1 for i in range(SUPERBLOCK["total_blocks"]) if is_block_allocated(i))

print(f"Mounted {SUPERBLOCK['magic']} on {DISK_FILE}")
print(f"Blocks: {SUPERBLOCK['total_blocks']}, allocated: {allocated_count}, root meta block: {SUPERBLOCK['root_meta_block']}")

curr_meta_block=SUPERBLOCK["root_meta_block"]
while True:
  try:
    inp=input("> ").split()
    if not inp:
      continue
    cmd=inp[0].lower()

    if cmd in ("dir", "ls"):
      dir(curr_meta_block, inp[1] if len(inp) > 1 else None)
    elif cmd == "cd":
      if len(inp) == 1:
        curr_meta_block=SUPERBLOCK["root_meta_block"]
        continue
      curr_meta_block=cd(curr_meta_block, inp[1])
    elif cmd in ("cat", "read"):
      if len(inp) < 2:
        print("read requires a filename")
        continue
      read(curr_meta_block, inp[1])
    elif cmd == "pwd":
      pwd(curr_meta_block)
    elif cmd == "stat":
      if len(inp) < 2:
        print("stat requires a path")
        continue
      stat(curr_meta_block, inp[1])
    elif cmd == "mkdir":
      if len(inp) < 2:
        print("mkdir requires a directory name")
        continue
      mkdir(curr_meta_block, inp[1])
    elif cmd == "touch":
      if len(inp) < 2:
        print("touch requires a filename")
        continue
      touch(curr_meta_block, inp[1])
    elif cmd == "write":
      if len(inp) < 2:
        print("write requires a filename")
        continue
      write(curr_meta_block, inp[1], " ".join(inp[2:]) if len(inp) > 2 else "")
    elif cmd == "rm":
      if len(inp) < 2:
        print("rm requires a filename")
        continue
      rm(curr_meta_block, inp[1])
    elif cmd == "rmdir":
      if len(inp) < 2:
        print("rmdir requires a directory name")
        continue
      rmdir(curr_meta_block, inp[1])
    elif cmd == "cp":
      if len(inp) < 3:
        print("cp requires source and destination paths")
        continue
      cp(curr_meta_block, inp[1], inp[2])
    elif cmd == "ln":
      if len(inp) < 3:
        print("ln requires source and destination paths")
        continue
      ln(curr_meta_block, inp[1], inp[2])
    elif cmd == "help":
      help()
    elif cmd in ("cls", "clear"):
      subprocess.run("cls" if subprocess.os.name=="nt" else "clear", shell=True)
    elif cmd == "exit":
      break
    else:
      print("invalid option")
  except KeyboardInterrupt:
    break

DISK.printStats()
