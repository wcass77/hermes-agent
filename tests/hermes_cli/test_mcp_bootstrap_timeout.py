"""A stalled catalog bootstrap stays inside the catalog's failure contract."""

import subprocess

import pytest

from hermes_cli import mcp_catalog


def test_bootstrap_timeout_stops_install_with_catalog_error(tmp_path, monkeypatch):
    attempted = []

    def stalled(command, **kwargs):
        attempted.append(command)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(mcp_catalog.subprocess, 'run', stalled)
    with pytest.raises(mcp_catalog.CatalogError, match='timed out') as error:
        mcp_catalog._run_bootstrap(tmp_path, ['slow installer', 'next step'])
    assert error.value.failure_class == 'bootstrap_failed'
    assert attempted == ['slow installer']
