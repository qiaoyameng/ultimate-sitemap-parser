.. _cli:

CLI Reference
-------------

The CLI provides a simple command-line interface to retrieve sitemap data.

``usp``
=======

.. code-block:: none

    usage: usp [-h] [-v]  ...

    Ultimate Sitemap Parser

    options:
      -h, --help     show this help message and exit
      -v, --version  show program's version number and exit

    commands:

        ls           List sitemap pages
        alternate-check  Diagnose hreflang alternate relationships from a local sitemap

``usp ls``
==========

.. code-block:: none

    usage: usp ls [-h] [-f FORMAT] [-r] [-k] [-u] [-v/-vv] [-l LOG_FILE] url

    download, parse and list the sitemap structure

    positional arguments:
      url              URL of the site including protocol

    options:
      -h, --help       show this help message and exit
      -f FORMAT, --format FORMAT
                            set output format (default: tabtree)
                            choices:
                              tabtree: Sitemaps and pages, nested with tab indentation
                              pages: Flat list of pages, one per line
      -r, --no-robots  don't discover sitemaps through robots.txt
      -k, --no-known   don't discover sitemaps through well-known URLs
      -u, --strip-url  strip the supplied URL from each page and sitemap URL
      -v, --verbose         increase output verbosity (-v=INFO, -vv=DEBUG)
      -l LOG_FILE, --log-file LOG_FILE
                            write log to this file and suppress console output

.. rubric:: Examples

.. code-block:: shell-session

    $ usp ls https://example.org/
    https://example.org/
        https://example.org/robots.txt
            https://example.org/sitemap.xml
                https://example.org/page1.html


.. code-block:: shell-session

    $ usp ls https://example.org/ --strip-url
    https://example.org/
        /robots.txt
            /sitemap.xml
                /page1.html

.. code-block:: shell-session

    $ usp ls https://example.org/ --format pages
    https://example.org/page1.html

``usp alternate-check``
=======================

Offline diagnostic for ``hreflang`` alternate relationships. Reads a sitemap
document from a file or stdin, parses it in memory and emits a JSON report
without requesting any page body. See :doc:`/guides/alternate-diagnostics`
for the semantics of coverage, cluster ids and issue types.

.. code-block:: none

    usage: usp alternate-check [-h] [-c COVERAGE] [--fully-covered-url URL]
                               [-o OUTPUT] [-v/-vv] [-l LOG_FILE] sitemap

    positional arguments:
      sitemap               path to a sitemap document, use '-' to read it from stdin

    options:
      -c COVERAGE, --coverage COVERAGE
                            coverage statement for the input (default: complete)
                            choices:
                              complete: treat input as a complete page set
                              partial:  treat input as an incomplete subtree
      --fully-covered-url URL
                            URL belonging to a subtree declared fully covered;
                            may be repeated (only meaningful with --coverage partial)
      -o OUTPUT, --output OUTPUT
                            write the JSON report to this file instead of stdout

The exit status is ``1`` when confirmed errors are found and ``0`` otherwise;
insufficient-evidence findings do not cause a non-zero exit status.

.. rubric:: Examples

.. code-block:: shell-session

    $ usp alternate-check sitemap.xml --coverage complete
    $ cat sitemap.xml | usp alternate-check - --coverage partial
