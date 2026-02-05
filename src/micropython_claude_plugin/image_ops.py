"""Image operations for MicroPython devices (filesystem images/backups)."""

import base64
import json
import tarfile
import io
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .serial_connection import MicroPythonDevice
from .file_ops import FileOperations


@dataclass
class ImageMetadata:
    """Metadata for a device image."""
    device_info: dict
    file_count: int
    total_size: int
    created_at: str


class ImageOperations:
    """Handle filesystem image operations for MicroPython devices."""

    def __init__(self, device: MicroPythonDevice):
        self.device = device
        self.file_ops = FileOperations(device)

    def get_device_info(self) -> dict:
        """Get information about the device."""
        code = '''
import sys
import os

info = {}

# Platform info
info["platform"] = sys.platform
info["version"] = sys.version

# Implementation info
try:
    info["implementation"] = {
        "name": sys.implementation.name,
        "version": ".".join(str(v) for v in sys.implementation.version[:3])
    }
except:
    pass

# Memory info
try:
    import gc
    gc.collect()
    info["mem_free"] = gc.mem_free()
    info["mem_alloc"] = gc.mem_alloc()
except:
    pass

# Filesystem info
try:
    stat = os.statvfs("/")
    info["fs_block_size"] = stat[0]
    info["fs_total_blocks"] = stat[2]
    info["fs_free_blocks"] = stat[3]
except:
    pass

# Machine info
try:
    import machine
    info["freq"] = machine.freq()
    info["unique_id"] = machine.unique_id().hex()
except:
    pass

import json
print(json.dumps(info))
'''
        output = self.device.execute(code)
        try:
            return json.loads(output.strip())
        except json.JSONDecodeError:
            return {"raw_output": output}

    def pull_image(self, output_path: str | Path, base_path: str = "/") -> ImageMetadata:
        """
        Pull a filesystem image from the device.

        Creates a tar archive containing all files from the device.

        Args:
            output_path: Path to save the image (tar file)
            base_path: Base path on device to start from

        Returns:
            ImageMetadata with information about the created image
        """
        import datetime

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Get device info
        device_info = self.get_device_info()

        # Collect all files
        all_files = self._collect_files_recursive(base_path)

        file_count = 0
        total_size = 0

        # Create tar archive
        with tarfile.open(output_path, "w:gz") as tar:
            # Add metadata file
            metadata = {
                "device_info": device_info,
                "base_path": base_path,
                "created_at": datetime.datetime.now().isoformat(),
            }
            metadata_bytes = json.dumps(metadata, indent=2).encode('utf-8')
            metadata_info = tarfile.TarInfo(name=".micropython_image_metadata.json")
            metadata_info.size = len(metadata_bytes)
            tar.addfile(metadata_info, io.BytesIO(metadata_bytes))

            # Add each file
            for file_path, file_info in all_files:
                try:
                    # Read file content
                    content = self.file_ops.read_file(file_path)

                    # Create tar entry
                    # Remove leading slash and base_path for archive
                    archive_name = file_path
                    if archive_name.startswith(base_path):
                        archive_name = archive_name[len(base_path):]
                    archive_name = archive_name.lstrip('/')

                    if not archive_name:
                        continue

                    tar_info = tarfile.TarInfo(name=archive_name)
                    tar_info.size = len(content)
                    if file_info.mtime:
                        tar_info.mtime = file_info.mtime

                    tar.addfile(tar_info, io.BytesIO(content))

                    file_count += 1
                    total_size += len(content)

                except Exception as e:
                    # Log but continue with other files
                    print(f"Warning: Could not read {file_path}: {e}")

        return ImageMetadata(
            device_info=device_info,
            file_count=file_count,
            total_size=total_size,
            created_at=datetime.datetime.now().isoformat()
        )

    def push_image(
        self,
        image_path: str | Path,
        target_path: str = "/",
        clean: bool = False
    ) -> dict:
        """
        Push a filesystem image to the device.

        Args:
            image_path: Path to the image (tar file)
            target_path: Base path on device to extract to
            clean: If True, remove existing files first

        Returns:
            Dictionary with results (files_written, errors, etc.)
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        results = {
            "files_written": 0,
            "bytes_written": 0,
            "errors": [],
            "metadata": None
        }

        # Clean target if requested
        if clean and target_path != "/":
            try:
                self.file_ops.rmdir(target_path, recursive=True)
            except Exception:
                pass  # Directory might not exist

        # Extract and upload files
        with tarfile.open(image_path, "r:*") as tar:
            for member in tar.getmembers():
                if member.name == ".micropython_image_metadata.json":
                    # Read metadata
                    f = tar.extractfile(member)
                    if f:
                        results["metadata"] = json.loads(f.read().decode('utf-8'))
                    continue

                if not member.isfile():
                    continue

                # Extract file content
                f = tar.extractfile(member)
                if f is None:
                    continue

                content = f.read()

                # Build target path
                device_path = f"{target_path}/{member.name}".replace("//", "/")

                try:
                    self.file_ops.write_file(device_path, content)
                    results["files_written"] += 1
                    results["bytes_written"] += len(content)
                except Exception as e:
                    results["errors"].append(f"Error writing {device_path}: {e}")

        return results

    def _collect_files_recursive(self, path: str) -> list:
        """Recursively collect all files from a path on the device."""
        from .file_ops import FileInfo

        files = []
        try:
            entries = self.file_ops.list_files(path)
            for entry in entries:
                full_path = f"{path}/{entry.name}".replace("//", "/")
                if entry.is_dir:
                    files.extend(self._collect_files_recursive(full_path))
                else:
                    files.append((full_path, entry))
        except Exception:
            pass
        return files

    def create_snapshot(self, output_path: str | Path) -> ImageMetadata:
        """
        Create a complete snapshot of the device filesystem.

        Alias for pull_image with root path.
        """
        return self.pull_image(output_path, base_path="/")

    def restore_snapshot(
        self,
        snapshot_path: str | Path,
        clean: bool = True
    ) -> dict:
        """
        Restore a device from a snapshot.

        Args:
            snapshot_path: Path to the snapshot file
            clean: If True, this is a full restore (clean first)

        Returns:
            Dictionary with restore results
        """
        return self.push_image(snapshot_path, target_path="/", clean=clean)

    def compare_with_image(self, image_path: str | Path) -> dict:
        """
        Compare device filesystem with an image.

        Returns:
            Dictionary with comparison results
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        results = {
            "matching": [],
            "different": [],
            "only_on_device": [],
            "only_in_image": []
        }

        # Get current device files
        device_files = {}
        for file_path, file_info in self._collect_files_recursive("/"):
            device_files[file_path.lstrip('/')] = file_info

        # Compare with image
        image_files = set()
        with tarfile.open(image_path, "r:*") as tar:
            for member in tar.getmembers():
                if member.name == ".micropython_image_metadata.json":
                    continue
                if not member.isfile():
                    continue

                image_files.add(member.name)

                if member.name in device_files:
                    # Both exist - compare sizes
                    device_size = device_files[member.name].size
                    if device_size == member.size:
                        results["matching"].append(member.name)
                    else:
                        results["different"].append({
                            "path": member.name,
                            "device_size": device_size,
                            "image_size": member.size
                        })
                else:
                    results["only_in_image"].append(member.name)

        # Find files only on device
        for path in device_files:
            if path not in image_files:
                results["only_on_device"].append(path)

        return results
