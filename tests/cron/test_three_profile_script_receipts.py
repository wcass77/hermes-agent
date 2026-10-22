"""Concurrent script-only fires retain three distinct homes, credentials and receipts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading

import yaml

from agent.secret_scope import set_multiplex_active
from cron.scheduler_script import _run_job_script
from gateway.run import _profile_runtime_scope


def test_three_profile_fires_keep_credentials_and_receipts_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-launch")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-launch-bootstrap")
    (root / ".env").write_text("OP_SERVICE_ACCOUNT_TOKEN=synthetic-launch-bootstrap\n")
    homes = [root / "profiles" / name for name in ("pilot", "scorpius", "zhaan")]
    for home in homes:
        (home / "scripts").mkdir(parents=True)
        (home / ".env").write_text("BUSINESS_API_KEY=synthetic-" + home.name + "\n")
        (home / "config.yaml").write_text(yaml.safe_dump({"terminal": {"env_passthrough": ["BUSINESS_API_KEY"]}}))
        (home / "scripts/probe.py").write_text(
            "import json, os\nfrom pathlib import Path\n"
            "home=Path(os.environ['HERMES_HOME'])\n"
            "receipt={'profile':home.name,'own_credential':os.environ.get('BUSINESS_API_KEY')=='synthetic-'+home.name,"
            "'bootstrap_absent':'OP_SERVICE_ACCOUNT_TOKEN' not in os.environ,"
            "'provider_absent':'OPENAI_API_KEY' not in os.environ}\n"
            "(home/'state').mkdir(exist_ok=True)\n"
            "(home/'state/probe-receipt.json').write_text(json.dumps(receipt))\n"
            "print(json.dumps(receipt))\n")
    before = dict(os.environ)
    barrier = threading.Barrier(3)

    def fire(home):
        with _profile_runtime_scope(home):
            barrier.wait(timeout=10)
            success, output = _run_job_script("probe.py", interpreter=sys.executable)
            assert success, output
            return json.loads(output)

    set_multiplex_active(True)
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            receipts = list(executor.map(fire, homes))
        assert receipts == [{"profile": home.name, "own_credential": True, "bootstrap_absent": True, "provider_absent": True} for home in homes]
        for home, receipt in zip(homes, receipts):
            assert json.loads((home / "state/probe-receipt.json").read_text()) == receipt
        assert not (root / "state/probe-receipt.json").exists()
        assert os.environ == before
    finally:
        set_multiplex_active(False)
