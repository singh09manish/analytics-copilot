"""Regression test for scripts/aws_bootstrap_secret.py's failure-path secret hygiene.

Task 8 code review, Finding 1 (CRITICAL): the script's whole promise is "values are
not echoed," but an uncaught `subprocess.CalledProcessError` from a failed `aws
secretsmanager put-secret-value` call used to print the full payload anyway --
`CalledProcessError.__str__` includes the process's `cmd`, and the payload used to be
a `--secret-string <value>` element of that `cmd`. This triggered on ordinary
conditions (expired credentials, wrong region, throttling, a dropped connection), not
just exotic ones. Fixed by moving the payload onto stdin (so it is never part of
`cmd`) and wrapping the call so a failure reports only the exit code.

This test proves the fix hermetically: it points `push_secret` at `/usr/bin/false`
(always exits 1 immediately, no stdout/stderr, no network) and asserts that no marker
value from a fake payload appears anywhere in stdout, stderr, or the raised
SystemExit's own message.
"""
import importlib.util
import pathlib

import pytest

SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "aws_bootstrap_secret.py"
_spec = importlib.util.spec_from_file_location("aws_bootstrap_secret", SCRIPT_PATH)
aws_bootstrap_secret = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aws_bootstrap_secret)

FAKE_PAYLOAD = {
    "ANTHROPIC_API_KEY": "sk-ant-marker-should-never-appear",
    "JWT_SECRET": "jwt-marker-should-never-appear",
    "SNOWFLAKE_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----\nmarker-pem-should-never-appear\n",
}


def test_failed_upload_does_not_leak_payload(monkeypatch, capsys):
    monkeypatch.setattr(aws_bootstrap_secret, "AWS_BIN", "/usr/bin/false")

    with pytest.raises(SystemExit) as exc_info:
        aws_bootstrap_secret.push_secret(FAKE_PAYLOAD)

    captured = capsys.readouterr()
    exit_message = str(exc_info.value)
    for marker in FAKE_PAYLOAD.values():
        assert marker not in captured.out
        assert marker not in captured.err
        assert marker not in exit_message


def test_failed_upload_reports_exit_code_not_command(monkeypatch, capsys):
    monkeypatch.setattr(aws_bootstrap_secret, "AWS_BIN", "/usr/bin/false")

    with pytest.raises(SystemExit) as exc_info:
        aws_bootstrap_secret.push_secret(FAKE_PAYLOAD)

    message = str(exc_info.value)
    assert "exit code 1" in message
    # Naming the operation in prose is fine; a reconstructed argv/cmd list is not --
    # that's exactly the shape CalledProcessError.__str__ produced before this fix.
    assert "--secret-id" not in message
    assert "--secret-string" not in message
    assert "file:///dev/stdin" not in message
    assert "[" not in message  # no Python list repr of the command
