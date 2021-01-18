#!/usr/bin/env python3

import csv
import json
import logging
import os
import sys
import yaml

import click

from pkg_resources import resource_filename

from babel.dates import format_datetime
from launchpadlib.launchpad import Launchpad

# Log in to launchpad annonymously - we use launchpad to find
# the package publish time
launchpad = Launchpad.login_anonymously('ubuntu-package-status', 'production', version='devel')
ubuntu = launchpad.distributions["ubuntu"]
ubuntu_archive = ubuntu.main_archive

# Which archive pockets are checked
ARCHIVE_POCKETS = ['Release', 'Proposed', 'Security', 'Updates']


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
                if stats['full_version']:
                    print("\t\t{} {} @ {} ({})".format(
                            pocket,
                            stats['full_version'],
                            stats['date_published'],
                            stats['date_published_formatted']
                    ))


def print_package_status_summary_csv(package_status):
    csv_stdout_writer = csv.writer(sys.stdout)
    csv_stdout_writer.writerow([
        "ubuntu_version",
        "package",
        "pocket",
        "full_version",
        "date_published",
        "date_published_formatted"
    ])
    for ubuntu_version, package_stats in package_status.items():
        for package, pockets in package_stats.items():
            for pocket, stats in pockets.items():
                if stats['full_version']:
                    csv_stdout_writer.writerow([
                        ubuntu_version,
                        package,
                        pocket,
                        stats['full_version'],
                        stats['date_published'],
                        stats['date_published_formatted']
                    ])


def get_status_for_single_package(package, series, pocket):
    package_stats = {"full_version": None,
                     "version": None,
                     "date_published": None,
                     "date_published_formatted": None,
                     "link": None,
                     "binaries": {
                         "amd64": {
                             "version": None,
                             "link": None
                         },
                         "arm64": {
                             "version": None,
                             "link": None
                         }
                     }}
    try:
        lp_series = ubuntu.getSeries(name_or_version=series)
        lp_amd64_arch_series = lp_series.getDistroArchSeries(archtag='amd64')
        lp_arm64_arch_series = lp_series.getDistroArchSeries(archtag='arm64')
        package_published_sources = ubuntu_archive.getPublishedSources(
                exact_match=True,
                source_name=package,
                pocket=pocket,
                distro_series=lp_series,
                status="Published",
                order_by_date=True)

        amd64_package_published_binaries = ubuntu_archive.getPublishedBinaries(
                exact_match=True,
                binary_name=package,
                pocket=pocket,
                distro_arch_series=lp_amd64_arch_series,
                status="Published",
                order_by_date=True)

        arm64_package_published_binaries = ubuntu_archive.getPublishedBinaries(
                exact_match=True,
                binary_name=package,
                pocket=pocket,
                distro_arch_series=lp_arm64_arch_series,
                status="Published",
                order_by_date=True)

        if len(amd64_package_published_binaries) > 0:
            amd64_package_published_binary = amd64_package_published_binaries[0]
            amd64_binary_package_version = amd64_package_published_binary.binary_package_version
            amd64_binary_link = amd64_package_published_binary.build_link.replace('api.', '').replace('1.0/', '')
            package_stats["binaries"]["amd64"][
                "version"] = amd64_binary_package_version
            package_stats["binaries"]["amd64"][
                "link"] = amd64_binary_link

        if len(arm64_package_published_binaries) > 0:
            arm64_package_published_binary = arm64_package_published_binaries[0]
            arm64_binary_package_version = arm64_package_published_binary.binary_package_version
            arm64_binary_link = amd64_package_published_binary.self_link
            package_stats["binaries"]["arm64"][
                "version"] = arm64_binary_package_version
            package_stats["binaries"]["arm64"][
                "link"] = arm64_binary_link

        if len(package_published_sources) > 0:
            package_published_source = package_published_sources[0]

            package_stats["link"] = package_published_source.self_link

            full_version = package_published_source.source_package_version
            version = full_version

            # We're really only concerned with the version number up
            # to the last int if it's not a ~ version
            if "~" not in full_version:
                last_version_dot = full_version.find('-')
                version = full_version[0:last_version_dot]

            package_stats["version"] = version
            package_stats["full_version"] = full_version
            package_stats["date_published"] = \
                package_published_source.date_published.isoformat()
            date_published_formatted = format_datetime(
                    package_published_source.date_published)
            package_stats[
                "date_published_formatted"] = date_published_formatted

    except Exception as e:
        logging.error("Error querying launchpad API: %s. \n "
                      "We will retry. \n", str(e))

    return package_stats


def initialize_package_stats_dict(package_config):
    package_status = dict()

    default_package_stats = {"full_version": None,
                             "version": None,
                             "date_published": None,
                             "date_published_formatted": None,
                             "link": None,
                             "binaries": {
                                 "amd64": {
                                     "version": None,
                                     "link": None
                                 },
                                 "arm64": {
                                     "version": None,
                                     "link": None
                                 }
                             }}

    default_package_versions = {'Release': default_package_stats,
                                'Proposed': default_package_stats,
                                'Updates': default_package_stats,
                                'Security': default_package_stats}

    ubuntu_versions = package_config.get('ubuntu-versions', {})

    # initialise package status
    for ubuntu_version, packages in ubuntu_versions.items():
        package_list = packages.get("packages", [])
        package_status.setdefault(ubuntu_version,
                                  {package: default_package_versions.copy()
                                   for package in package_list})
    return package_status


def get_status_for_all_packages(package_config):
    package_status = initialize_package_stats_dict(package_config)
    for ubuntu_version, packages in package_status.items():
        for package in packages.keys():
            for pocket in ARCHIVE_POCKETS:
                logging.info("Getting stats for {} {} {}".format(
                        ubuntu_version, pocket.lower(), package))
                package_stats = get_status_for_single_package(package,
                                                              ubuntu_version,
                                                              pocket)
                package_status[ubuntu_version][package][pocket] = package_stats
    return package_status


@click.group()
@click.pass_context
def cli(ctx):
    pass


@cli.command()
@click.option('--config', required=False, default=resource_filename(
        'ubuntu_package_status', 'dist-config.yaml'),
        help="Config yaml specifying which packages ubuntu versions to watch."
             "{}".format(" When using the ubuntu-package-status snap this"
                         " config must reside under $HOME."
                                      if os.environ.get('SNAP', None) else ""))
@click.option('--logging-level', type=click.Choice(['DEBUG', 'INFO',
                                                    'WARNING', 'ERROR']),
              required=False, default="ERROR",
              help='How detailed would you like the output.')
@click.option('--config-skeleton', is_flag=True, default=False,
              help='Print example config.')
@click.option('--output-format', type=click.Choice(['TXT', 'CSV', 'JSON']),
              default='TXT')
@click.pass_context
def ubuntu_package_status(ctx, config, logging_level, config_skeleton,
                          output_format):
    # type: (Dict, Text, Text, bool, Text) -> None
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
    logging.basicConfig(level=level, stream=sys.stderr,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    # Parse config
    with open(config, 'r') as config_file:
        package_config = yaml.load(config_file)
        if config_skeleton:
            output = yaml.dump(package_config, Dumper=yaml.Dumper)
            print("# Sample config.")
            print(output)
            exit(0)

    # Initialise all package version
    package_status = get_status_for_all_packages(package_config)
    print_package_status(package_status, output_format)


if __name__ == '__main__':
    cli(obj={})
