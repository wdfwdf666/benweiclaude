"""Minimal project-scoped MCP stdio client and configuration helpers."""

from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from .sandbox import SandboxPolicy, SandboxProcess, SandboxRunner
from .tools_util import clipped_text

PROTOCOL_VERSION = "2025-03-26"
MAX_MCP_OUTPUT = 20_000


@dataclass(frozen=True)
class McpToolRef:
    server: str
    name: str
    public_name: str
    description: str
    input_schema: dict[str, Any]


def config_path(workspace: Path) -> Path:
    return workspace.resolve() / ".mcp.json"


def load_config(workspace: Path) -> dict[str, Any]:
    path = config_path(workspace)
    if not path.exists():
        return {"mcpServers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise ValueError(f"Invalid MCP config: {path}")
    return data


def save_config(workspace: Path, data: dict[str, Any]) -> None:
    path = config_path(workspace)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mcp_server_names(workspace: Path) -> list[str]:
    return sorted(load_config(workspace).get("mcpServers", {}))


def get_server_config(workspace: Path, name: str) -> dict[str, Any]:
    servers = load_config(workspace).get("mcpServers", {})
    if name not in servers:
        raise ValueError(f"MCP server not found: {name}")
    config = servers[name]
    if not isinstance(config, dict):
        raise ValueError(f"Invalid MCP server config: {name}")
    return config


def add_stdio_server(workspace: Path, name: str, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
    _validate_server_name(name)
    data = load_config(workspace)
    servers = data.setdefault("mcpServers", {})
    servers[name] = {"command": command, "args": args}
    if env:
        servers[name]["env"] = env
    save_config(workspace, data)


def add_json_server(workspace: Path, name: str, raw_json: str) -> None:
    _validate_server_name(name)
    value = json.loads(raw_json)
    if not isinstance(value, dict):
        raise ValueError("MCP server JSON must be an object")
    data = load_config(workspace)
    data.setdefault("mcpServers", {})[name] = value
    save_config(workspace, data)


def remove_server(workspace: Path, name: str) -> None:
    data = load_config(workspace)
    servers = data.setdefault("mcpServers", {})
    if name not in servers:
        raise ValueError(f"MCP server not found: {name}")
    del servers[name]
    save_config(workspace, data)


def _validate_server_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("MCP server names may contain only letters, numbers, hyphens and underscores")


def _tool_public_name(server: str, tool_name: str) -> str:
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    return f"mcp__{server}__{safe_tool}"


def _expand_env(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if ":-" in body:
            key, default = body.split(":-", 1)
            return env.get(key, default)
        if body not in env:
            raise ValueError(f"Missing environment variable for MCP config: {body}")
        return env[body]

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def _expand(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_env(value, env)
    if isinstance(value, list):
        return [_expand(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, env) for key, item in value.items()}
    return value


class StdioMcpClient:
    def __init__(self, workspace: Path, server: str, config: dict[str, Any], timeout: float = 10) -> None:
        self.workspace = workspace
        self.server = server
        self.config = config
        self.timeout = timeout
        self.process: SandboxProcess | None = None
        self.sandbox = SandboxRunner()
        self._next_id = 1
        self._lock = threading.Lock()

    def __enter__(self) -> "StdioMcpClient":
        return self.start()

    def start(self) -> "StdioMcpClient":
        """Start and initialize the server, or reuse the live process."""
        if self.is_alive():
            return self
        self.close()
        allowlist = self.config.get("envAllowlist", [])
        if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
            raise ValueError(f"Invalid envAllowlist for MCP server {self.server}")
        network_mode = self.config.get("networkMode", "none")
        policy = SandboxPolicy(
            self.workspace, profile="trusted-process", network_mode=network_mode,
            environment_allowlist=tuple(allowlist), timeout_seconds=self.timeout,
            max_output_bytes=MAX_MCP_OUTPUT,
        )
        env = self.sandbox.environment(policy)
        config_env = self.config.get("env", {})
        if not isinstance(config_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in config_env.items()):
            raise ValueError(f"Invalid env for MCP server {self.server}")
        expanded_env = _expand(config_env, env)
        env.update(expanded_env)
        expanded = _expand(self.config, env)
        transport = expanded.get("type", "stdio")
        if transport not in {"stdio", ""}:
            raise ValueError(f"MCP server {self.server} uses unsupported transport: {transport}")
        command = expanded.get("command")
        args = expanded.get("args", [])
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"MCP server {self.server} needs a command")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {self.server} args must be strings")
        self.process = self.sandbox.spawn(
            [command, *args], policy, env_additions=expanded_env,
        )
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "codepilot", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=2)
            except Exception:
                pass
        finally:
            process.close()
            self.process = None

    def _write(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("MCP server is not running")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        self.process.stdin.write(payload.encode("utf-8"))
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("MCP server is not running")
        while True:
            line = self._readline()
            if not line:
                stderr = ""
                if self.process.stderr:
                    try:
                        stderr_value = self.process.stderr.read(2000)
                        stderr = stderr_value.decode("utf-8", errors="replace")
                    except Exception:
                        stderr = ""
                raise RuntimeError(f"MCP server {self.server} closed stdout. {stderr.strip()}")
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("MCP response result must be an object")
            return result

    def _readline(self) -> str:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("MCP server is not running")
        result: queue.Queue[bytes] = queue.Queue(maxsize=1)

        def read() -> None:
            result.put(self.process.stdout.readline())

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            return result.get(timeout=self.timeout).decode("utf-8", errors="replace")
        except queue.Empty as exc:
            raise TimeoutError(f"MCP server {self.server} did not respond within {self.timeout:g}s") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(request_id)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise RuntimeError("MCP tools/list returned a non-list tools field")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            prefix = "MCP tool returned an error: "
        else:
            prefix = ""
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return clipped_text(prefix + ("\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)), MAX_MCP_OUTPUT)
        return clipped_text(prefix + json.dumps(result, ensure_ascii=False), MAX_MCP_OUTPUT)


class McpManager:
    def __init__(self, workspace: Path, event_sink=None, trust_validator=None) -> None:
        self.workspace = workspace.resolve()
        self.event_sink = event_sink
        self.trust_validator = trust_validator
        self.tools: list[McpToolRef] = []
        self.server_configs: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self.clients: dict[str, StdioMcpClient] = {}
        self._lock = threading.RLock()
        self.reload()

    @property
    def path(self) -> Path:
        return config_path(self.workspace)

    def reload(self) -> None:
        self._ensure_trusted()
        self.close()
        self.tools = []
        self.server_configs = {}
        self.errors = {}
        servers = load_config(self.workspace).get("mcpServers", {})
        for server, config in sorted(servers.items()):
            if not isinstance(config, dict):
                self.errors[server] = "invalid server config"
                continue
            self.server_configs[server] = config
            try:
                client = StdioMcpClient(self.workspace, server, config).start()
                self.clients[server] = client
                for item in client.list_tools():
                    name = item.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    schema = item.get("inputSchema", {"type": "object", "properties": {}})
                    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                        schema = {"type": "object", "properties": {}}
                    self.tools.append(McpToolRef(
                        server=server,
                        name=name,
                        public_name=_tool_public_name(server, name),
                        description=str(item.get("description") or f"MCP tool {name} from {server}"),
                        input_schema=schema,
                    ))
            except Exception as exc:
                client = self.clients.pop(server, None)
                if client is not None:
                    client.close()
                self.errors[server] = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        with self._lock:
            clients, self.clients = self.clients, {}
            for client in clients.values():
                client.close()

    def _client(self, server: str, config: dict[str, Any]) -> StdioMcpClient:
        """Return a live client; a dead process is reconnected before the next call."""
        with self._lock:
            client = self.clients.get(server)
            if client is not None and client.is_alive():
                return client
            if client is not None:
                client.close()
            client = StdioMcpClient(self.workspace, server, config).start()
            self.clients[server] = client
            self.errors.pop(server, None)
            return client

    def _ensure_trusted(self) -> None:
        if self.trust_validator is not None and not self.trust_validator():
            raise ValueError("Workspace executable configuration changed; trust must be confirmed again")

    def status(self) -> list[dict[str, Any]]:
        servers = load_config(self.workspace).get("mcpServers", {})
        rows = []
        for server in sorted(servers):
            names = [tool.public_name for tool in self.tools if tool.server == server]
            rows.append({
                "name": server,
                "status": "failed" if server in self.errors else "connected",
                "error": self.errors.get(server, ""),
                "tools": names,
            })
        return rows

    def as_langchain_tools(self) -> list[StructuredTool]:
        return [self._to_langchain_tool(ref) for ref in self.tools]

    def _to_langchain_tool(self, ref: McpToolRef) -> StructuredTool:
        def call(**kwargs) -> str:
            return self.call(ref.public_name, kwargs)

        call.__name__ = ref.public_name
        return StructuredTool.from_function(
            func=call,
            name=ref.public_name,
            description=f"[MCP:{ref.server}] {ref.description}",
            args_schema=ref.input_schema,
            infer_schema=False,
        )

    def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        self._ensure_trusted()
        ref = next((item for item in self.tools if item.public_name == public_name), None)
        if ref is None:
            raise ValueError(f"Unknown MCP tool: {public_name}")
        config = self.server_configs.get(ref.server)
        if config is None:
            raise ValueError(f"MCP server configuration is not loaded: {ref.server}")
        client = self._client(ref.server, config)
        try:
            return client.call_tool(ref.name, arguments)
        except Exception as exc:
            # The outcome of tools/call may be ambiguous, so reconnect only for
            # the next invocation rather than replaying this request.
            client.close()
            self.errors[ref.server] = f"{type(exc).__name__}: {exc}"
            raise
