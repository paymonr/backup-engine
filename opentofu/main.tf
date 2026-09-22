# opentofu/main.tf
resource "aws_s3_bucket" "backup" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration {
    status = var.base_bucket_versioned ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  dynamic "rule" {
    for_each = toset(["appdata/", "media/"])
    content {
      id     = "backstop-${replace(rule.value, "/", "")}"
      status = "Enabled"
      filter { prefix = rule.value }
      noncurrent_version_expiration { noncurrent_days = var.noncurrent_version_expiration_days }
      abort_incomplete_multipart_upload { days_after_initiation = var.abort_incomplete_multipart_days }
    }
  }
}

# --- Least-privilege runtime IAM (object-only on the two prefixes) ---
resource "aws_iam_user" "runtime" {
  name = "${var.name_prefix}-runtime"
}

resource "aws_iam_user_policy" "runtime" {
  name = "${var.name_prefix}-runtime-object-only"
  user = aws_iam_user.runtime.name
  policy = templatefile("${path.module}/../provisioning/iam-policy.json.tmpl", {
    bucket_arn            = aws_s3_bucket.backup.arn
    bucket_admin_role_arn = aws_iam_role.bucket_admin.arn
  })
}

resource "aws_iam_access_key" "runtime" {
  user = aws_iam_user.runtime.name
}

# --- Bucket-admin role: create/config/version-toggle for per-job dedicated
# buckets (spec: extra buckets are provisioned via AssumeRole, never on the
# everyday runtime key). Also carries a separate Teardown statement (enumerate
# by tag + empty + delete) so JIT buckets -- created outside tofu -- can be
# torn down; those perms stay on this role only, never the runtime key.
resource "aws_iam_role" "bucket_admin" {
  name = "${var.name_prefix}-bucket-admin"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_user.runtime.arn }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "bucket_admin" {
  name = "${var.name_prefix}-bucket-admin-create-config"
  role = aws_iam_role.bucket_admin.name
  policy = templatefile("${path.module}/../provisioning/bucket-admin-policy.json.tmpl", {
    bucket = var.bucket_name
  })
}
