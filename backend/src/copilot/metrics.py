"""CloudWatch Embedded Metric Format.

A JSON line on stdout with an `_aws` block is parsed by the CloudWatch Logs agent
into a metric. Because the ECS task already ships stdout to CloudWatch Logs via the
awslogs driver, this needs no PutMetricData call, no boto3, and no IAM permission --
which is why the ECS *task* role can stay empty of AWS permissions.

The trade-off: a malformed `_aws` block is ignored silently. There is no error and
no metric. That is why the shape is pinned by a test.
"""
import json
import time

NAMESPACE = "AnalyticsCopilot"


def emit(name: str, value: float, unit: str = "Count", **dimensions: str) -> None:
    try:
        dims = {k: (str(v) if v is not None else "unknown") for k, v in dimensions.items()}
        payload = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dims)] if dims else [[]],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }],
            },
            name: value,
            **dims,
        }
        print(json.dumps(payload), flush=True)
    except Exception:  # noqa: BLE001 -- telemetry must never break a response
        pass
