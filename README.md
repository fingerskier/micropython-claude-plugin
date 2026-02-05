# micropython-claude-plugin
A plugin to interact with micropython devices within Claude

## Features

* File sync
  * upload
  * download
  * newest-wins
* Pull an image from the device
* Push an image to the device
* _Run_ the device program
  * stream output to Claude
  * let Claude send commands


## Architecture

* Python-based
* MCP server
* USB-Serial I/O

## Installation

```bash
pip install -e .
```

Or install dependencies directly:

```bash
pip install mcp pyserial click
```

## Usage

### Running the MCP Server

```bash
# Start the server (connects to device when requested via tools)
micropython-claude

# Or auto-connect to a specific port on startup
micropython-claude --port /dev/ttyUSB0 --baudrate 115200
```

### Claude Desktop Configuration

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "micropython": {
      "command": "micropython-claude",
      "args": []
    }
  }
}
```

## Available Tools

### Connection Tools

| Tool | Description |
|------|-------------|
| `list_devices` | List available serial ports that might be MicroPython devices |
| `connect` | Connect to a MicroPython device on a serial port |
| `disconnect` | Disconnect from the current device |
| `device_info` | Get information about the connected device |

### File Operations

| Tool | Description |
|------|-------------|
| `list_files` | List files and directories on the device |
| `read_file` | Read a file from the device |
| `write_file` | Write content to a file on the device |
| `delete_file` | Delete a file from the device |
| `mkdir` | Create a directory on the device |
| `upload_file` | Upload a file from local filesystem to the device |
| `download_file` | Download a file from the device to local filesystem |
| `sync_file` | Sync a file between local and device (upload/download/newest-wins) |
| `sync_directory` | Sync a directory between local and device |

### Image Operations

| Tool | Description |
|------|-------------|
| `pull_image` | Pull a filesystem image from the device (backup) |
| `push_image` | Push a filesystem image to the device (restore) |
| `compare_image` | Compare device filesystem with a saved image |

### Execution Tools

| Tool | Description |
|------|-------------|
| `execute` | Execute Python code on the device |
| `run_file` | Run a Python file on the device |
| `run_main` | Run the main.py file on the device |
| `send_command` | Send a command to the device REPL |
| `interrupt` | Send interrupt (Ctrl+C) to stop running program |
| `soft_reset` | Perform a soft reset of the device |
| `get_variable` | Get the value of a variable on the device |
| `set_variable` | Set a variable on the device |

## Examples

### Basic Workflow

1. List available devices to find your MicroPython board
2. Connect to the device
3. List files to see what's on the device
4. Upload/download files as needed
5. Execute code or run programs
6. Disconnect when done

### File Sync Example

The `sync_file` tool supports three modes:
- `upload`: Always copy from local to device
- `download`: Always copy from device to local
- `newest`: Compare modification times and sync the newest version

### Creating Backups

Use `pull_image` to create a complete backup of your device filesystem:
- Creates a compressed tar archive
- Includes all files and directories
- Stores device metadata

Restore with `push_image` to restore from a backup.

## Supported Devices

Works with any MicroPython device accessible via USB serial, including:
- Raspberry Pi Pico / Pico W
- ESP32 / ESP8266
- STM32 boards
- Other MicroPython-compatible boards

## Requirements

- Python 3.10+
- pyserial
- mcp (Model Context Protocol SDK)
- click
