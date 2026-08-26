# opentofu/main.tf
resource "aws_s3_bucket" "backup" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration { status = "Enabled" }
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

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "ListBucketScoped"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.backup.arn]
  }
  statement {
    sid = "ObjectRW"
    actions = [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
      "s3:RestoreObject",
    ]
    resources = [
      "${aws_s3_bucket.backup.arn}/appdata/*",
      "${aws_s3_bucket.backup.arn}/media/*",
    ]
  }
}

resource "aws_iam_user_policy" "runtime" {
  name   = "${var.name_prefix}-runtime-object-only"
  user   = aws_iam_user.runtime.name
  policy = data.aws_iam_policy_document.runtime.json
}

resource "aws_iam_access_key" "runtime" {
  user = aws_iam_user.runtime.name
}
