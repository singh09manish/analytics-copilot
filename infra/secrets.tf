resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/runtime"
  description             = "Runtime secrets for the Analytics Copilot backend task."
  recovery_window_in_days = 0 # demo: allow immediate recreate after destroy
}

# Placeholder so the ECS task definition can reference specific JSON keys before the
# operator has pushed real values. aws_bootstrap_secret.py overwrites this wholesale.
resource "aws_secretsmanager_secret_version" "placeholder" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    ANTHROPIC_API_KEY          = "unset"
    JWT_SECRET                 = "unset"
    DEMO_ANALYST_PASSWORD_HASH = "unset"
    DEMO_ADMIN_PASSWORD_HASH   = "unset"
    SNOWFLAKE_ACCOUNT          = "unset"
    SNOWFLAKE_PRIVATE_KEY_PEM  = "unset"
  })

  lifecycle {
    ignore_changes = [secret_string] # the operator's real values must not be reverted
  }

  # ignore_changes only suppresses in-place updates -- it does not stop Terraform
  # from recreating this resource if the tracked version is garbage-collected after
  # a couple of credential rotations, which would restore the "unset" placeholder.
  # Blast radius is limited (the app's JWT length gate crash-loops on "unset"
  # rather than running insecurely) but is still an outage. After the first
  # successful run of aws_bootstrap_secret.py, the operator should run
  # `terraform state rm aws_secretsmanager_secret_version.placeholder` so
  # Terraform stops tracking this resource entirely.
}
