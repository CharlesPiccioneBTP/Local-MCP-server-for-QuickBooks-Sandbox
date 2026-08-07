"""A read-only MCP server for QuickBooks Online.

The package is arranged so that the read-only property is structural rather than
a matter of discipline:

* `client.py` is the only module that can reach the QuickBooks API host. It
  issues GET and nothing else, to two path prefixes and nothing else.
* `auth.py` is the only module that issues a POST, and its client is pinned to
  Intuit's OAuth token endpoint, which is not the accounting API.

`tests/test_readonly.py` enforces both statements against the source tree.
"""

__version__ = "0.1.0"
