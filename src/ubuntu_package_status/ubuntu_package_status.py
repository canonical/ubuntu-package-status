#!/usr/bin/env python3

import csv
import faulthandler
import json
import logging
import os
import sys
import yaml

import click
import humanize
import pytz

from collections import defaultdict
from datetime import datetime, timedelta
from itertools import product
from pkg_resources import resource_filename

from babel.dates import format_datetime
from joblib import Parallel, delayed
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
        for package, arches in package_stats.items():
            print("\t{}".format(package))
            for arch, pockets in arches.items():
                for pocket, stats in pockets.items():
                    if stats["full_version"]:
                        print(
                            "\t\t{} {} {} @ {} ({})".format(
                                arch,
                                pocket,
                                stats["full_version"],
                                stats["date_published_formatted"],
                                stats["published_age"],
                            )
                        )


def print_package_status_summary_csv(package_status):
    csv_stdout_writer = csv.writer(sys.stdout)
    csv_stdout_writer.writerow(
        [
            "ubuntu_version",
            "package",
            "pocket",
            "architecture"
            "full_version",
            "date_published",
            "date_published_formatted",
            "published_age",
            "link",
            "build_link"
        ]
    )
    for ubuntu_version, package_stats in package_status.items():
        for package, pockets in package_stats.items():
            for pocket, arches in pockets.items():
                for arch, stats in arches.items():
                    if stats["full_version"]:
                        csv_stdout_writer.writerow(
                            [
                                ubuntu_version,
                                package,
                                pocket,
                                arch,
                                stats["full_version"],
                                stats["date_published"],
                                stats["date_published_formatted"],
                                stats["published_age"],
                                stats["link"],
                                stats["build_link"],
                            ]
                        )

@delayed
def get_status_for_single_package_by_pocket_and_architecture(
    ubuntu_version, package, pocket, package_architecture
):
    package_stats = {
        "full_version": None,
        "version": None,
        "date_published": None,
        "date_published_formatted": None,
        "published_age": None,
        "link": None,
        "build_link": None
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

            version = binary_package_version

            package_stats["link"] = package_published_binary.self_link

            build_link = package_published_binary.build_link.replace('api.', '')\
                .replace('1.0/', '')\
                .replace('devel/', '')
            package_stats["build_link"] = build_link

            # We're really only concerned with the version number up
            # to the last int if it's not a ~ version
            if "~" not in binary_package_version:
                last_version_dot = binary_package_version.find('-')
                version = binary_package_version[0:last_version_dot]
            package_stats["version"] = version

            package_stats[
                "date_published"
            ] = package_published_binary.date_published.isoformat()
            date_published_formatted = format_datetime(
                package_published_binary.date_published
            )
            package_stats["date_published_formatted"] = date_published_formatted

            current_time = pytz.utc.localize(datetime.utcnow())
            published_age = current_time - package_published_binary.date_published
            if published_age < timedelta():
                # A negative timedelta means the time is in the future; this will be
                # due to inconsistent clocks across systems, so assume that there is no
                # delta
                published_age = timedelta()
            published_age = humanize.naturaltime(published_age)

            package_stats["published_age"] = published_age

    except Exception as e:
        logging.error(
            "Error querying launchpad API: %s. \n " "We will retry. \n", str(e)
        )

    return {"package": package,
            "pocket": pocket,
            "ubuntu_version": ubuntu_version,
            "architecture": package_architecture,
            "status": package_stats}


def initialize_package_stats_dict(package_config, package_architectures=["amd64"]):
    package_status = dict()

    ubuntu_versions = package_config.get("ubuntu-versions", {})

    # initialise package status
    for ubuntu_version, packages in ubuntu_versions.items():
        package_list = packages.get("packages", [])
        package_status.setdefault(
            ubuntu_version, defaultdict(dict)
        )
        for package in package_list:
            for pocket in ARCHIVE_POCKETS:
                package_status[ubuntu_version][package].setdefault(
                    pocket, defaultdict(dict)
                )
                for package_architecture in package_architectures:
                    package_status[ubuntu_version][package][pocket].setdefault(
                        package_architecture, defaultdict(dict)
                    )

    return package_status


def get_status_for_all_packages(package_config, package_architectures=["amd64"]):
    package_statuses = initialize_package_stats_dict(package_config, package_architectures)
    ubuntu_version_package_pocket_architecture_combinations = []
    for ubuntu_version, packages in package_statuses.items():
        # find all possible combinations of pocket, architecture and package name and create parallel jobs to query
        # launchpad for details on that package arch and pocket
        package_names = packages.keys()
        pockets = ARCHIVE_POCKETS

        # create a list of tuples of all combinations
        possible_combinations = list(product([ubuntu_version], package_names, pockets, package_architectures))
        ubuntu_version_package_pocket_architecture_combinations.extend(possible_combinations)

    n_jobs = -1
    single_package_statuses = Parallel(n_jobs=n_jobs)(
        get_status_for_single_package_by_pocket_and_architecture(ubuntu_version_name,
                                                                 package_name,
                                                                 package_pocket,
                                                                 package_architecture)
        for ubuntu_version_name, package_name, package_pocket, package_architecture in
        ubuntu_version_package_pocket_architecture_combinations
    )
    for single_package in single_package_statuses:
        package_name = single_package["package"]
        package_ubuntu_version = single_package["ubuntu_version"]
        package_pocket = single_package["pocket"]
        package_architecture = single_package["architecture"]
        package_status = single_package["status"]
        package_statuses[package_ubuntu_version][package_name][package_pocket][package_architecture] = package_status

    return package_statuses


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
    "--series", help='the Ubuntu series eg. "20.04" or "focal"', required=False, default=None
)
@click.option(
    "--package-name", "package_names", multiple=True, help='Binary package name', required=False, default=[]
)
@click.option(
    "--logging-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    required=False,
    default="ERROR",
    help="How detailed would you like the output.",
    show_default=True
)
@click.option(
    "--config-skeleton", is_flag=True, default=False, help="Print example config.",
    show_default=True
)
@click.option(
    "--output-format", type=click.Choice(["TXT", "CSV", "JSON"]), default="TXT",
    show_default=True
)
@click.option(
    "--package-architecture", "package_architectures",
    help="The architecture to use when querying package "
    "version in the archive. We use this in our Launchpad "
    'query to query either "source" package or "amd64" package '
    'version. Using "amd64" will query the version of the '
    'binary package. "source" is a valid value for '
    "architecture with Launchpad and will query the version of "
    "the source package. The default is amd64. "
    "This option can be specified multiple times.",
    required=True,
    multiple=True,
    default=["amd64"],
    show_default=True
)
@click.pass_context
def ubuntu_package_status(
    ctx, config, series, package_names, logging_level, config_skeleton, output_format, package_architectures
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

    # was there a config passed in or individual package?
    if not series and not package_names:
        # Parse config
        with open(config, "r") as config_file:
            package_config = yaml.safe_load(config_file)
            if config_skeleton:
                output = yaml.dump(package_config, Dumper=yaml.Dumper)
                print(output)
                exit(0)
    else:
        package_config = {
            'ubuntu-versions':
                {
                    series:
                        {
                            'packages': package_names
                        }
                }
        }

    # Initialise all package version
    package_status = get_status_for_all_packages(package_config, package_architectures)
    print_package_status(package_status, output_format)


if __name__ == "__main__":
    ubuntu_package_status(obj={})
