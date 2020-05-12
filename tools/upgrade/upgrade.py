# Copyright (c) 2016-present, Facebook, Inc.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import json
import logging
import subprocess
import sys
import traceback
from logging import Logger
from pathlib import Path
from typing import List

from ...client.commands import ExitCode
from . import UserError
from .ast import UnstableAST
from .codemods import MissingGlobalAnnotations, MissingOverrideReturnAnnotations
from .commands.command import Command, ErrorSuppressingCommand
from .commands.fixme import Fixme
from .configuration import Configuration
from .errors import Errors, errors_from_targets
from .filesystem import (
    FilesystemException,
    LocalMode,
    add_local_mode,
    find_files,
    find_targets,
    get_filesystem,
    path_exists,
    remove_non_pyre_ignores,
)
from .repository import Repository


LOG: Logger = logging.getLogger(__name__)


class StrictDefault(Command):
    def run(self) -> None:
        project_configuration = Configuration.find_project_configuration()
        if project_configuration is None:
            LOG.info("No project configuration found for the given directory.")
            return
        local_configuration = self._arguments.local_configuration
        if local_configuration:
            configuration_path = local_configuration / ".pyre_configuration.local"
        else:
            configuration_path = project_configuration
        with open(configuration_path) as configuration_file:
            configuration = Configuration(
                configuration_path, json.load(configuration_file)
            )
            LOG.info("Processing %s", configuration.get_directory())
            configuration.add_strict()
            configuration.write()
            errors = configuration.get_errors()

            if len(errors) == 0:
                return
            for filename, _ in errors:
                add_local_mode(filename, LocalMode.UNSAFE)

            if self._arguments.lint:
                self._repository.format()


class GlobalVersionUpdate(Command):
    def run(self) -> None:
        global_configuration = Configuration.find_project_configuration()
        if global_configuration is None:
            LOG.error("No global configuration file found.")
            return

        with open(global_configuration, "r") as global_configuration_file:
            configuration = json.load(global_configuration_file)
            if "version" not in configuration:
                LOG.error(
                    "Global configuration at %s has no version field.",
                    global_configuration,
                )
                return

            old_version = configuration["version"]

        # Rewrite.
        with open(global_configuration, "w") as global_configuration_file:
            configuration["version"] = self._arguments.hash

            # This will sort the keys in the configuration - we won't be clobbering
            # comments since Python's JSON parser disallows comments either way.
            json.dump(
                configuration, global_configuration_file, sort_keys=True, indent=2
            )
            global_configuration_file.write("\n")

        paths = self._arguments.paths
        configuration_paths = (
            [path / ".pyre_configuration.local" for path in paths]
            if paths
            else [
                configuration.get_path()
                for configuration in Configuration.gather_local_configurations()
                if configuration.is_local
            ]
        )
        for configuration_path in configuration_paths:
            if "mock_repository" in str(configuration_path):
                # Skip local configurations we have for testing.
                continue
            with open(configuration_path) as configuration_file:
                contents = json.load(configuration_file)
                if "version" in contents:
                    LOG.info(
                        "Skipping %s as it already has a custom version field.",
                        configuration_path,
                    )
                    continue
                if contents.get("differential"):
                    LOG.info(
                        "Skipping differential configuration at `%s`",
                        configuration_path,
                    )
                    continue
                contents["version"] = old_version

            with open(configuration_path, "w") as configuration_file:
                json.dump(contents, configuration_file, sort_keys=True, indent=2)
                configuration_file.write("\n")

        try:
            self._repository.submit_changes(
                commit=True,
                submit=self._arguments.submit,
                title="Update pyre global configuration version",
                summary=f"Automatic upgrade to hash `{self._arguments.hash}`",
                ignore_failures=True,
            )
        except subprocess.CalledProcessError:
            action = "submit" if self._arguments.submit else "commit"
            raise FilesystemException(f"Error while attempting to {action} changes.")


class FixmeSingle(ErrorSuppressingCommand):
    def run(self) -> None:
        project_configuration = Configuration.find_project_configuration()
        if project_configuration is None:
            LOG.info("No project configuration found for the given directory.")
            return
        configuration_path = self._arguments.path / ".pyre_configuration.local"
        with open(configuration_path) as configuration_file:
            configuration = Configuration(
                configuration_path, json.load(configuration_file)
            )
            self._suppress_errors_in_project(
                configuration, project_configuration.parent
            )


