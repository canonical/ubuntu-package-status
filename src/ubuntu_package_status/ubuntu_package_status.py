#!/usr/bin/env python3

import csv
import faulthandler
import json
import logging
import os
import sys
import yaml

import click
from joblib import Parallel, delayed

from pkg_resources import resource_filename

from babel.dates import format_datetime
from launchpadlib.launchpad import Launchpad

# Which archive pockets are checked
ARCHIVE_POCKETS = ["Release", "Proposed", "Security", "Updates"]

faulthandler.enable()


def print_package_status(package_status, output_format="TXT"):
    if output_format == "JSON":
        print(json.dumps(package_status, indent=4))
    elif output_format == "CSV":
        print_package_status_summary_csv(package_status)
    else:
        print_package_status_summary_txt(package_status)


def print_package_status_summary_txt(package_status):
    for ubuntu_version, package_stats in package_status.items():
        print(ubuntu_version)
        for package, pockets in package_stats.items():
            print("\t{}".format(package))
            for pocket, stats in pockets.items():
                if stats["full_version"]:
                    print(
                        "\t\t{} {} @ {} ({})".format(
                            pocket,
                            stats["full_version"],
                            stats["date_published"],
                            stats["date_published_formatted"],
                        )
                    )


def print_package_status_summary_csv(package_status):
    csv_stdout_writer = csv.writer(sys.stdout)
    csv_stdout_writer.writerow(
        [
            "ubuntu_version",
            "package",
            "pocket",
            "full_version",
            "date_published",
            "date_published_formatted",
        ]
    )
    for ubuntu_version, package_stats in package_status.items():
        for package, pockets in package_stats.items():
            for pocket, stats in pockets.items():
                if stats["full_version"]:
                    csv_stdout_writer.writerow(
                        [
                            ubuntu_version,
                            package,
                            pocket,
                            stats["full_version"],
                            stats["date_published"],
                            stats["date_published_formatted"],
                        ]
                    )

@delayed
def get_status_for_single_package_by_pocket(
    ubuntu_version, package, pocket, package_architecture
):
    package_stats = {
        "full_version": None,
        "date_published": None,
        "date_published_formatted": None,
    }
    try:

        # Log in to launchpad annonymously - we use launchpad to find
        # the package publish time
        launchpad = Launchpad.login_anonymously(
            "ubuntu-package-status", "production", version="devel"
        )
        ubuntu = launchpad.distributions["ubuntu"]
        ubuntu_archive = ubuntu.main_archive

        lp_series = ubuntu.getSeries(name_or_version=ubuntu_version)
        lp_arch_series = lp_series.getDistroArchSeries(archtag=package_architecture)

        package_published_binaries = ubuntu_archive.getPublishedBinaries(
            exact_match=True,
            binary_name=package,
            pocket=pocket,
            distro_arch_series=lp_arch_series,
            status="Published",
            order_by_date=True,
        )

        if len(package_published_binaries) > 0:
            package_published_binary = package_published_binaries[0]
            binary_package_version = package_published_binary.binary_package_version
            package_stats["full_version"] = binary_package_version

            package_stats[
                "date_published"
            ] = package_published_binary.date_published.isoformat()
            date_published_formatted = format_datetime(
                package_published_binary.date_published
            )
            package_stats["date_published_formatted"] = date_published_formatted

    except Exception as e:
        logging.error(
            "Error querying launchpad API: %s. \n " "We will retry. \n", str(e)
        )

    return {"pocket": pocket,
            "status": package_stats}

@delayed
def get_status_for_single_package(
    ubuntu_version, package, pockets, package_architecture
):
    n_jobs = -1
    single_package_statuses = Parallel(n_jobs=n_jobs)(
        get_status_for_single_package_by_pocket(ubuntu_version, package, pocket,
                                      package_architecture)
        for pocket in pockets
    )
    package_status = {}
    for single_package_status in single_package_statuses:
        single_package_status_pocket = single_package_status["pocket"]
        package_status[single_package_status_pocket] = single_package_status["status"]

    return {"package": package,
            "ubuntu_version": ubuntu_version,
            "status": package_status}


def initialize_package_stats_dict(package_config):
    package_status = dict()

    default_package_stats = {
        "full_version": None,
        "date_published": None,
        "date_published_formatted": None,
    }

    default_package_versions = {
        "Release": default_package_stats,
        "Proposed": default_package_stats,
        "Updates": default_package_stats,
        "Security": default_package_stats,
    }

    ubuntu_versions = package_config.get("ubuntu-versions", {})

    # initialise package status
    for ubuntu_version, packages in ubuntu_versions.items():
        package_list = packages.get("packages", [])
        package_status.setdefault(
            ubuntu_version,
            {package: default_package_versions.copy() for package in package_list},
        )
    return package_status


def get_status_for_all_packages(package_config, package_architecture="amd64"):
    package_status = initialize_package_stats_dict(package_config)
    for ubuntu_version, packages in package_status.items():

        n_jobs = -1
        package_statuses = Parallel(n_jobs=n_jobs)(
            get_status_for_single_package(ubuntu_version, package, ARCHIVE_POCKETS,
                                          package_architecture)
            for package in packages.keys()
        )
        for single_package in package_statuses:
            single_package_name = single_package["package"]
            single_package_ubuntu_version = single_package["ubuntu_version"]
            single_package_status = single_package["status"]
            for pocket, pocket_status in single_package_status.items():
                package_status[single_package_ubuntu_version][single_package_name][pocket] = pocket_status

    return package_status


@click.command()
@click.option(
    "--config",
    required=False,
    default=resource_filename("ubuntu_package_status", "dist-config.yaml"),
    help="Config yaml specifying which packages ubuntu versions to watch."
    "{}".format(
        " When using the ubuntu-package-status snap this"
        " config must reside under $HOME."
        if os.environ.get("SNAP", None)
        else ""
    ),
)
@click.option(
    "--logging-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    required=False,
    default="ERROR",
    help="How detailed would you like the output.",
)
@click.option(
    "--config-skeleton", is_flag=True, default=False, help="Print example config."
)
@click.option(
    "--output-format", type=click.Choice(["TXT", "CSV", "JSON"]), default="TXT"
)
@click.option(
    "--package-architecture",
    help="The architecture to use when querying package "
    "version in the archive. We use this in our Launchpad "
    'query to query either "source" package or "amd64" package '
    'version. Using "amd64" will query the version of the '
    'binary package. "source" is a valid value for '
    "architecture with Launchpad and will query the version of "
    "the source package. The default is amd64.",
    required=True,
    default="amd64",
)
@click.pass_context
def ubuntu_package_status(
    ctx, config, logging_level, config_skeleton, output_format, package_architecture
):
    # type: (Dict, Text, Text, bool, Text, Text) -> None
    """
    Watch specified packages in the ubuntu archive for transition between
    archive pockets. Useful when waiting for a package update to be published.

    Usage:
    python ubuntu_package_status.py \
    --config="your-ubuntu-package-status-config.yaml"
    """

    # We log to stderr so that a shell calling this will not have logging
    # output in the $() capture.
    level = logging.getLevelName(logging_level)
    logging.basicConfig(
        level=level, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    # Parse config
    with open(config, "r") as config_file:
        package_config = yaml.safe_load(config_file)
        if config_skeleton:
            output = yaml.dump(package_config, Dumper=yaml.Dumper)
            print(output)
            exit(0)

    # Initialise all package version
    package_status = get_status_for_all_packages(package_config, package_architecture)
    print_package_status(package_status, output_format)


if __name__ == "__main__":
    ubuntu_package_status(obj={})
