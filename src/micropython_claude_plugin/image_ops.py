"""Image operations for MicroPython devices (filesystem images/backups).

Compares use sha256 so two files of the same size but different content
are correctly flagged as differing. A full-root wipe (``clean=True`` at
``/``) is refused unless the caller explicitly opts in with
``allow_root_wipe=True``, since a partial wipe of ``/`` can brick a
device that relies on ``boot.py``/``main.py``.
"""

import datetime
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .file_ops import FileOperations, _sanitize_path
from .serial_connection import MicroPythonDevice


@dataclass
class ImageMetadata:
    device_info: dict
    file_count: int
    total_size: int
    created_at: str
    errors: list[str] = field(default_factory=list)


class ImageOperations:
    """Filesystem image operations for MicroPython devices."""

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self.file_ops = FileOperations(device)

    def get_device_info(self) -> dict:
        code = '''
import sys, os, json
info = {}
info["platform"] = sys.platform
info["version"] = sys.version
try:
    info["implementation"] = {
        "name": sys.implementation.name,
        "version": ".".join(str(v) for v in sys.implementation.version[:3])
    }
except Exception:
    pass
try:
    import gc
    gc.collect()
    info["mem_free"] = gc.mem_free()
    info["mem_alloc"] = gc.mem_alloc()
except Exception:
    pass
try:
    st = os.statvfs("/")
    info["fs_block_size"] = st[0]
    info["fs_total_blocks"] = st[2]
    info["fs_free_blocks"] = st[3]
except Exception:
    pass
try:
    import machine
    info["freq"] = machine.freq()
    info["unique_id"] = machine.unique_id().hex()
except Exception:
    pass
print(json.dumps(info))
'''
        with self.device.raw_repl_session():
            output = self.device.execute(code)
        try:
            return json.loads(output.strip())
        except json.JSONDecodeError:
            return {"raw_output": output}

    def _sha256_device(self, path: str) -> str | None:
        """Compute sha256 of a device file; returns None if unreadable."""
        path = _sanitize_path(path)
        code = f'''
import hashlib, ubinascii
try:
    h = hashlib.sha256()
    with open("{path}", "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            h.update(chunk)
    print(ubinascii.hexlify(h.digest()).decode())
except Exception as e:
    print("ERROR:" + str(e))
'''
        output = self.device.execute(code, timeout=60.0).strip()
        if output.startswith("ERROR:"):
            return None
        return output.splitlines()[-1].strip()

    def pull_image(self, output_path: str | Path, base_path: str = "/") -> ImageMetadata:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        created_at = datetime.datetime.now().isoformat()
        errors: list[str] = []
        file_count = 0
        total_size = 0

        with self.device.raw_repl_session():
            device_info = self.get_device_info()
            all_files = self._collect_files_recursive(base_path)

            # Collect all file contents first, then write the archive.
            # This lets us include the error list in the archive's
            # metadata (the metadata tar entry must be written first /
            # known up front since tar has no rewriting).
            collected: list[tuple[str, bytes, object]] = []
            for file_path, file_info in all_files:
                try:
                    content = self.file_ops.read_file(file_path)
                except Exception as e:
                    errors.append(f"{file_path}: {e}")
                    continue
                collected.append((file_path, content, file_info))

            with tarfile.open(output_path, "w:gz") as tar:
                metadata = {
                    "device_info": device_info,
                    "base_path": base_path,
                    "created_at": created_at,
                    "errors": errors,
                }
                metadata_bytes = json.dumps(metadata, indent=2).encode('utf-8')
                metadata_info = tarfile.TarInfo(name=".micropython_image_metadata.json")
                metadata_info.size = len(metadata_bytes)
                tar.addfile(metadata_info, io.BytesIO(metadata_bytes))

                for file_path, content, file_info in collected:
                    archive_name = file_path
                    if archive_name.startswith(base_path):
                        archive_name = archive_name[len(base_path):]
                    archive_name = archive_name.lstrip('/')
                    if not archive_name:
                        continue

                    tar_info = tarfile.TarInfo(name=archive_name)
                    tar_info.size = len(content)
                    if getattr(file_info, "mtime", None):
                        tar_info.mtime = file_info.mtime
                    tar.addfile(tar_info, io.BytesIO(content))

                    file_count += 1
                    total_size += len(content)

        return ImageMetadata(
            device_info=device_info,
            file_count=file_count,
            total_size=total_size,
            created_at=created_at,
            errors=errors,
        )

    def push_image(
        self,
        image_path: str | Path,
        target_path: str = "/",
        clean: bool = False,
        allow_root_wipe: bool = False,
        allow_platform_mismatch: bool = False,
    ) -> dict:
        """Push a filesystem image to the device.

        Args:
            image_path: Path to the image (tar file).
            target_path: Base path on device to extract to (default: "/").
            clean: If True, remove existing files under target_path first.
            allow_root_wipe: Required to be True when both ``clean`` is
                True and ``target_path`` is "/". Guards against
                accidentally wiping boot.py/main.py and bricking a device.
            allow_platform_mismatch: Required to be True when the
                archive's recorded ``device_info.platform`` differs from
                the connected device's current ``platform``. Refuses
                cross-platform restores by default (e.g. RP2040 image
                onto an ESP32) since boot/SDK assumptions diverge.
                Archives written without metadata, or without a
                ``device_info.platform`` field, do NOT trigger the
                guard — the check fires only when both platforms are
                known AND differ.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        if clean and target_path == "/" and not allow_root_wipe:
            raise ValueError(
                "Refusing to clean '/': pass allow_root_wipe=True to "
                "confirm. This removes boot.py/main.py and may require "
                "reflashing to recover."
            )

        archive_metadata = self._read_archive_metadata(image_path)

        results: dict = {
            "files_written": 0,
            "bytes_written": 0,
            "errors": [],
            "metadata": None,
            "cleaned": False,
        }

        with self.device.raw_repl_session():
            archive_platform = None
            if archive_metadata and isinstance(archive_metadata.get("device_info"), dict):
                archive_platform = archive_metadata["device_info"].get("platform")
            current_platform = None
            if archive_platform is not None:
                try:
                    current_platform = self.get_device_info().get("platform")
                except Exception:
                    current_platform = None

            if (
                archive_platform is not None
                and current_platform is not None
                and archive_platform != current_platform
                and not allow_platform_mismatch
            ):
                raise ValueError(
                    f"Image platform '{archive_platform}' does not match "
                    f"device platform '{current_platform}'. Pass "
                    f"allow_platform_mismatch=True to override (boot/SDK "
                    f"assumptions may diverge across MicroPython ports)."
                )

            if clean:
                if target_path == "/":
                    # Wipe each top-level entry — ``rmdir("/")`` is not
                    # defined on MicroPython.
                    try:
                        for entry in self.file_ops.list_files("/"):
                            full = f"/{entry.name}"
                            try:
                                if entry.is_dir:
                                    self.file_ops.rmdir(full, recursive=True)
                                else:
                                    self.file_ops.delete_file(full)
                            except Exception as e:
                                results["errors"].append(f"clean {full}: {e}")
                        results["cleaned"] = True
                    except Exception as e:
                        results["errors"].append(f"clean: {e}")
                else:
                    try:
                        self.file_ops.rmdir(target_path, recursive=True)
                        results["cleaned"] = True
                    except Exception as e:
                        results["errors"].append(f"clean {target_path}: {e}")

            with tarfile.open(image_path, "r:*") as tar:
                for member in tar.getmembers():
                    if member.name == ".micropython_image_metadata.json":
                        f = tar.extractfile(member)
                        if f:
                            results["metadata"] = json.loads(f.read().decode('utf-8'))
                        continue

                    if not member.isfile():
                        continue

                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    content = f.read()

                    device_path = f"{target_path}/{member.name}".replace("//", "/")

                    try:
                        self.file_ops.write_file(device_path, content)
                        results["files_written"] += 1
                        results["bytes_written"] += len(content)
                    except Exception as e:
                        results["errors"].append(f"write {device_path}: {e}")

        return results

    def _read_archive_metadata(self, image_path: Path) -> dict | None:
        """Return the parsed ``.micropython_image_metadata.json`` entry
        from an image archive, or None if the archive lacks one or the
        entry can't be parsed. Used by the platform-mismatch guard so
        the check runs before any device wipe."""
        try:
            with tarfile.open(image_path, "r:*") as tar:
                try:
                    member = tar.getmember(".micropython_image_metadata.json")
                except KeyError:
                    return None
                f = tar.extractfile(member)
                if f is None:
                    return None
                return json.loads(f.read().decode("utf-8"))
        except (tarfile.TarError, json.JSONDecodeError, OSError):
            return None

    def _collect_files_recursive(self, path: str) -> list:
        files = []
        try:
            for entry in self.file_ops.list_files(path):
                full = f"{path}/{entry.name}".replace("//", "/")
                if entry.is_dir:
                    files.extend(self._collect_files_recursive(full))
                else:
                    files.append((full, entry))
        except Exception:
            pass
        return files

    def create_snapshot(self, output_path: str | Path) -> ImageMetadata:
        return self.pull_image(output_path, base_path="/")

    def restore_snapshot(
        self,
        snapshot_path: str | Path,
        clean: bool = True,
        allow_root_wipe: bool = False,
        allow_platform_mismatch: bool = False,
    ) -> dict:
        return self.push_image(
            snapshot_path,
            target_path="/",
            clean=clean,
            allow_root_wipe=allow_root_wipe,
            allow_platform_mismatch=allow_platform_mismatch,
        )

    def compare_with_image(self, image_path: str | Path) -> dict:
        """Compare device filesystem with an image using size + sha256.

        Files whose sizes match but whose contents differ are flagged
        under ``different``. Two files that hash-match are guaranteed
        identical.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        results: dict = {
            "matching": [],
            "different": [],
            "only_on_device": [],
            "only_in_image": [],
        }

        with self.device.raw_repl_session():
            device_files: dict[str, "FileInfo"] = {}
            for file_path, file_info in self._collect_files_recursive("/"):
                device_files[file_path.lstrip('/')] = file_info

            image_files: set[str] = set()
            with tarfile.open(image_path, "r:*") as tar:
                for member in tar.getmembers():
                    if member.name == ".micropython_image_metadata.json":
                        continue
                    if not member.isfile():
                        continue

                    image_files.add(member.name)

                    if member.name not in device_files:
                        results["only_in_image"].append(member.name)
                        continue

                    device_size = device_files[member.name].size
                    if device_size != member.size:
                        results["different"].append({
                            "path": member.name,
                            "device_size": device_size,
                            "image_size": member.size,
                            "reason": "size",
                        })
                        continue

                    # Sizes match — compare hashes.
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    image_hash = hashlib.sha256(f.read()).hexdigest()
                    device_hash = self._sha256_device("/" + member.name)

                    if device_hash is None:
                        results["different"].append({
                            "path": member.name,
                            "device_size": device_size,
                            "image_size": member.size,
                            "reason": "device_unreadable",
                        })
                    elif device_hash == image_hash:
                        results["matching"].append(member.name)
                    else:
                        results["different"].append({
                            "path": member.name,
                            "device_size": device_size,
                            "image_size": member.size,
                            "device_sha256": device_hash,
                            "image_sha256": image_hash,
                            "reason": "content",
                        })

            for path in device_files:
                if path not in image_files:
                    results["only_on_device"].append(path)

        return results
