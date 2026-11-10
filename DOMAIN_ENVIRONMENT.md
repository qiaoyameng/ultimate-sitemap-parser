# Reproducible offline environment

Upstream: https://github.com/GateNLP/ultimate-sitemap-parser
Upstream SHA: 6ab7704f53ad06fc9bd9742ef9c91848b8f9d7f5
Runtime: 3.13.14 (tags/v3.13.14:fd17997, Jun 10 2026, 13:03:48) [MSC v.1944 64 bit (AMD64)]
Platform: win32

Create an isolated virtual environment, then run from this checkout:
```
python -m pip install -r requirements-domain.lock
python -m pip install -e .
```
Set `PYTHONUTF8=1` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` before tests.
```
python -m pytest -q -o addopts= -o log_cli=false -p pytest_mock -p requests_mock.contrib._pytest_plugin tests/tree tests/web_client tests/test_helpers.py -k "not test_get_nonexistent_domain"
```
Measured selected pytest process: 4.035 seconds.
Excludes the nonexistent-domain DNS test, whose result depends on the host proxy. Remaining selected tests use local fixtures and requests mocks.

Tests use synthetic in-memory or temporary-file inputs under the upstream license.
No account, cloud service, browser or external database is needed. Recreate the
environment from this immutable commit to reset the starting state.