class FixmeAll(ErrorSuppressingCommand):
    def run(self) -> None:
        project_configuration = Configuration.find_project_configuration()
        if project_configuration is None:
            LOG.info("No project configuration found for the current directory.")
            return

        configurations = Configuration.gather_local_configurations()
        for configuration in configurations:
            self._suppress_errors_in_project(
                configuration, project_configuration.parent
            )


class FixmeTargets(ErrorSuppressingCommand):
    def run(self) -> None:
        subdirectory = self._arguments.subdirectory
        subdirectory = Path(subdirectory) if subdirectory else None
        project_configuration = Configuration.find_project_configuration(subdirectory)
        if project_configuration is None:
            LOG.error("No project configuration found for the given directory.")
            return
        project_directory = project_configuration.parent
        search_root = subdirectory if subdirectory else project_directory

        all_targets = find_targets(search_root)
        if not all_targets:
            return
        for path, target_names in all_targets.items():
            self._run_fixme_targets_file(project_directory, path, target_names)
        try:
            self._repository.submit_changes(
                commit=(not self._arguments.no_commit),
                submit=self._arguments.submit,
                title=f"Upgrade pyre version for {search_root} (TARGETS)",
            )
        except subprocess.CalledProcessError:
            action = "submit" if self._arguments.submit else "commit"
            raise FilesystemException(f"Error while attempting to {action} changes.")

    def _run_fixme_targets_file(
        self, project_directory: Path, path: str, target_names: List[str]
    ) -> None:
        LOG.info("Processing %s/TARGETS...", path)
        targets = [path + ":" + name + "-pyre-typecheck" for name in target_names]
        errors = errors_from_targets(project_directory, path, targets)
        if not errors:
            return
        LOG.info("Found %d type errors in %s/TARGETS.", len(errors), path)

        if not errors:
            return

        self._suppress_errors(errors)

        if not self._arguments.lint:
            return

        if self._repository.format():
            errors = errors_from_targets(project_directory, path, targets)
            if not errors:
                LOG.info("Errors unchanged after linting.")
                return
            LOG.info("Found %d type errors after linting.", len(errors))
            self._suppress_errors(errors)


class MigrateTargets(Command):
    def run(self) -> None:
        subdirectory = self._arguments.subdirectory
        subdirectory = Path(subdirectory) if subdirectory else Path.cwd()
        LOG.info("Migrating typecheck targets in {}".format(subdirectory))

        # Remove explicit check types options.
        targets_files = [
            str(subdirectory / path)
            for path in get_filesystem().list(
                str(subdirectory), patterns=[r"**/TARGETS"]
            )
        ]
        LOG.info("...found {} targets files".format(len(targets_files)))
        remove_check_types_command = [
            "sed",
            "-i",
            r'/check_types_options \?= \?"mypy",/d',
        ] + targets_files
        remove_options_command = [
            "sed",
            "-i",
            r's/typing_options \?= \?".*strict",/check_types_options = "strict",/g',
        ] + targets_files
        subprocess.check_output(remove_check_types_command)
        subprocess.check_output(remove_options_command)

        remove_non_pyre_ignores(subdirectory)
        FixmeTargets(self._arguments, self._repository).run()


