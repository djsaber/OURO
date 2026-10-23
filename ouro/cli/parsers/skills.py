"""Register skills command arguments."""

from __future__ import annotations

import argparse


def _add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    skills_parser = subparsers.add_parser("skills", help="Manage Agent Skills")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)

    skills_list_parser = skills_subparsers.add_parser("list", help="List available skills")
    skills_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_list_parser.add_argument("--json", action="store_true", help="Print JSON")
    skills_list_parser.add_argument(
        "--all",
        dest="skills_all",
        action="store_true",
        help="Include disabled skills",
    )

    skills_show_parser = skills_subparsers.add_parser("show", help="Show a skill")
    skills_show_parser.add_argument("skill_name", help="Skill name")
    skills_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_roots_parser = skills_subparsers.add_parser("roots", help="Show skill search roots")
    skills_roots_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_roots_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_prompt_parser = skills_subparsers.add_parser(
        "prompt",
        help="Preview injected skills prompt",
    )
    skills_prompt_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_prompt_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_validate_parser = skills_subparsers.add_parser(
        "validate",
        help="Validate one skill, a skill collection, or all discovered skills",
    )
    skills_validate_parser.add_argument("path", nargs="?", help="SKILL.md or directory")
    skills_validate_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_validate_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install skills from a local directory or git URL",
    )
    skills_install_parser.add_argument("source", help="Local path, git URL, or github:owner/repo")
    skills_install_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_install_parser.add_argument(
        "--scope",
        choices=["user", "workspace"],
        default="user",
        help="Install into OURO_HOME/skills or ./.ouro/skills",
    )
    install_selection = skills_install_parser.add_mutually_exclusive_group()
    install_selection.add_argument("--skill", dest="skill_selector", help="Select one skill")
    install_selection.add_argument(
        "--all",
        dest="install_all",
        action="store_true",
        help="Install every valid skill in the source",
    )
    skills_install_parser.add_argument("--ref", help="Git branch or tag")
    skills_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing skill atomically",
    )
    skills_install_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_update_parser = skills_subparsers.add_parser(
        "update",
        help="Update an Ouro-managed skill from its recorded source",
    )
    skills_update_parser.add_argument("skill_name", help="Skill name")
    skills_update_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_update_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_remove_parser = skills_subparsers.add_parser(
        "remove",
        help="Remove an Ouro-managed skill",
    )
    skills_remove_parser.add_argument("skill_name", help="Skill name")
    skills_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_remove_parser.add_argument(
        "--force",
        action="store_true",
        help="Remove an unregistered skill from an Ouro-native skill directory",
    )
    skills_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    for command, help_text in (
        ("enable", "Enable a discovered skill"),
        ("disable", "Disable a discovered skill"),
    ):
        command_parser = skills_subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("skill_name", help="Skill name")
        command_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
        command_parser.add_argument("--json", action="store_true", help="Print JSON")
