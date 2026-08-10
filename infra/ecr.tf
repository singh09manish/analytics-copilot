resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # a demo registry should not block terraform destroy

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Two separate rules, not one "any" rule: tagStatus = "any" would expire SHA-tagged
# images right alongside untagged layers, destroying rollback and forensic history.
# The deploy pipeline pushes both `:<git-sha>` and `:latest`, so untagged layers are
# only ever superseded push artifacts -- pure cost -- while tagged images are the
# rollback trail and must be pruned by count instead of age.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 10 most recent tagged images"
        selection    = { tagStatus = "tagged", tagPrefixList = ["*"], countType = "imageCountMoreThan", countNumber = 10 }
        action       = { type = "expire" }
      },
    ]
  })
}
