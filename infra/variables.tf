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
}

variable "app_model" {
  type    = string
  default = "claude-sonnet-5"
}
