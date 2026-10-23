"""Register plugins command arguments."""

from __future__ import annotations

import argparse


def _add_plugin_parser(subparsers: argparse._SubParsersAction) -> None:
    plugin = subparsers.add_parser("plugin", help="Manage OURO plugins")
    commands = plugin.add_subparsers(dest="plugin_command", required=True)

    scaffold = commands.add_parser("scaffold", help="Create a minimal local user plugin")
    scaffold.add_argument("destination")
    scaffold.add_argument("--plugin-id", required=True)
    scaffold.add_argument("--name", required=True)
    scaffold.add_argument("--namespace")
    _add_plugin_output(scaffold)

    validate = commands.add_parser("validate", help="Validate a plugin package without installing")
    validate.add_argument("source")
    _add_plugin_source_args(validate)
    _add_plugin_output(validate)

    report = commands.add_parser("import-report", help="Inspect portable community resources")
    report.add_argument("source")
    _add_plugin_output(report)

    list_parser = commands.add_parser("list", help="List installed plugins")
    _add_plugin_output(list_parser)
    show = commands.add_parser("show", help="Show plugin provenance and state")
    show.add_argument("plugin_id")
    _add_plugin_output(show)

    configuration = commands.add_parser(
        "configuration", help="Inspect a plugin configuration contract"
    )
    configuration.add_argument("plugin_id")
    configuration.add_argument("--digest", help="Inspect this installed digest")
    configuration.add_argument(
        "--export",
        dest="export_path",
        help="Export a complete JSON editing draft, including saved keys",
    )
    _add_plugin_output(configuration)

    list_commands = commands.add_parser("commands", help="List active plugin commands")
    _add_plugin_output(list_commands)
    run_command = commands.add_parser("run-command", help="Run an active plugin command")
    run_command.add_argument("command_id")
    run_command.add_argument("argument", nargs="?", default="")
    run_command.add_argument("--session", dest="session_id")
    run_command.add_argument("--workspace")
    _add_plugin_output(run_command)

    install = commands.add_parser("install", help="Install immutable plugin bytes")
    install.add_argument("source")
    _add_plugin_source_args(install)
    _add_plugin_mutation_args(install)

    configure = commands.add_parser("configure", help="Set validated plugin configuration")
    configure.add_argument("plugin_id")
    config_input = configure.add_mutually_exclusive_group()
    config_input.add_argument("--values", help="Complete JSON values object")
    config_input.add_argument("--file", help="Complete JSON draft exported by plugin configuration")
    configure.add_argument(
        "--credentials",
        help='JSON slot bindings: "env:VARIABLE" or {"type":"inline","value":"key"}',
    )
    configure.add_argument("--digest", help="Configure an installed candidate digest")
    configure.add_argument(
        "--expected-revision", help="Require this configuration editing baseline"
    )
    _add_plugin_mutation_args(configure)

    select = commands.add_parser("select", help="Select one installed immutable digest")
    select.add_argument("plugin_id")
    select.add_argument("digest")
    select.add_argument("--expected-digest")
    _add_plugin_output(select)

    for name in ("enable", "disable", "restart", "check"):
        command = commands.add_parser(name, help=f"{name.title()} a plugin")
        command.add_argument("plugin_id")
        _add_plugin_output(command)

    update = commands.add_parser("update", help="Install an update side by side")
    update.add_argument("plugin_id")
    update.add_argument("source")
    _add_plugin_source_args(update)
    _add_plugin_request_id(update)
    _add_plugin_output(update)

    reload_parser = commands.add_parser(
        "reload",
        help="Validate, install, and atomically select local development bytes",
    )
    reload_parser.add_argument("plugin_id")
    reload_parser.add_argument("source")
    _add_plugin_source_args(reload_parser)
    _add_plugin_request_id(reload_parser)
    _add_plugin_output(reload_parser)

    rollback = commands.add_parser("rollback", help="Select and activate an installed digest")
    rollback.add_argument("plugin_id")
    rollback.add_argument("digest")
    _add_plugin_output(rollback)

    remove = commands.add_parser("remove", help="Remove an install while retaining data")
    remove.add_argument("plugin_id")
    remove.add_argument("--digest")
    remove.add_argument("--purge-data", action="store_true")
    _add_plugin_request_id(remove)
    _add_plugin_output(remove)

    purge = commands.add_parser(
        "purge-data", help="Delete a disabled or uninstalled plugin's private data"
    )
    purge.add_argument("plugin_id")
    _add_plugin_output(purge)

    maintain = commands.add_parser("maintain", help="Run a command on disabled private plugin data")
    maintain.add_argument("plugin_id")
    maintain.add_argument("--timeout", type=float, default=300, help="Command timeout in seconds")
    _add_plugin_output(maintain)
    maintain.add_argument("--execute", nargs=argparse.REMAINDER, required=True, help="Command argv")

    gc = commands.add_parser("gc", help="Remove unreferenced plugin cache entries")
    _add_plugin_output(gc)


def _add_plugin_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref")
    parser.add_argument("--subdirectory")


def _add_plugin_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preview", action="store_true")
    _add_plugin_request_id(parser)
    _add_plugin_output(parser)


def _add_plugin_request_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-id",
        help="Audit correlation id; it does not deduplicate repeated requests",
    )


def _add_plugin_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--json", action="store_true")
