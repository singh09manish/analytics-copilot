output "app_url" {
  description = "The single HTTPS entry point: SPA at /, API at /api/*."
  value       = "https://${aws_cloudfront_distribution.web.domain_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "web_bucket" {
  value = aws_s3_bucket.web.bucket
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.web.id
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repo variable in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "secret_name" {
  value = aws_secretsmanager_secret.app.name
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}
