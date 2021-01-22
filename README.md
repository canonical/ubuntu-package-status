# ubuntu-package-status
Helpful utility to fetch package version data for specified packages in the Ubuntu archive.

Similar to [rmadison](http://manpages.ubuntu.com/manpages/focal/man1/rmadison.1.html) utility or the [rmadison webpage](https://people.canonical.com/~ubuntu-archive/madison.cgi) but allows for querying multiple packages at the same time and also support TXT, JSON and CSV output formats.

This makes it easy to use with other tools - like a monitoring tool which is my use case. 

# Usage:
`ubuntu-package-status --help` Shows all available options and their expected values
`ubuntu-package-status --config-skeleton` Shows the format of the expected yaml config

```
ubuntu-package-status 
        --config="config.yaml" 
        --logging-level=DEBUG 
        --output-format=CSV > "package-stats.csv"
```

This will write the current state of your packages in the archive to a "package-stats.csv" file.

# Requirements

[python3-launchpadlib](https://packages.ubuntu.com/focal/python3-launchpadlib)