class TargetsToConfiguration(ErrorSuppressingCommand):
    def remove_target_typing_fields(self, files: List[str]) -> None:
        LOG.info("Removing typing options from %s targets files", len(files))
        typing_options_regex = [
            r"typing \?=.*",
            r"check_types \?=.*",
            r"check_types_options \?=.*",
            r"typing_options \?=.*",
        ]
        remove_typing_fields_command = [
            "sed",
            "-i",
            "/" + r"\|".join(typing_options_regex) + "/d",
        ] + files
        subprocess.run(remove_typing_fields_command)

    def convert_directory(self, directory: Path) -> None:
        all_targets = find_targets(directory)
        if not all_targets:
            LOG.warning("No configuration created because no targets found.")
            return
        targets_files = [
            str(directory / path)
            for path in get_filesystem().list(str(directory), patterns=[r"**/TARGETS"])
        ]
        if self._arguments.glob:
            new_targets = ["//" + str(directory) + "/..."]
        else:
            new_targets = []
            for path, target_names in all_targets.items():
                new_targets += ["//" + path + ":" + name for name in target_names]

        configuration_path = directory / ".pyre_configuration.local"
        if path_exists(str(configuration_path)):
            LOG.warning(
                "Pyre project already exists at %s.\n\
                Amending targets to existing configuration.",
                configuration_path,
            )
            with open(configuration_path) as configuration_file:
                configuration = Configuration(
                    configuration_path, json.load(configuration_file)
                )
                configuration.add_targets(new_targets)
                configuration.deduplicate_targets()
                configuration.write()
        else:
            LOG.info("Creating local configuration at %s.", configuration_path)
            configuration_contents = {"targets": new_targets, "strict": True}
            # Heuristic: if all targets with type checked targets are setting
            # a target to be strictly checked, let's turn on default strict.
            for targets_file in targets_files:
                regex_patterns = [
                    r"check_types_options \?=.*strict.*",
                    r"typing_options \?=.*strict.*",
                ]
                result = subprocess.run(
                    ["grep", "-x", r"\|".join(regex_patterns), targets_file]
                )
                if result.returncode != 0:
                    configuration_contents["strict"] = False
                    break
            configuration = Configuration(configuration_path, configuration_contents)
            configuration.write()

            # Add newly created configuration files to version control
            self._repository.add_paths([configuration_path])

        # Remove all type-related target settings
        self.remove_target_typing_fields(targets_files)
        remove_non_pyre_ignores(directory)

        all_errors = configuration.get_errors()
        error_threshold = self._arguments.fixme_threshold
        glob_threshold = self._arguments.glob

        for path, errors in all_errors:
            errors = list(errors)
            error_count = len(errors)
            if glob_threshold and error_count > glob_threshold:
                # Fall back to non-glob codemod.
                LOG.info(
                    "Exceeding error threshold of %d; falling back to listing "
                    "individual targets.",
                    glob_threshold,
                )
                self._repository.revert_all(remove_untracked=True)
                self._arguments.glob = None
                return self.run()
            if error_threshold and error_count > error_threshold:
                LOG.info(
                    "%d errors found in `%s`. Adding file-level ignore.",
                    error_count,
                    path,
                )
                add_local_mode(path, LocalMode.IGNORE)
            else:
                self._suppress_errors(Errors(errors))

        # Lint and re-run pyre once to resolve most formatting issues
        if self._arguments.lint:
            if self._repository.format():
                errors = configuration.get_errors(should_clean=False)
                self._suppress_errors(errors)

    def run(self) -> None:
        # TODO(T62926437): Basic integration testing.
        subdirectory = self._arguments.subdirectory
        subdirectory = Path(subdirectory) if subdirectory else Path.cwd()
        LOG.info(
            "Converting typecheck targets to pyre configurations in `%s`", subdirectory
        )

        configurations = find_files(subdirectory, ".pyre_configuration.local")
        configuration_directories = sorted(
            Path(configuration.replace("/.pyre_configuration.local", ""))
            for configuration in configurations
        )
        if len(configuration_directories) == 0:
            configuration_directories = [subdirectory]
        converted = []
        for directory in configuration_directories:
            if all(
                str(directory).startswith(str(converted_directory)) is False
                for converted_directory in converted
            ):
                self.convert_directory(directory)
                converted.append(directory)

        try:
            summary = self._repository.MIGRATION_SUMMARY
            glob = self._arguments.glob
            if glob:
                summary += (
                    f"\n\nConfiguration target automatically expanded to include "
                    f"all subtargets, expanding type coverage while introducing "
                    f"no more than {glob} fixmes per file."
                )
            title = f"Convert type check targets in {subdirectory} to use configuration"
            self._repository.submit_changes(
                commit=(not self._arguments.no_commit),
                submit=self._arguments.submit,
                title=title,
                summary=summary,
                set_dependencies=False,
            )
        except subprocess.CalledProcessError:
            action = "submit" if self._arguments.submit else "commit"
            raise FilesystemException(f"Error while attempting to {action} changes.")


