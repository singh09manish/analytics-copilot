"""EMF turns a stdout log line into a CloudWatch metric. The shape is load-bearing:
if `_aws.CloudWatchMetrics` is malformed, CloudWatch silently ignores it and the
metric never appears -- there is no error anywhere."""
import json

from copilot import metrics


def test_emit_writes_valid_emf(capsys):
    metrics.emit("Answered", 1, "Count", role="analyst", intent="data_query")
    line = json.loads(capsys.readouterr().out.strip())
    aws = line["_aws"]
    assert "Timestamp" in aws
    directive = aws["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "AnalyticsCopilot"
    assert {"Name": "Answered", "Unit": "Count"} in directive["Metrics"]
    # Dimensions must be declared AND present as top-level keys, or the metric drops.
    assert ["role", "intent"] in directive["Dimensions"]
    assert line["role"] == "analyst" and line["intent"] == "data_query"
    assert line["Answered"] == 1


def test_emit_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("stdout is gone")

    monkeypatch.setattr("builtins.print", boom)
    metrics.emit("Answered", 1)  # must not raise -- telemetry never breaks a response


def test_emit_coerces_dimension_values_to_str(capsys):
    metrics.emit("Latency", 12.5, "Milliseconds", role=None)
    line = json.loads(capsys.readouterr().out.strip())
    assert line["role"] == "unknown"
