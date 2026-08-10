variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "analytics-copilot"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image_tag" {
  description = "ECR tag the ECS task runs. CI updates the service directly, so this only matters for the first apply."
  type        = string
  default     = "latest"
}

variable "github_repo" {
  description = "owner/name of the GitHub repo allowed to assume the deploy role via OIDC."
  type        = string

  # This value feeds directly into an IAM trust policy condition. A typo produces a
  # role nothing can assume (debuggable only via CloudTrail); a stray "*" would
  # widen the trust to repos it shouldn't cover.
  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo must be of the form \"owner/name\" (e.g. \"singh09manish/analytics-copilot\")."
  }
}

variable "github_repo_immutable" {
  description = <<-EOT
    The same repo in GitHub's immutable-subject form, with numeric owner and repo
    ids: owner@<owner_id>/name@<repo_id>. GitHub issues this instead of the classic
    owner/name in the OIDC subject claim so that renaming a repo cannot hand trust
    to whoever registers the old name. Read the exact value with:
      gh api repos/<owner>/<name>/actions/oidc/customization/sub --jq .sub_claim_prefix
    Both forms are accepted by the trust policy, so this is safe to leave at the
    default on an account that still issues the classic form.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+(@[0-9]+)?/[A-Za-z0-9._-]+(@[0-9]+)?$", var.github_repo_immutable))
    error_message = "github_repo_immutable must look like \"owner@123/name@456\"."
  }
}

variable "app_model" {
  type    = string
  default = "claude-sonnet-5"
}