class ExpandTargetCoverage(ErrorSuppressingCommand):
    def run(self) -> None:
        subdirectory = self._arguments.subdirectory
        subdirectory = Path(subdirectory) if subdirectory else Path.cwd()

        # Do not change if configurations exist below given root
        existing_configurations = find_files(subdirectory, ".pyre_configuration.local")
        if existing_configurations and not existing_configurations == [
            str(subdirectory / ".pyre_configuration.local")
        ]:
            LOG.warning(
                "Cannot expand targets because nested configurations exist:\n%s",
                "\n".join(existing_configurations),
            )
            return

        # Expand coverage
        local_configuration = Configuration.find_local_configuration(subdirectory)
        if not local_configuration:
            LOG.warning("Could not find a local configuration to codemod.")
            return
        LOG.info("Expanding typecheck targets in `%s`", local_configuration)
        with open(local_configuration) as configuration_file:
            configuration = Configuration(
                local_configuration, json.load(configuration_file)
            )
            configuration.add_targets(["//" + str(subdirectory) + "/..."])
            configuration.deduplicate_targets()
            configuration.write()

        # Suppress errors
        all_errors = configuration.get_errors()
        error_threshold = self._arguments.fixme_threshold

        for path, errors in all_errors:
            errors = list(errors)
            error_count = len(errors)
            if error_threshold and error_count > error_threshold:
                LOG.info(
                    "%d errors found in `%s`. Adding file-level ignore.",
                    error_count,
                    path,
                )
                add_local_mode(path, LocalMode.IGNORE)
            else:
                self._suppress_errors(Errors(errors))

        # Lint and re-run pyre once to resolve most formatting issues
        if self._arguments.lint:
            if self._repository.format():
                errors = configuration.get_errors(should_clean=False)
                self._suppress_errors(errors)

        try:
            self._repository.submit_changes(
                commit=(not self._arguments.no_commit),
                submit=self._arguments.submit,
                title=f"Expand target type coverage in {local_configuration}",
                summary="Expanding type coverage of targets in configuration.",
                set_dependencies=False,
            )
        except subprocess.CalledProcessError:
            action = "submit" if self._arguments.submit else "commit"
            raise FilesystemException(f"Error while attempting to {action} changes.")


class ConsolidateNestedConfigurations(ErrorSuppressingCommand):
    def run(self) -> None:
        subdirectory = self._arguments.subdirectory
        subdirectory = Path(subdirectory) if subdirectory else Path.cwd()

        # Find configurations
        configurations = sorted(find_files(subdirectory, ".pyre_configuration.local"))
        if not configurations:
            LOG.warning(
                f"Skipping consolidation. No configurations found in {subdirectory}"
            )
            return
        if len(configurations) == 1:
            configuration = configurations[0]
            LOG.warning(
                f"Skipping consolidation. Only one configuration found: {configuration}"
            )
            return

        # Gather nesting structure of configurations
        nested_configurations = {}
        for configuration in configurations:
            if len(nested_configurations) == 0:
                nested_configurations[configuration] = []
                continue
            inserted = False
            for topmost_configuration in nested_configurations.keys():
                existing = topmost_configuration.replace(
                    "/.pyre_configuration.local", ""
                )
                current = configuration.replace("/.pyre_configuration.local", "")
                if current.startswith(existing):
                    nested_configurations[topmost_configuration].append(configuration)
                    inserted = True
                    break
                elif existing.startswith(current):
                    nested_configurations[configuration] = nested_configurations[
                        topmost_configuration
                    ] + [topmost_configuration]
                    del nested_configurations[topmost_configuration]
                    inserted = True
                    break
            if not inserted:
                nested_configurations[configuration] = []

        # Consolidate targets
        for topmost, nested in nested_configurations.items():
            if len(nested) == 0:
                continue
            total_targets = []
            for nested_configuration in nested:
                with open(nested_configuration) as configuration_file:
                    configuration = Configuration(
                        Path(nested_configuration), json.load(configuration_file)
                    )
                    targets = configuration.targets
                    if targets:
                        total_targets.extend(targets)
            with open(topmost) as configuration_file:
                configuration = Configuration(
                    Path(topmost), json.load(configuration_file)
                )
                configuration.add_targets(total_targets)
                configuration.deduplicate_targets()
                configuration.write()
            self._repository.remove_paths(nested)

            # Suppress errors
            all_errors = configuration.get_errors()
            for _, errors in all_errors:
                self._suppress_errors(Errors(list(errors)))

        try:
            self._repository.submit_changes(
                commit=(not self._arguments.no_commit),
                submit=self._arguments.submit,
                title=f"Consolidate configurations in {subdirectory}",
                summary="Consolidating nested configurations.",
                set_dependencies=False,
            )
        except subprocess.CalledProcessError:
            action = "submit" if self._arguments.submit else "commit"
            raise FilesystemException(f"Error while attempting to {action} changes.")


