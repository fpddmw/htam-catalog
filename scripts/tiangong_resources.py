#!/usr/bin/env python3
"""Bind owner-private TianGong resources to existing tools; no API implementation."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

DEFAULT_PROFILE = Path.home() / ".config/htam/tiangong-resources.json"
SEARCH = ("sci", "report", "patent")
DOCUMENT_NAMES = {
    "UNSTRUCTURED_API_BASE_URL", "UNSTRUCTURED_AUTH_TOKEN",
    "UNSTRUCTURED_PROVIDER", "UNSTRUCTURED_MODEL",
    "TIANGONG_LCA_UNSTRUCTURED_API_BASE_URL", "TIANGONG_LCA_UNSTRUCTURED_API_KEY",
    "TIANGONG_LCA_UNSTRUCTURED_PROVIDER", "TIANGONG_LCA_UNSTRUCTURED_MODEL",
}
SEARCH_COMMON = {
    "TIANGONG_AI_APIKEY", "TIANGONG_RESEARCH_API_BASE_URL",
    "TIANGONG_AI_SEARCH_API_BASE_URL", "TIANGONG_AI_API_BASE_URL",
    "TIANGONG_REGION",
}
SEARCH_NAMES = {
    f"TIANGONG_{name.upper()}_{suffix}"
    for name in SEARCH for suffix in ("APIKEY", "SEARCH_URL")
}
ALL_NAMES = DOCUMENT_NAMES | SEARCH_COMMON | SEARCH_NAMES | {
    "TIANGONG_LCA_UNSTRUCTURED_RETURN_TXT", "TIANGONG_LCA_UNSTRUCTURED_CHUNK_TYPE",
    "TIANGONG_ESG_APIKEY", "TIANGONG_RESEARCH_CAPABILITY_CREDENTIALS_JSON",
    "TIANGONG_RESEARCH_ADAPTER_CREDENTIALS_JSON",
}


def private_text(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Configuration must be an owner-owned regular non-symlink file")
    if info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError("Configuration requires owner-only permissions and size <=64 KiB")
    return path.read_text(encoding="utf-8")


def literal_env(path):
    entries = {}
    for line in private_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Source environment must contain literal NAME=value entries")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in entries:
            raise ValueError("Duplicate source environment key")
        entries[key] = value
    return entries


def safe_url(value):
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Resource address must be HTTPS without userinfo, query, or fragment")
    return value.rstrip("/")


def load_profile(path):
    data = json.loads(private_text(path))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported resource profile schema")
    for name in ("document", *SEARCH):
        env = data["resources"][name]["env"]
        allowed = DOCUMENT_NAMES if name == "document" else {
            f"TIANGONG_{name.upper()}_APIKEY", f"TIANGONG_{name.upper()}_SEARCH_URL", "TIANGONG_REGION"
        }
        if set(env) - allowed or any(not isinstance(v, str) or not v for v in env.values()):
            raise ValueError("Invalid resource environment mapping")
        required = {"UNSTRUCTURED_API_BASE_URL", "UNSTRUCTURED_AUTH_TOKEN", "TIANGONG_LCA_UNSTRUCTURED_API_BASE_URL", "TIANGONG_LCA_UNSTRUCTURED_API_KEY"} if name == "document" else {
            f"TIANGONG_{name.upper()}_APIKEY", f"TIANGONG_{name.upper()}_SEARCH_URL", "TIANGONG_REGION"
        }
        if not required <= set(env):
            raise ValueError("Resource profile is incomplete")
        for key, value in env.items():
            if key.endswith(("BASE_URL", "SEARCH_URL")):
                safe_url(value)
        if name == "document":
            if env["UNSTRUCTURED_AUTH_TOKEN"] != env["TIANGONG_LCA_UNSTRUCTURED_API_KEY"] or env["UNSTRUCTURED_API_BASE_URL"] != env["TIANGONG_LCA_UNSTRUCTURED_API_BASE_URL"]:
                raise ValueError("Document CLI and Skill bindings disagree")
    return data


def child_environment(data, resource):
    env = {k: v for k, v in os.environ.items() if k not in ALL_NAMES | {"TIANGONG_AI_CLI", "TIANGONG_AI_CLI_BIN"}}
    env.update(data["resources"][resource]["env"])
    if resource == "document":
        env["TIANGONG_LCA_UNSTRUCTURED_RETURN_TXT"] = "true"
        env["TIANGONG_LCA_UNSTRUCTURED_CHUNK_TYPE"] = "false"
    runtime = data.get("runtime", {})
    if runtime:
        env["PATH"] = runtime["node_bin_dir"] + ":" + runtime["lca_bin_dir"] + ":" + env.get("PATH", "")
        if resource in SEARCH:
            package = Path(runtime["ai_cli"]).parent.parent / "package.json"
            if json.loads(package.read_text())["version"] != runtime["ai_version"]:
                raise ValueError("Search CLI version differs from the reviewed binding")
            env["TIANGONG_AI_CLI_BIN"] = runtime["ai_cli"]
    return env


def import_ar(source, destination, replace):
    source = source.resolve() / ".tiangong-research"
    # Copy only provider credentials from the owner stores, never research sessions.
    search_store = json.loads(literal_env(source / ".env")["TIANGONG_RESEARCH_CAPABILITY_CREDENTIALS_JSON"])
    adapter_store = json.loads(literal_env(source / "setup-adapters.env")["TIANGONG_RESEARCH_ADAPTER_CREDENTIALS_JSON"])
    settings = json.loads(private_text(source / "setup-config.json"))["settings"]
    resources = {}
    for name in SEARCH:
        upper = name.upper()
        key = search_store.get(f"tiangong.{name}.api-key", "")
        if not key:
            raise ValueError("Source search credential missing")
        resources[name] = {"env": {
            f"TIANGONG_{upper}_APIKEY": key,
            f"TIANGONG_{upper}_SEARCH_URL": safe_url(settings[f"tiangong.{name}.endpoint"]),
            "TIANGONG_REGION": settings[f"tiangong.{name}.region"],
        }}
    token = adapter_store.get("tiangong.unstructure.auth-token", "")
    if not token:
        raise ValueError("Source document credential missing")
    base = safe_url(settings["tiangong.unstructure.base-url"])
    if base.endswith("/mineru_with_images"):
        raise ValueError("Document configuration needs base URL, not the full parsing endpoint")
    resources["document"] = {"env": {
        "UNSTRUCTURED_API_BASE_URL": base,
        "UNSTRUCTURED_AUTH_TOKEN": token,
        "TIANGONG_LCA_UNSTRUCTURED_API_BASE_URL": base,
        "TIANGONG_LCA_UNSTRUCTURED_API_KEY": token,
    }}
    owner = Path.home()
    data = {"schema_version": 1, "resources": resources, "runtime": {
        "node_bin_dir": str(owner / ".local/lib/node-v24.19.0-linux-x64/bin"),
        "lca_bin_dir": str(owner / ".local/share/htam/tiangong-tools/node_modules/.bin"),
        "ai_cli": str(owner / ".local/share/htam/tiangong-ai-0.0.63/node_modules/@tiangong-ai/cli/bin/tiangong-ai.js"),
        "ai_version": "0.0.63",
    }, "provenance": {
        "source_workspace": str(source.parent),
        "source_files": [".tiangong-research/.env", ".tiangong-research/setup-adapters.env", ".tiangong-research/setup-config.json"],
        "imported_at_utc": datetime.now(timezone.utc).isoformat(),
    }}
    if destination.exists() and not replace:
        raise ValueError("Profile already exists; use --replace for an intentional refresh")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("Profile directory must be owner-only (chmod 700)")
    if destination.is_symlink():
        raise ValueError("Profile destination must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".tiangong-resources-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        load_profile(Path(temporary))
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    commands = parser.add_subparsers(dest="action", required=True)
    importer = commands.add_parser("import-ar", help="Copy only four owner resources from an authorized workspace")
    importer.add_argument("--workspace", type=Path, required=True)
    importer.add_argument("--replace", action="store_true")
    commands.add_parser("status", help="Show presence and variable names, never values")
    runner = commands.add_parser("run", help="Inject one resource into an existing tool process")
    runner.add_argument("resource", choices=("document", *SEARCH))
    runner.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "import-ar":
            import_ar(args.workspace, args.profile, args.replace)
        data = load_profile(args.profile)
        if args.action in ("status", "import-ar"):
            print(json.dumps({"profile": str(args.profile), "resources": {
                name: {"configured": True, "environment_names": sorted(item["env"])}
                for name, item in data["resources"].items()
            }}, ensure_ascii=False, indent=2))
            return
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise ValueError("run requires an executable and arguments after --")
        os.execvpe(command[0], command, child_environment(data, args.resource))
    except (OSError, ValueError, KeyError, TypeError):
        # Do not emit exception bodies: they can contain private configuration values.
        print("Resource binding failed: check owner-only files, required settings and executable; no credential values were logged.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
