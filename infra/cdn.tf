resource "aws_s3_bucket" "web" {
  bucket        = "${local.name}-web-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # demo: let terraform destroy remove the built SPA
}

# The bucket stays private; only CloudFront may read it, via Origin Access Control.
resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn }
      }
    }]
  })
}

# API responses must never be cached, and the Authorization header has to survive
# the hop to the ALB -- CloudFront strips it by default.
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${local.name}-api-nocache"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

resource "aws_cloudfront_origin_request_policy" "api" {
  name = "${local.name}-api-forward-all"

  cookies_config { cookie_behavior = "all" }
  query_strings_config { query_string_behavior = "all" }
  headers_config {
    # AWS's own "AllViewerExceptHostHeader" managed policy isn't a header_behavior
    # value on a custom policy -- the provider's enum is none/whitelist/allViewer/
    # allViewerAndWhitelistCloudFront/allExcept, so the same effect (forward every
    # viewer header except Host, which must stay the ALB's own so it can route)
    # is expressed as allExcept + an explicit exclusion list.
    header_behavior = "allExcept"
    headers {
      items = ["Host"]
    }
  }
}

resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  default_root_object = "index.html"
  comment             = local.name
  price_class         = "PriceClass_100" # NA + EU only; cheapest

  origin {
    origin_id                = "s3-web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id   = "alb-api"
    domain_name = aws_lb.app.dns_name

    custom_origin_config {
      http_port  = 80
      https_port = 443
      # CloudFront terminates TLS for the browser and talks HTTP to the ALB inside
      # AWS. Putting a cert on the ALB would require owning a domain.
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # /chat is a non-streaming JSON endpoint that makes 3-4 sequential Claude
      # calls around one or two Snowflake queries and routinely runs past
      # CloudFront's 30s default origin read timeout -- the same reason the ALB
      # itself (alb.tf) got idle_timeout = 180. 60s is CloudFront's default quota
      # ceiling (a higher value needs an AWS quota increase request), so it's the
      # most headroom available without one. Without this, most real /chat calls
      # would 504 at the edge while the backend keeps working.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # AWS managed: CachingOptimized
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = aws_cloudfront_cache_policy.api.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.api.id
  }

  # A single-page app owns its own routing: unknown paths must return index.html,
  # not S3's 403.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net, no domain needed
  }
}