def run(repository: Repository) -> None:
    parser = argparse.ArgumentParser(fromfile_prefix_chars="@")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate error messages to maximum line length.",
    )
    parser.add_argument(
        "--max-line-length",
        default=88,
        type=int,
        help="Enforce maximum line length on new comments "
        + "(default: %(default)s, use 0 to set no maximum line length)",
    )
    parser.add_argument(
        "--only-fix-error-code",
        type=int,
        help="Only add fixmes for errors with this specific error code.",
        default=None,
    )

    commands = parser.add_subparsers()

    # Subcommands: Codemods
    missing_overridden_return_annotations = commands.add_parser(
        "missing-overridden-return-annotations",
        help="Add annotations according to errors inputted through stdin.",
    )
    missing_overridden_return_annotations.set_defaults(
        command=MissingOverrideReturnAnnotations
    )

    missing_global_annotations = commands.add_parser(
        "missing-global-annotations",
        help="Add annotations according to errors inputted through stdin.",
    )
    missing_global_annotations.set_defaults(command=MissingGlobalAnnotations)

    # Subcommand: Change default pyre mode to strict and adjust module headers.
    strict_default = commands.add_parser("strict-default")
    strict_default.set_defaults(command=StrictDefault)
    strict_default.add_argument(
        "-l",
        "--local-configuration",
        type=path_exists,
        help="Path to project root with local configuration",
    )
    strict_default.add_argument(
        # TODO(T53195818): Not implemented
        "--remove-strict-headers",
        action="store_true",
        help="Delete unnecessary `# pyre-strict` headers.",
    )
    strict_default.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)

    # Subcommand: Set global configuration to given hash, and add version override
    # to all local configurations to run previous version.
    update_global_version = commands.add_parser("update-global-version")
    update_global_version.set_defaults(command=GlobalVersionUpdate)
    update_global_version.add_argument("hash", help="Hash of new Pyre version")
    update_global_version.add_argument(
        "--paths",
        nargs="*",
        help="A list of paths to local Pyre projects.",
        default=[],
        type=path_exists,
    )
    update_global_version.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )

    # Subcommand: Fixme all errors inputted through stdin.
    fixme = commands.add_parser("fixme")
    fixme.set_defaults(command=Fixme)
    fixme.add_argument("--error-source", choices=["stdin", "generate"], default="stdin")
    fixme.add_argument("--comment", help="Custom comment after fixme comments")
    fixme.add_argument(
        "--unsafe", action="store_true", help="Don't check syntax when applying fixmes."
    )
    fixme.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)

    # Subcommand: Fixme all errors for a single project.
    fixme_single = commands.add_parser("fixme-single")
    fixme_single.set_defaults(command=FixmeSingle)
    fixme_single.add_argument(
        "path", help="Path to project root with local configuration", type=path_exists
    )
    fixme_single.add_argument(
        "--upgrade-version",
        action="store_true",
        help="Upgrade and clean project if a version override set.",
    )
    fixme_single.add_argument(
        "--error-source", choices=["stdin", "generate"], default="generate"
    )
    fixme_single.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_single.add_argument(
        "--no-commit", action="store_true", help=argparse.SUPPRESS
    )
    fixme_single.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)
    fixme_single.add_argument(
        "--unsafe", action="store_true", help="Don't check syntax when applying fixmes."
    )

    # Subcommand: Fixme all errors in all projects with local configurations.
    fixme_all = commands.add_parser("fixme-all")
    fixme_all.set_defaults(command=FixmeAll)
    fixme_all.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    fixme_all.add_argument(
        "--upgrade-version",
        action="store_true",
        help="Upgrade and clean projects with a version override set.",
    )
    fixme_all.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_all.add_argument("--no-commit", action="store_true", help=argparse.SUPPRESS)
    fixme_all.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)

    # Subcommand: Fixme all errors in targets running type checking
    fixme_targets = commands.add_parser("fixme-targets")
    fixme_targets.set_defaults(command=FixmeTargets)
    fixme_targets.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    fixme_targets.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_targets.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)
    fixme_targets.add_argument(
        "--subdirectory", help="Only upgrade TARGETS files within this directory."
    )
    fixme_targets.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Subcommand: Migrate and fixme errors in targets running type checking
    migrate_targets = commands.add_parser("migrate-targets")
    migrate_targets.set_defaults(command=MigrateTargets)
    migrate_targets.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    migrate_targets.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )
    migrate_targets.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)
    migrate_targets.add_argument(
        "--subdirectory", help="Only upgrade TARGETS files within this directory."
    )
    migrate_targets.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Subcommand: Remove targets integration and replace with configuration
    targets_to_configuration = commands.add_parser("targets-to-configuration")
    targets_to_configuration.set_defaults(command=TargetsToConfiguration)
    targets_to_configuration.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    targets_to_configuration.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )
    targets_to_configuration.add_argument(
        "--lint", action="store_true", help=argparse.SUPPRESS
    )
    targets_to_configuration.add_argument(
        "--subdirectory", help="Only upgrade TARGETS files within this directory."
    )
    targets_to_configuration.add_argument(
        "--glob",
        type=int,
        help="Use a toplevel glob target instead of listing individual targets. \
        Fall back to individual targets if errors per file ever hits given threshold.",
    )
    targets_to_configuration.add_argument(
        "--fixme-threshold",
        type=int,
        help="Ignore all errors in a file if fixme count exceeds threshold.",
    )
    targets_to_configuration.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Subcommand: Expand target coverage in configuration up to given error limit
    expand_target_coverage = commands.add_parser("expand-target-coverage")
    expand_target_coverage.set_defaults(command=ExpandTargetCoverage)
    expand_target_coverage.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    expand_target_coverage.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )
    expand_target_coverage.add_argument(
        "--lint", action="store_true", help=argparse.SUPPRESS
    )
    expand_target_coverage.add_argument(
        "--subdirectory", help="Only upgrade TARGETS files within this directory."
    )
    expand_target_coverage.add_argument(
        "--fixme-threshold",
        type=int,
        help="Ignore all errors in a file if fixme count exceeds threshold.",
    )
    expand_target_coverage.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Subcommand: Consolidate nested local configurations
    consolidate_nested_configurations = commands.add_parser("consolidate-nested")
    consolidate_nested_configurations.set_defaults(
        command=ConsolidateNestedConfigurations
    )
    consolidate_nested_configurations.add_argument(
        "-c", "--comment", help="Custom comment after fixme comments"
    )
    consolidate_nested_configurations.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )
    consolidate_nested_configurations.add_argument(
        "--lint", action="store_true", help=argparse.SUPPRESS
    )
    consolidate_nested_configurations.add_argument("--subdirectory")
    consolidate_nested_configurations.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Initialize default values.
    arguments = parser.parse_args()
    if not hasattr(arguments, "command"):
        arguments.command = Fixme
        arguments.error_source = "stdin"

    # Initialize values that may be null-checked, but do not exist as a flag
    # for all subcommands
    if not hasattr(arguments, "paths"):
        arguments.paths = None
    if not hasattr(arguments, "error_source"):
        arguments.error_source = None
    if not hasattr(arguments, "comment"):
        arguments.comment = None

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG if arguments.verbose else logging.INFO,
    )

    try:
        exit_code = ExitCode.SUCCESS
        arguments.command(arguments, repository).run()
    except UnstableAST as error:
        LOG.error(str(error))
        exit_code = ExitCode.FOUND_ERRORS
    except UserError as error:
        LOG.error(str(error))
        exit_code = ExitCode.FAILURE
    except Exception as error:
        LOG.error(str(error))
        LOG.debug(traceback.format_exc())
        exit_code = ExitCode.FAILURE

    sys.exit(exit_code)


def main() -> None:
    run(Repository())


if __name__ == "__main__":
    main()
