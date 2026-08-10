"""Push the local .env values into Secrets Manager.

Terraform creates the secret but never its contents, so no secret value ever lands
in Terraform state. Run this once after the first apply, and again whenever a
credential rotates.
"""
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_NAME = "analytics-copilot/runtime"
REGION = "us-east-1"

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

    subprocess.run(
        ["aws", "secretsmanager", "put-secret-value",
         "--secret-id", SECRET_NAME, "--region", REGION,
         "--secret-string", json.dumps(payload)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"Uploaded {len(payload)} keys to {SECRET_NAME}: {', '.join(sorted(payload))}")
    print("Values are not echoed. Roll the task to pick them up:")
    print("  aws ecs update-service --cluster analytics-copilot "
          "--service analytics-copilot --force-new-deployment")


if __name__ == "__main__":
    main()
