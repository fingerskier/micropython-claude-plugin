"""File operations for MicroPython devices."""

import base64
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .serial_connection import MicroPythonDevice


@dataclass
class FileInfo:
    """Information about a file on the device."""
    name: str
    size: int
    is_dir: bool
    mtime: int | None = None  # Modification time (if available)


class SyncDirection(Enum):
    """Direction for file sync."""
    UPLOAD = "upload"      # Local to device
    DOWNLOAD = "download"  # Device to local
    NEWEST = "newest"      # Newest wins


class FileOperations:
    """File operations for MicroPython devices."""

    # Chunk size for file transfers (to avoid memory issues on device)
    CHUNK_SIZE = 512

    def __init__(self, device: MicroPythonDevice):
        self.device = device

    def list_files(self, path: str = "/") -> list[FileInfo]:
        """List files and directories at the given path on the device."""
        code = f'''
import os
try:
    entries = os.listdir("{path}")
    for name in entries:
        full_path = "{path}" + ("/" if "{path}" != "/" else "") + name
        try:
            stat = os.stat(full_path)
            is_dir = stat[0] & 0x4000 != 0
            size = stat[6]
            mtime = stat[8] if len(stat) > 8 else 0
            print(f"{{name}}|{{size}}|{{1 if is_dir else 0}}|{{mtime}}")
        except:
            print(f"{{name}}|0|0|0")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code)
        files = []

        for line in output.strip().split('\n'):
            if not line or line.startswith('ERROR:'):
                if line.startswith('ERROR:'):
                    raise RuntimeError(line[6:])
                continue

            parts = line.split('|')
            if len(parts) >= 4:
                files.append(FileInfo(
                    name=parts[0],
                    size=int(parts[1]),
                    is_dir=parts[2] == '1',
                    mtime=int(parts[3]) if parts[3] != '0' else None
                ))

        return files

    def file_exists(self, path: str) -> bool:
        """Check if a file exists on the device."""
        code = f'''
import os
try:
    os.stat("{path}")
    print("EXISTS")
except:
    print("NOT_FOUND")
'''
        output = self.device.execute(code)
        return "EXISTS" in output

    def get_file_info(self, path: str) -> FileInfo | None:
        """Get information about a specific file."""
        code = f'''
import os
try:
    stat = os.stat("{path}")
    is_dir = stat[0] & 0x4000 != 0
    size = stat[6]
    mtime = stat[8] if len(stat) > 8 else 0
    name = "{path}".split("/")[-1]
    print(f"{{name}}|{{size}}|{{1 if is_dir else 0}}|{{mtime}}")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code)

        for line in output.strip().split('\n'):
            if line.startswith('ERROR:'):
                return None
            parts = line.split('|')
            if len(parts) >= 4:
                return FileInfo(
                    name=parts[0],
                    size=int(parts[1]),
                    is_dir=parts[2] == '1',
                    mtime=int(parts[3]) if parts[3] != '0' else None
                )
        return None

    def read_file(self, path: str) -> bytes:
        """Read a file from the device."""
        # Read file in chunks and base64 encode to handle binary data
        code = f'''
import ubinascii
try:
    with open("{path}", "rb") as f:
        while True:
            chunk = f.read({self.CHUNK_SIZE})
            if not chunk:
                break
            print(ubinascii.b2a_base64(chunk).decode().strip())
    print("EOF")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code, timeout=30.0)

        data = b''
        for line in output.strip().split('\n'):
            line = line.strip()
            if line == 'EOF':
                break
            if line.startswith('ERROR:'):
                raise RuntimeError(line[6:])
            if line:
                try:
                    data += base64.b64decode(line)
                except Exception:
                    pass

        return data

    def write_file(self, path: str, content: bytes) -> None:
        """Write a file to the device."""
        # Ensure parent directory exists
        parent = '/'.join(path.rsplit('/', 1)[:-1]) or '/'
        if parent != '/':
            self.mkdir(parent, exist_ok=True)

        # Write file in chunks using base64 encoding
        encoded = base64.b64encode(content).decode('ascii')

        # First, create/truncate the file
        code = f'''
try:
    f = open("{path}", "wb")
    f.close()
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code)
        if 'ERROR:' in output:
            raise RuntimeError(output.split('ERROR:')[1].strip())

        # Write in chunks
        for i in range(0, len(encoded), self.CHUNK_SIZE):
            chunk = encoded[i:i + self.CHUNK_SIZE]
            code = f'''
import ubinascii
try:
    data = ubinascii.a2b_base64("{chunk}")
    with open("{path}", "ab") as f:
        f.write(data)
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
            output = self.device.execute(code)
            if 'ERROR:' in output:
                raise RuntimeError(output.split('ERROR:')[1].strip())

    def delete_file(self, path: str) -> None:
        """Delete a file from the device."""
        code = f'''
