"""MCP Server for MicroPython device interaction."""

import json
from typing import Any

import click
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .serial_connection import (
    MicroPythonDevice,
    list_devices,
    find_micropython_devices,
    DeviceInfo
)
from .file_ops import FileOperations, SyncDirection
from .image_ops import ImageOperations
from .device_runner import DeviceRunner, InteractiveSession


# Global state for the connected device
_device: MicroPythonDevice | None = None
_file_ops: FileOperations | None = None
_image_ops: ImageOperations | None = None
_runner: DeviceRunner | None = None
_session: InteractiveSession | None = None


def get_device() -> MicroPythonDevice:
    """Get the connected device or raise an error."""
    if _device is None or not _device.is_connected:
        raise RuntimeError("No device connected. Use 'connect' tool first.")
    return _device


def get_file_ops() -> FileOperations:
    """Get file operations instance."""
    global _file_ops
    if _file_ops is None:
        _file_ops = FileOperations(get_device())
    return _file_ops


def get_image_ops() -> ImageOperations:
    """Get image operations instance."""
    global _image_ops
    if _image_ops is None:
        _image_ops = ImageOperations(get_device())
    return _image_ops


def get_runner() -> DeviceRunner:
    """Get device runner instance."""
    global _runner
    if _runner is None:
        _runner = DeviceRunner(get_device())
    return _runner


def get_session() -> InteractiveSession:
    """Get or create interactive session."""
    global _session
    if _session is None:
        _session = InteractiveSession(get_device())
    return _session


