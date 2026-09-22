# opentofu/outputs.tf
output "bucket_name" { value = aws_s3_bucket.backup.id }
output "region" { value = var.region }
output "restic_repository" {
  value = "s3:s3.${var.region}.amazonaws.com/${aws_s3_bucket.backup.id}/appdata"
}
output "runtime_access_key_id" {
  value     = aws_iam_access_key.runtime.id
  sensitive = true
}
output "runtime_secret_access_key" {
  value     = aws_iam_access_key.runtime.secret
  sensitive = true
}
output "bucket_admin_role_arn" {
  value = aws_iam_role.bucket_admin.arn
}
output "runtime_user_arn" {
  value = aws_iam_user.runtime.arn
}
output "rclone_remote" {
  value = {
    type          = "s3"
    provider      = "AWS"
    region        = var.region
    storage_class = "DEEP_ARCHIVE"
    bucket        = aws_s3_bucket.backup.id
  }
}
output "permissions_level" {
  value = jsondecode(file("${path.module}/../provisioning/permissions.json")).level
}
