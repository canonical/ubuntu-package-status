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
from launchpadlib.credentials import UnencryptedFileCredentialStore
from launchpadlib.launchpad import Launchpad
from launchpadlib.uris import service_roots

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
            for pocket, pockets in arches.items():
                for arch, stats in pockets.items():
                    if stats["full_version"]:
                        print(
                            "\t\t{}/{} {} {} @ {} ({})".format(
                                pocket,
                                stats["component"],
                                arch,
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
            "component",
            "architecture",
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
                                stats["component"],
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
    ubuntu_version, package, pocket, package_architecture, lp_user=None, lp_credentials_store=None
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


        if not lp_credentials_store:
            creds_prefix = os.environ.get("SNAP_USER_COMMON", os.path.expanduser("~"))
            store = UnencryptedFileCredentialStore(os.path.join(creds_prefix, ".launchpad.credentials"))
        else:
            store = UnencryptedFileCredentialStore(lp_credentials_store)

        if lp_user:
            launchpad = Launchpad.login_with(
                lp_user,
                credential_store=store,
                service_root=service_roots['production'], version='devel')
        else:
            # Log in to launchpad annonymously - we use launchpad to find
            # the package publish time
            launchpad = Launchpad.login_anonymously(
                'ubuntu-package-status',
                service_root=service_roots['production'], version='devel')

        ubuntu = launchpad.distributions["ubuntu"]
        ubuntu_archive = ubuntu.main_archive

        if pocket.startswith("ppa:"):
            ppa_owner_and_name = pocket.replace("ppa:", "")
            ppa_owner, ppa_name = ppa_owner_and_name.split("/")
            ubuntu_archive = launchpad.people[ppa_owner].getPPAByName(name=ppa_name)
            archive_pocket = "Release"  # PPAs only have a release pocket
        else:
            archive_pocket = pocket

        lp_series = ubuntu.getSeries(name_or_version=ubuntu_version)

        if package_architecture != "source":
            lp_arch_series = lp_series.getDistroArchSeries(archtag=package_architecture)

            package_published_binaries = ubuntu_archive.getPublishedBinaries(
                exact_match=True,
                binary_name=package,
                pocket=archive_pocket,
                distro_arch_series=lp_arch_series,
                status="Published",
                order_by_date=True,
            )

            if len(package_published_binaries) > 0:
                package_published_binary = package_published_binaries[0]
                binary_package_version = package_published_binary.binary_package_version
                gather_package_stats(binary_package_version, package_published_binary, package_stats)
        else:
            package_published_sources = ubuntu_archive.getPublishedSources(
                exact_match=True,
                source_name=package,
                pocket=archive_pocket,
                distro_series=lp_series,
                status="Published",
                order_by_date=True)
            if len(package_published_sources) > 0:
                package_published_source = package_published_sources[0]
                source_package_version = package_published_source.source_package_version
                gather_package_stats(source_package_version, package_published_source, package_stats)

    except Exception as e:
        logging.error(
            "Error querying launchpad API: %s. \n " "We will retry. \n", str(e)
        )

    return {"package": package,
            "pocket": pocket,
            "ubuntu_version": ubuntu_version,
            "architecture": package_architecture,
            "status": package_stats}


def gather_package_stats(package_version, package_published, package_stats):
    package_stats["full_version"] = package_version
    version = package_version
    package_stats["link"] = package_published.self_link
    try:
        build_link = package_published.build_link.replace('api.', '') \
            .replace('1.0/', '') \
            .replace('devel/', '')
        package_stats["build_link"] = build_link
    except AttributeError as ex:
        # a build link is not available for source packages so if it doesn't exist we can continue
        pass
    # We're really only concerned with the version number up
    # to the last int if it's not a ~ version
    if "~" not in package_version:
        last_version_dot = package_version.find('-')
        version = package_version[0:last_version_dot]
    package_stats["version"] = version
    package_stats[
        "date_published"
    ] = package_published.date_published.isoformat()
    date_published_formatted = format_datetime(
        package_published.date_published
    )
    package_stats["date_published_formatted"] = date_published_formatted
    current_time = pytz.utc.localize(datetime.utcnow())
    published_age = current_time - package_published.date_published
    if published_age < timedelta():
        # A negative timedelta means the time is in the future; this will be
        # due to inconsistent clocks across systems, so assume that there is no
        # delta
        published_age = timedelta()
    published_age = humanize.naturaltime(published_age)
    package_stats["published_age"] = published_age
    package_stats["component"] = package_published.component_name


def initialize_package_stats_dict(package_config, package_architectures=["amd64"], ppas=[]):
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
            for ppa in ppas:
                package_status[ubuntu_version][package].setdefault(
                    ppa, defaultdict(dict)
                )
                for package_architecture in package_architectures:
                    package_status[ubuntu_version][package][ppa].setdefault(
                        package_architecture, defaultdict(dict)
                    )

    return package_status


def get_status_for_all_packages(package_config,
                                package_architectures=["amd64"],
                                ppas=[],
                                lp_user=None,
                                lp_credentials_store=None):
    package_statuses = initialize_package_stats_dict(package_config, package_architectures, ppas)
    ubuntu_version_package_pocket_architecture_combinations = []
    for ubuntu_version, packages in package_statuses.items():
        # find all possible combinations of pocket, architecture and package name and create parallel jobs to query
        # launchpad for details on that package arch and pocket
        package_names = packages.keys()
        pockets = ARCHIVE_POCKETS
        if ppas:
            pockets.extend(ppas)
        # create a list of tuples of all combinations
        possible_combinations = list(product([ubuntu_version], package_names, pockets, package_architectures))
        ubuntu_version_package_pocket_architecture_combinations.extend(possible_combinations)

    n_jobs = -1
    single_package_statuses = Parallel(n_jobs=n_jobs)(
        get_status_for_single_package_by_pocket_and_architecture(ubuntu_version_name,
                                                                 package_name,
                                                                 package_pocket,
                                                                 package_architecture,
                                                                 lp_user=lp_user,
                                                                 lp_credentials_store=lp_credentials_store)
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
    "--series",
    "series",
    multiple=True,
    help="The Ubuntu series eg. '20.04' or 'focal'."
    "This option can be specified multiple times.",
    required=False,
    default=[],

)
@click.option(
    "--package-name",
    "package_names",
    multiple=True,
    help="Binary package name"
    "This option can be specified multiple times.",
    required=False,
    default=[]
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
    "--config-skeleton",
    is_flag=True,
    default=False,
    help="Print example config.",
    show_default=True
)
@click.option(
    "--output-format",
    type=click.Choice(["TXT", "CSV", "JSON"]),
    default="TXT",
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
@click.option(
    "--ppa",
    "ppas",
    required=False,
    multiple=True,
    type=click.STRING,
    help="Additional PPAs that you wish to query for package version status."
    "Expected format is "
    "ppa:'%LAUNCHPAD_USERNAME%/%PPA_NAME%' eg. ppa:philroche/cloud-init"
    "Multiple --ppa options can be specified",
    default=[]
)
@click.option(
    "--launchpad-user",
    "lp_user",
    required=False,
    type=click.STRING,
    help="Launchpad username to use when querying PPAs. This is important id "
         "you are querying PPAs that are not public.",
    default=None
)
@click.option(
    "--launchpad-credentials-store",
    "lp_credentials_store",
    envvar="LP_CREDENTIALS_STORE",
    required=False,
    help="An optional path to an already configured launchpad credentials store.",
    default=None,
)
@click.pass_context
def ubuntu_package_status(
        ctx, config, series, package_names, logging_level, config_skeleton, output_format, package_architectures, ppas,
        lp_user, lp_credentials_store
):
    # type: (Dict, List[Text], List[Text], bool, Text, List[Text], List[Text], Optional[Text], Optional[Text]) -> None
    """
    Watch specified packages in the ubuntu archive for transition between
    archive pockets/PPAs. Useful when waiting for a package update to be published.

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
        # create a dict for each series specified
        package_config = {
            'ubuntu-versions': {}
        }
        for individual_series in series:
            package_config['ubuntu-versions'][individual_series] = {
                'packages': package_names
            }

    # Initialise all package version
    package_status = get_status_for_all_packages(package_config,
                                                 package_architectures,
                                                 list(ppas),
                                                 lp_user,
                                                 lp_credentials_store)
    print_package_status(package_status, output_format)


if __name__ == "__main__":
    ubuntu_package_status(obj={})