# Create the MCP server
server = Server("micropython-claude-plugin")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        # Connection tools
        Tool(
            name="list_devices",
            description="List available serial ports that might be MicroPython devices",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_micropython": {
                        "type": "boolean",
                        "description": "Only show likely MicroPython devices",
                        "default": False
                    }
                }
            }
        ),
        Tool(
            name="connect",
            description="Connect to a MicroPython device on the specified serial port",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {
                        "type": "string",
                        "description": "Serial port (e.g., /dev/ttyUSB0, COM3)"
                    },
                    "baudrate": {
                        "type": "integer",
                        "description": "Baud rate (default: 115200)",
                        "default": 115200
                    }
                },
                "required": ["port"]
            }
        ),
        Tool(
            name="disconnect",
            description="Disconnect from the current device",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="device_info",
            description="Get information about the connected device",
            inputSchema={"type": "object", "properties": {}}
        ),

        # File operation tools
        Tool(
            name="list_files",
            description="List files and directories on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to list (default: /)",
                        "default": "/"
                    }
                }
            }
        ),
        Tool(
            name="read_file",
            description="Read a file from the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file on the device"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="write_file",
            description="Write content to a file on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file on the device"
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file"
                    }
                },
                "required": ["path", "content"]
            }
        ),
        Tool(
            name="delete_file",
            description="Delete a file from the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to delete"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="mkdir",
            description="Create a directory on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path of the directory to create"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="upload_file",
            description="Upload a file from local filesystem to the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "Local file path"
                    },
                    "remote_path": {
                        "type": "string",
                        "description": "Path on the device"
                    }
                },
                "required": ["local_path", "remote_path"]
            }
        ),
        Tool(
            name="download_file",
            description="Download a file from the device to local filesystem",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Path on the device"
                    },
                    "local_path": {
                        "type": "string",
                        "description": "Local file path"
                    }
                },
                "required": ["remote_path", "local_path"]
            }
        ),
        Tool(
            name="sync_file",
            description="Sync a file between local and device (upload, download, or newest-wins)",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "Local file path"
                    },
                    "remote_path": {
                        "type": "string",
                        "description": "Path on the device"
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["upload", "download", "newest"],
                        "description": "Sync direction (default: newest)",
                        "default": "newest"
                    }
                },
                "required": ["local_path", "remote_path"]
            }
        ),
        Tool(
            name="sync_directory",
            description="Sync a directory between local and device",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_dir": {
                        "type": "string",
                        "description": "Local directory path"
                    },
                    "remote_dir": {
                        "type": "string",
                        "description": "Directory on the device"
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["upload", "download", "newest"],
                        "description": "Sync direction (default: newest)",
                        "default": "newest"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "File pattern to match (default: *)",
                        "default": "*"
                    }
                },
                "required": ["local_dir", "remote_dir"]
            }
        ),

        # Image operation tools
        Tool(
            name="pull_image",
            description="Pull a filesystem image from the device (creates a backup archive)",
            inputSchema={
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Path to save the image file"
                    },
                    "base_path": {
                        "type": "string",
                        "description": "Base path on device to backup (default: /)",
                        "default": "/"
                    }
                },
                "required": ["output_path"]
            }
        ),
        Tool(
            name="push_image",
            description="Push a filesystem image to the device (restore from backup)",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Path to the image file"
                    },
                    "target_path": {
                        "type": "string",
                        "description": "Base path on device to restore to (default: /)",
                        "default": "/"
                    },
                    "clean": {
                        "type": "boolean",
                        "description": "Remove existing files first",
                        "default": False
                    }
                },
                "required": ["image_path"]
            }
        ),
        Tool(
            name="compare_image",
            description="Compare device filesystem with a saved image",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Path to the image file to compare"
                    }
                },
                "required": ["image_path"]
            }
        ),

        # Execution tools
        Tool(
            name="execute",
            description="Execute Python code on the device and return the output",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Execution timeout in seconds (default: 30)",
                        "default": 30
                    }
                },
                "required": ["code"]
            }
        ),
        Tool(
            name="run_file",
            description="Run a Python file on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file on the device"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Execution timeout in seconds (default: 30)",
                        "default": 30
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="run_main",
            description="Run the main.py file on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Execution timeout in seconds (default: 30)",
                        "default": 30
                    }
                }
            }
        ),
        Tool(
            name="send_command",
            description="Send a command to the device REPL (for interactive use)",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to send"
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="interrupt",
            description="Send interrupt (Ctrl+C) to stop running program",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="soft_reset",
            description="Perform a soft reset of the device",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_variable",
            description="Get the value of a variable on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the variable"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="set_variable",
            description="Set a variable on the device",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the variable"
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to set (Python expression)"
                    }
                },
                "required": ["name", "value"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    global _device, _file_ops, _image_ops, _runner, _session

    try:
        # Connection tools
        if name == "list_devices":
            filter_mp = arguments.get("filter_micropython", False)
            devices = find_micropython_devices() if filter_mp else list_devices()
            result = [
                {
                    "port": d.port,
                    "description": d.description,
                    "vid": d.vid,
                    "pid": d.pid
                }
                for d in devices
            ]
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "connect":
            port = arguments["port"]
            baudrate = arguments.get("baudrate", 115200)

            # Disconnect existing connection
            if _device and _device.is_connected:
                _device.disconnect()

            _device = MicroPythonDevice(port, baudrate)
            _device.connect()

            # Reset dependent objects
            _file_ops = None
            _image_ops = None
            _runner = None
            _session = None

            return [TextContent(type="text", text=f"Connected to {port} at {baudrate} baud")]

        elif name == "disconnect":
            if _device:
                _device.disconnect()
                _device = None
                _file_ops = None
                _image_ops = None
                _runner = None
                _session = None
            return [TextContent(type="text", text="Disconnected")]

        elif name == "device_info":
            info = get_image_ops().get_device_info()
            return [TextContent(type="text", text=json.dumps(info, indent=2))]

        # File operation tools
        elif name == "list_files":
            path = arguments.get("path", "/")
            files = get_file_ops().list_files(path)
            result = [
                {
                    "name": f.name,
                    "size": f.size,
                    "is_dir": f.is_dir,
                    "mtime": f.mtime
                }
                for f in files
            ]
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "read_file":
            content = get_file_ops().read_file(arguments["path"])
            # Try to decode as text, fall back to showing as hex
            try:
                text = content.decode('utf-8')
                return [TextContent(type="text", text=text)]
            except UnicodeDecodeError:
                return [TextContent(type="text", text=f"Binary file ({len(content)} bytes): {content.hex()[:200]}...")]

        elif name == "write_file":
            content = arguments["content"].encode('utf-8')
            get_file_ops().write_file(arguments["path"], content)
            return [TextContent(type="text", text=f"Written {len(content)} bytes to {arguments['path']}")]

        elif name == "delete_file":
            get_file_ops().delete_file(arguments["path"])
            return [TextContent(type="text", text=f"Deleted {arguments['path']}")]

        elif name == "mkdir":
            get_file_ops().mkdir(arguments["path"], exist_ok=True)
            return [TextContent(type="text", text=f"Created directory {arguments['path']}")]

        elif name == "upload_file":
            get_file_ops().upload_file(arguments["local_path"], arguments["remote_path"])
            return [TextContent(type="text", text=f"Uploaded {arguments['local_path']} to {arguments['remote_path']}")]

        elif name == "download_file":
            get_file_ops().download_file(arguments["remote_path"], arguments["local_path"])
            return [TextContent(type="text", text=f"Downloaded {arguments['remote_path']} to {arguments['local_path']}")]

        elif name == "sync_file":
            direction = SyncDirection(arguments.get("direction", "newest"))
            result = get_file_ops().sync_file(
                arguments["local_path"],
                arguments["remote_path"],
                direction
            )
            return [TextContent(type="text", text=result)]

        elif name == "sync_directory":
            direction = SyncDirection(arguments.get("direction", "newest"))
            results = get_file_ops().sync_directory(
                arguments["local_dir"],
                arguments["remote_dir"],
                direction,
                arguments.get("pattern", "*")
            )
            return [TextContent(type="text", text="\n".join(results))]

        # Image operation tools
        elif name == "pull_image":
            metadata = get_image_ops().pull_image(
                arguments["output_path"],
                arguments.get("base_path", "/")
            )
            result = {
                "output_path": arguments["output_path"],
                "file_count": metadata.file_count,
                "total_size": metadata.total_size,
                "created_at": metadata.created_at,
                "device_info": metadata.device_info
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "push_image":
            result = get_image_ops().push_image(
                arguments["image_path"],
                arguments.get("target_path", "/"),
                arguments.get("clean", False)
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "compare_image":
            result = get_image_ops().compare_with_image(arguments["image_path"])
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Execution tools
        elif name == "execute":
            result = get_runner().execute_code(
                arguments["code"],
                arguments.get("timeout", 30)
            )
            output = result.output
            if result.error:
                output += f"\n[Error: {result.error}]"
            output += f"\n[Execution time: {result.duration_ms}ms]"
            return [TextContent(type="text", text=output)]

        elif name == "run_file":
            result = get_runner().execute_file(
                arguments["path"],
                arguments.get("timeout", 30)
            )
            output = result.output
            if result.error:
                output += f"\n[Error: {result.error}]"
            return [TextContent(type="text", text=output)]

        elif name == "run_main":
            result = get_runner().run_main(arguments.get("timeout", 30))
            output = result.output
            if result.error:
                output += f"\n[Error: {result.error}]"
            return [TextContent(type="text", text=output)]

        elif name == "send_command":
            output = get_session().execute(arguments["command"])
            return [TextContent(type="text", text=output)]

        elif name == "interrupt":
            get_device().interrupt()
            return [TextContent(type="text", text="Interrupt sent")]

        elif name == "soft_reset":
            output = get_runner().soft_reset()
            return [TextContent(type="text", text=output)]

        elif name == "get_variable":
            output = get_session().get_variable(arguments["name"])
            return [TextContent(type="text", text=output)]

        elif name == "set_variable":
            output = get_session().set_variable(arguments["name"], arguments["value"])
            return [TextContent(type="text", text=output)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


@click.command()
@click.option("--port", "-p", help="Serial port to connect to on startup")
@click.option("--baudrate", "-b", default=115200, help="Baud rate (default: 115200)")
def main(port: str | None, baudrate: int):
    """MicroPython Claude Plugin - MCP server for device interaction."""
    import asyncio

    async def run():
        global _device, _file_ops, _image_ops, _runner, _session

        # Auto-connect if port specified
        if port:
            _device = MicroPythonDevice(port, baudrate)
            _device.connect()

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
