"""Push the local .env values into Secrets Manager.

Terraform creates the secret but never its contents, so no secret value ever lands
in Terraform state. Run this once after the first apply, and again whenever a
credential rotates.
"""
import json
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_NAME = "analytics-copilot/runtime"
REGION = "us-east-1"

# Same PATH problem the Makefile already works around for `uv`: a local install
# puts the AWS CLI in ~/.local/bin, which a `uv run` subprocess doesn't inherit
# on this machine. Prefer whatever's on PATH, fall back to the standard location.
AWS_BIN = shutil.which("aws") or str(pathlib.Path.home() / ".local/bin/aws")

# Values copied straight from .env.
FROM_ENV = [
    "ANTHROPIC_API_KEY",
    "JWT_SECRET",
    "DEMO_ANALYST_PASSWORD_HASH",
    "DEMO_ADMIN_PASSWORD_HASH",
    "SNOWFLAKE_ACCOUNT",
]


def read_env() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        sys.exit(f"No {env_path}. Nothing to upload.")
    out: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def push_secret(payload: dict[str, str]) -> None:
    """Upload payload as the secret's new version. A failure must never echo it.

    The payload goes to the AWS CLI over stdin (`file:///dev/stdin`), never as a
    `--secret-string <value>` argv element: argv is visible to any other process on
    the box via `ps` for the whole subprocess lifetime, and -- the sharper problem --
    an uncaught `subprocess.CalledProcessError`'s `__str__` includes the full `cmd`
    list. With the payload on argv, that meant every ordinary failure (expired
    credentials, wrong region, ResourceNotFoundException, throttling, a dropped
    connection) printed ANTHROPIC_API_KEY, JWT_SECRET, both password hashes,
    SNOWFLAKE_ACCOUNT, and the whole Snowflake private-key PEM straight to the
    operator's terminal -- the exact thing this script's own "values are not
    echoed" promise says never happens. The `except` below reports only the exit
    code, never the command or the payload. stdout is still suppressed (the CLI's
    success response can include metadata like ARN/VersionId, which is not secret
    but also not useful here); stderr is left connected to the terminal so the AWS
    CLI's own error text -- which never contains the payload -- still reaches the
    operator, so they can see *why* it failed even though this script won't say.
    """
    try:
        subprocess.run(
            [AWS_BIN, "secretsmanager", "put-secret-value",
             "--secret-id", SECRET_NAME, "--region", REGION,
             "--secret-string", "file:///dev/stdin"],
            input=json.dumps(payload), text=True,
            check=True, stdout=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        sys.exit(
            f"aws secretsmanager put-secret-value failed (exit code {e.returncode}). "
            "See the AWS CLI's error output above for why. (Deliberately not printed "
            "here: the command and its payload.)"
        )


def main() -> None:
    env = read_env()
    payload = {}
    for key in FROM_ENV:
        value = env.get(key, "")
        if not value or value == "dev-secret-change-me":
            sys.exit(f"{key} is missing or still the placeholder in .env. Fix it first.")
        payload[key] = value

    key_path = REPO_ROOT / env.get("SNOWFLAKE_PRIVATE_KEY_PATH", "secrets/copilot_svc_key.p8")
    if not key_path.exists():
        sys.exit(f"Snowflake private key not found at {key_path}.")
    payload["SNOWFLAKE_PRIVATE_KEY_PEM"] = key_path.read_text()

    push_secret(payload)
    print(f"Uploaded {len(payload)} keys to {SECRET_NAME}: {', '.join(sorted(payload))}")
    print("Values are not echoed. Roll the task to pick them up:")
    print("  aws ecs update-service --cluster analytics-copilot "
          "--service analytics-copilot --force-new-deployment")


if __name__ == "__main__":
    main()
