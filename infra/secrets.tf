resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/runtime"
  description             = "Runtime secrets for the Analytics Copilot backend task."
  recovery_window_in_days = 0 # demo: allow immediate recreate after destroy
}

# Deliberately no aws_secretsmanager_secret_version resource here. Terraform must
# never own this secret's contents -- the original constraint, and it still holds --
# because any value it manages lands in plan output and state in plaintext. An
# earlier revision of this file worked around that by having Terraform create a
# "placeholder" version with "unset" values (so the ECS task definition below had
# something to reference), on the theory that aws_bootstrap_secret.py would
# overwrite it out-of-band and ignore_changes would stop Terraform reverting that.
# It didn't hold: ignore_changes only suppresses in-place updates on a version
# Terraform still tracks in state. The very first `terraform apply` after the
# operator ran `terraform state rm` on it (the then-documented fix for the
# placeholder being garbage-collected after a few rotations) would notice the
# declared resource missing from state and recreate it -- wiping the real,
# already-live secret back to "unset" as AWSCURRENT. Removing the resource
# entirely removes that footgun instead of documenting around it.
#
# `scripts/aws_bootstrap_secret.py` (invoked by `make aws-secret`) is what actually
# creates the secret's first version, straight from `.env` via `aws secretsmanager
# put-secret-value` -- outside Terraform entirely, so no value it pushes ever
# reaches Terraform state. It must run once after the first `terraform apply` on a
# fresh account, before the first deploy.
#
# If it's skipped: aws_secretsmanager_secret.app exists but has zero versions, so
# the ECS task's `secrets` block (below) fails to resolve at container start and
# the task never comes up. That's a loud, visible failure -- not a silent one, and
# not an insecure one (there is no "unset" value for the JWT gate to crash-loop on
# or, worse, quietly accept) -- so it surfaces immediately as a failed deployment
# rather than as a security hole discovered later.