import os
try:
    os.remove("{path}")
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code)
        if 'ERROR:' in output:
            raise RuntimeError(output.split('ERROR:')[1].strip())

    def mkdir(self, path: str, exist_ok: bool = False) -> None:
        """Create a directory on the device."""
        code = f'''
import os
def mkdir_p(path):
    parts = path.strip("/").split("/")
    current = ""
    for part in parts:
        current = current + "/" + part
        try:
            os.mkdir(current)
        except OSError as e:
            if e.args[0] != 17:  # 17 = EEXIST
                raise
    print("OK")

try:
    mkdir_p("{path}")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code)
        if 'ERROR:' in output and not exist_ok:
            raise RuntimeError(output.split('ERROR:')[1].strip())

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory from the device."""
        if recursive:
            code = f'''
import os
def rmdir_r(path):
    for entry in os.listdir(path):
        full = path + "/" + entry
        stat = os.stat(full)
        if stat[0] & 0x4000:  # Directory
            rmdir_r(full)
        else:
            os.remove(full)
    os.rmdir(path)

try:
    rmdir_r("{path}")
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        else:
            code = f'''
import os
try:
    os.rmdir("{path}")
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
'''
        output = self.device.execute(code, timeout=30.0)
        if 'ERROR:' in output:
            raise RuntimeError(output.split('ERROR:')[1].strip())

    def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        """Upload a file from local filesystem to device."""
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        content = local_path.read_bytes()
        self.write_file(remote_path, content)

    def download_file(self, remote_path: str, local_path: str | Path) -> None:
        """Download a file from device to local filesystem."""
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        content = self.read_file(remote_path)
        local_path.write_bytes(content)

    def sync_file(
        self,
        local_path: str | Path,
        remote_path: str,
        direction: SyncDirection = SyncDirection.NEWEST
    ) -> str:
        """
        Sync a file between local and device.

        Returns:
            Description of what was done
        """
        local_path = Path(local_path)

        local_exists = local_path.exists()
        remote_info = self.get_file_info(remote_path)
        remote_exists = remote_info is not None

        if direction == SyncDirection.UPLOAD:
            if not local_exists:
                raise FileNotFoundError(f"Local file not found: {local_path}")
            self.upload_file(local_path, remote_path)
            return f"Uploaded {local_path} to {remote_path}"

        elif direction == SyncDirection.DOWNLOAD:
            if not remote_exists:
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            self.download_file(remote_path, local_path)
            return f"Downloaded {remote_path} to {local_path}"

        else:  # NEWEST wins
            if not local_exists and not remote_exists:
                raise FileNotFoundError(
                    f"Neither local ({local_path}) nor remote ({remote_path}) exists"
                )

            if not local_exists:
                self.download_file(remote_path, local_path)
                return f"Downloaded {remote_path} (local didn't exist)"

            if not remote_exists:
                self.upload_file(local_path, remote_path)
                return f"Uploaded {local_path} (remote didn't exist)"

            # Both exist - compare modification times
            local_mtime = int(local_path.stat().st_mtime)
            remote_mtime = remote_info.mtime or 0

            if local_mtime > remote_mtime:
                self.upload_file(local_path, remote_path)
                return f"Uploaded {local_path} (local is newer)"
            elif remote_mtime > local_mtime:
                self.download_file(remote_path, local_path)
                return f"Downloaded {remote_path} (remote is newer)"
            else:
                return f"Files are in sync (same mtime)"

    def sync_directory(
        self,
        local_dir: str | Path,
        remote_dir: str,
        direction: SyncDirection = SyncDirection.NEWEST,
        pattern: str = "*"
    ) -> list[str]:
        """
        Sync a directory between local and device.

        Returns:
            List of actions taken
        """
        import fnmatch

        local_dir = Path(local_dir)
        results = []

        if direction == SyncDirection.UPLOAD or direction == SyncDirection.NEWEST:
            # Ensure local directory exists for upload/newest
            if local_dir.exists():
                for local_file in local_dir.rglob(pattern):
                    if local_file.is_file():
                        rel_path = local_file.relative_to(local_dir)
                        remote_path = f"{remote_dir}/{rel_path}".replace("\\", "/")
                        try:
                            result = self.sync_file(local_file, remote_path, direction)
                            results.append(result)
                        except Exception as e:
                            results.append(f"Error syncing {local_file}: {e}")

        if direction == SyncDirection.DOWNLOAD or direction == SyncDirection.NEWEST:
            # Get remote files
            try:
                remote_files = self._list_files_recursive(remote_dir)
                for remote_file in remote_files:
                    if fnmatch.fnmatch(remote_file, pattern) or pattern == "*":
                        rel_path = remote_file[len(remote_dir):].lstrip('/')
                        local_file = local_dir / rel_path

                        # Skip if already synced in upload phase
                        if direction == SyncDirection.NEWEST and local_file.exists():
                            continue

                        try:
                            result = self.sync_file(local_file, remote_file, direction)
                            results.append(result)
                        except Exception as e:
                            results.append(f"Error syncing {remote_file}: {e}")
            except Exception as e:
                results.append(f"Error listing remote directory: {e}")

        return results

    def _list_files_recursive(self, path: str) -> list[str]:
        """Recursively list all files in a directory on the device."""
        files = []
        try:
            entries = self.list_files(path)
            for entry in entries:
                full_path = f"{path}/{entry.name}".replace("//", "/")
                if entry.is_dir:
                    files.extend(self._list_files_recursive(full_path))
                else:
                    files.append(full_path)
        except Exception:
            pass
        return files
